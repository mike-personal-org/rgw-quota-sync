"""Read RGW user quotas from Keystone unified limits.

Two resources of the object-store service set an RGW user's quota. For each,
a registered limit is the default, and a project limit overrides it.
https://docs.openstack.org/keystone/latest/admin/unified-limits.html
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Self

import openstack

SERVICE_TYPE = "object-store"
# Keystone limits are at most 2**31 - 1, too small for bytes, so size is in GiB.
MAX_SIZE = "rgw_user_max_size_gib"
MAX_OBJECTS = "rgw_user_max_objects"
GIB = 1024**3

# Keystone shows a token every project's limits only if it is system scoped or
# has one of these roles. Otherwise it silently leaves out other projects'.
ALL_PROJECTS_ROLES = {"admin", "service"}


def project_id(rgw_uid: str) -> str:
    """Return the Keystone project ID of an RGW user created by Keystone.

    rgw_uid is "<project_id>$<project_id>" (RGW's "<tenant>$<user>"), or
    just "<project_id>" for users created before rgw_keystone_implicit_tenants.
    """
    return rgw_uid.split("$")[0]


@dataclass(frozen=True)
class Quota:
    max_size: int  # bytes; -1 = unlimited, 0 blocks all writes
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
    projects: dict[str, Quota]  # Keystone project ID (not the RGW uid) -> quota

    def quota_for(self, rgw_uid: str) -> Quota:
        """Return the quota for an RGW user: its project's, or the default."""
        return self.projects.get(project_id(rgw_uid), self.default)


class Identity(Protocol):
    """The part of a keystoneauth Adapter for Keystone v3 used here."""

    def get(self, url: str, *, params: Mapping[str, str]) -> Any: ...


def check_scope(system_scoped: bool, role_names: list[str]) -> None:
    """Raise PermissionError unless the token can list every project's limits."""
    if system_scoped or ALL_PROJECTS_ROLES & set(role_names):
        return
    raise PermissionError(
        "Keystone only lists a project or domain token's own limits: use a "
        "system scoped token, e.g. with the reader role on the system"
    )


def service_id(catalog: list[dict[str, Any]]) -> str:
    """Return the ID of the object-store service in a Keystone v3 catalog."""
    ids = [service["id"] for service in catalog if service["type"] == SERVICE_TYPE]
    if len(ids) != 1:
        raise LookupError(
            f"want one {SERVICE_TYPE} service in the Keystone catalog, found {len(ids)}"
        )
    return ids[0]


def _list(identity: Identity, collection: str, params: Mapping[str, str]) -> list:
    body = identity.get(f"/{collection}", params=params).json()
    # Keystone cuts a list at [unified_limit] list_limit, and can't page.
    if body.get("truncated"):
        raise RuntimeError(
            f"Keystone truncated the {collection} list: raise its "
            "[unified_limit] list_limit"
        )
    return body[collection]


def load_quotas(identity: Identity, service: str, region: str | None) -> Quotas:
    """Read the default quota and every project's override from Keystone.

    Like oslo.limit, only reads limits of the given region, if there is one.
    Raises LookupError if a registered limit is missing or ambiguous.
    """
    where = f"service {service}" + (f" in region {region}" if region else "")
    hint = "if they are for different regions, set the cloud's region_name"
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
                f"found {len(registered)}" + (f" ({hint})" if registered else "")
            )
        defaults[resource] = registered[0]["default_limit"]

        for limit in _list(identity, "limits", params):
            project = limit.get("project_id")
            if not project:
                continue  # a domain limit: RGW users belong to projects
            if resource in overrides.setdefault(project, {}):
                raise LookupError(
                    f"project {project} has several {resource} limits for "
                    f"{where} ({hint})"
                )
            overrides[project][resource] = limit["resource_limit"]

    return Quotas(
        default=Quota.from_limits(defaults),
        # A resource the project has no limit for keeps the default.
        projects={
            project: Quota.from_limits({**defaults, **limits})
            for project, limits in overrides.items()
        },
    )


def load_from_cloud() -> Quotas:
    """Read the quotas, authenticating as OS_CLOUD (clouds.yaml) or OS_* say."""
    conn = openstack.connect()
    access = conn.session.auth.get_access(conn.session)
    check_scope(access.system_scoped, access.role_names or [])
    return load_quotas(
        conn.identity,
        service_id(access.service_catalog.catalog),
        conn.config.region_name,
    )
