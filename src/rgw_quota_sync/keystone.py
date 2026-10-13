"""Read RGW user quotas from Keystone unified limits.

Two resources of the object-store service set an RGW user's quota. For each,
a registered limit is the default, and a project limit overrides it.
https://docs.openstack.org/keystone/latest/admin/unified-limits.html
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Self

import openstack

log = logging.getLogger(__name__)

SERVICE_TYPE = "object-store"
# Keystone limits are at most 2**31 - 1, too small for bytes, so size is in GiB.
MAX_SIZE = "rgw_user_max_size_gib"
MAX_OBJECTS = "rgw_user_max_objects"
GIB = 1024**3


def project_id(rgw_uid: str) -> str:
    """Return the Keystone project ID of an RGW user created by Keystone.

    rgw_uid is "<project_id>$<project_id>" (RGW's "<tenant>$<user>"), or
    just "<project_id>" for users created before rgw_keystone_implicit_tenants.
    Raises ValueError for any other uid.
    """
    tenant, dollar, user = rgw_uid.partition("$")
    if dollar and tenant != user:
        raise ValueError("uid is neither <project> nor <project>$<project>")
    return tenant


@dataclass(frozen=True)
class Quota:
    max_size: int  # bytes; -1 = unlimited, 0 still allows empty objects
    max_objects: int  # -1 = unlimited, 0 blocks all writes

    @classmethod
    def from_limits(cls, limits: Mapping[str, int]) -> Self:
        """Return the quota from Keystone limit values, by resource name."""
        size = limits[MAX_SIZE]
        return cls(size if size == -1 else size * GIB, limits[MAX_OBJECTS])

    def __str__(self) -> str:
        return f"max_size={self.max_size} max_objects={self.max_objects}"


@dataclass
class Quotas:
    default: Quota  # from the registered limits
    overrides: dict[str, Quota]  # Keystone project ID (not the RGW uid) -> quota
    project_ids: set[str]  # every project in Keystone

    def quota_for(self, project: str) -> Quota:
        """Return a Keystone project's quota: its override, or the default."""
        return self.overrides.get(project, self.default)


class Identity(Protocol):
    """The part of a keystoneauth Adapter for Keystone v3 used here."""

    def get(self, url: str, *, params: Mapping[str, str], raise_exc: bool) -> Any: ...


def check_scope(system_scoped: bool) -> None:
    """Raise PermissionError unless the token is system scoped.

    Keystone 30 also shows every project's limits to a project token with the
    admin or service role, but silently filters out other projects' for any
    token it doesn't trust that far. Only system scope is documented to see
    them all, so don't rely on the rest.
    """
    if not system_scoped:
        raise PermissionError(
            "Keystone may list only some projects' limits for this token: use a "
            "system scoped token, e.g. with the reader role on the system"
        )


def find_service(
    catalog: list[dict[str, Any]], endpoint_id: str | None, region: str | None
) -> tuple[str, str | None]:
    """Return the service ID and region to read limits for.

    With endpoint_id, both come from that endpoint in the Keystone v3 catalog,
    like oslo.limit's [oslo_limit] endpoint_id. Otherwise they are the only
    object-store service and the given region (the cloud's).
    """
    if endpoint_id is None:
        ids = [service["id"] for service in catalog if service["type"] == SERVICE_TYPE]
        if len(ids) != 1:
            raise LookupError(
                f"want one {SERVICE_TYPE} service in the Keystone catalog, "
                f"found {len(ids)}: set keystone_endpoint_id"
            )
        return ids[0], region

    for service in catalog:
        for endpoint in service["endpoints"]:
            if endpoint["id"] == endpoint_id:
                return service["id"], endpoint.get("region_id")
    raise LookupError(f"endpoint {endpoint_id} is not in the Keystone catalog")


def _list(identity: Identity, collection: str, params: Mapping[str, str]) -> list:
    # openstacksdk returns HTTP errors as responses unless told to raise.
    body = identity.get(f"/{collection}", params=params, raise_exc=True).json()
    # Keystone can cut a list short (list_limit options) and has no way to
    # page, so a truncated list would miss limits or projects.
    if body.get("truncated"):
        raise RuntimeError(f"Keystone truncated the {collection} list")
    return body[collection]


def load_quotas(identity: Identity, service: str, region: str | None) -> Quotas:
    """Read the default quota, every project's override and all project IDs.

    Like oslo.limit, only reads limits of the given region, if there is one.
    Raises LookupError if a registered limit is missing or ambiguous.
    """
    # Each project gets its own limit, which doesn't fit a hierarchy.
    model = identity.get("/limits/model", params={}, raise_exc=True).json()
    if model["model"]["name"] != "flat":
        log.warning(
            "Keystone limits: enforcement model is %s, but each project's RGW "
            "user gets its own limit, not a share of its parent's",
            model["model"]["name"],
        )

    where = f"service {service}" + (f" in region {region}" if region else "")
    defaults: dict[str, int] = {}
    overrides: dict[str, dict[str, int]] = {}  # project ID -> resource -> limit
    for resource in (MAX_SIZE, MAX_OBJECTS):
        params = {"service_id": service, "resource_name": resource}
        if region:
            params["region_id"] = region

        registered = _list(identity, "registered_limits", params)
        if len(registered) != 1:
            raise LookupError(
                f"want one registered limit {resource} for {where}, "
                f"found {len(registered)}"
                + (" for several regions: set a region" if registered else "")
            )
        defaults[resource] = registered[0]["default_limit"]

        # Each matches the one registered limit, so a project has at most one.
        for limit in _list(identity, "limits", params):
            if project := limit.get("project_id"):  # not a domain limit
                overrides.setdefault(project, {})[resource] = limit["resource_limit"]

    return Quotas(
        default=Quota.from_limits(defaults),
        # A resource the project has no limit for keeps the default.
        overrides={
            project: Quota.from_limits({**defaults, **limits})
            for project, limits in overrides.items()
        },
        project_ids={project["id"] for project in _list(identity, "projects", {})},
    )


def load_from_cloud(endpoint_id: str | None) -> Quotas:
    """Read the quotas, authenticating as OS_CLOUD (clouds.yaml) or OS_* say.

    endpoint_id is RGW's endpoint in the Keystone catalog, or None.
    """
    conn = openstack.connect()
    access = conn.session.auth.get_access(conn.session)
    check_scope(access.system_scoped)
    service, region = find_service(
        access.service_catalog.catalog, endpoint_id, conn.config.region_name
    )
    return load_quotas(conn.identity, service, region)
