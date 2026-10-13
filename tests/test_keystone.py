import logging

import pytest

from rgw_quota_sync import keystone
from rgw_quota_sync.keystone import GIB, MAX_OBJECTS, MAX_SIZE, Quota

SERVICE = "svc"


def registered(resource, default, region=None):
    return {
        "service_id": SERVICE,
        "region_id": region,
        "resource_name": resource,
        "default_limit": default,
    }


def limit(resource, value, project=None, domain=None, region=None):
    return {
        "service_id": SERVICE,
        "region_id": region,
        "resource_name": resource,
        "resource_limit": value,
        "project_id": project,
        "domain_id": domain,
    }


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeIdentity:
    """Answers like Keystone's GET /v3/registered_limits and /v3/limits."""

    def __init__(self, registered_limits, limits, projects=("big",)):
        self.collections = {
            "registered_limits": registered_limits,
            "limits": limits,
            "projects": [{"id": project} for project in projects],
        }
        self.truncated = False
        self.model = "flat"

    def get(self, url, *, params, raise_exc):
        assert raise_exc  # or a Keystone error would look like an empty answer
        if url == "/limits/model":
            return FakeResponse({"model": {"name": self.model, "description": ""}})
        collection = url.lstrip("/")
        matching = [
            entry
            for entry in self.collections[collection]
            if all(entry[key] == value for key, value in params.items())
        ]
        body = {collection: matching}
        if self.truncated:
            body["truncated"] = True
        return FakeResponse(body)


def defaults(region=None):
    return [
        registered(MAX_SIZE, 10 * 1024, region),  # 10 TiB
        registered(MAX_OBJECTS, -1, region),
    ]


def test_load_quotas():
    identity = FakeIdentity(
        defaults(),
        [
            limit(MAX_SIZE, 50 * 1024, project="big"),
            limit(MAX_SIZE, -1, project="unlimited"),
            limit(MAX_SIZE, 0, project="read_only"),
            limit(MAX_OBJECTS, 0, project="read_only"),
            limit(MAX_OBJECTS, 1000, project="objects_only"),
            limit(MAX_SIZE, 1, domain="some_domain"),  # not a project, ignored
        ],
    )
    quotas = keystone.load_quotas(identity, SERVICE, region=None)
    assert quotas.default == Quota(max_size=10 * 1024 * GIB, max_objects=-1)
    assert quotas.overrides == {
        "big": Quota(max_size=50 * 1024 * GIB, max_objects=-1),
        "unlimited": Quota(max_size=-1, max_objects=-1),  # -1 isn't multiplied
        "read_only": Quota(max_size=0, max_objects=0),
        # A resource without a project limit keeps the default.
        "objects_only": Quota(max_size=10 * 1024 * GIB, max_objects=1000),
    }
    assert quotas.quota_for("big") == quotas.overrides["big"]
    assert quotas.quota_for("new") == quotas.default
    assert quotas.project_ids == {"big"}


def test_load_quotas_reads_only_its_region():
    identity = FakeIdentity(
        defaults("here") + [registered(MAX_SIZE, 1, "other")],
        [
            limit(MAX_SIZE, 50 * 1024, project="big", region="here"),
            limit(MAX_SIZE, 1, project="big", region="other"),
            limit(MAX_SIZE, 1, project="elsewhere", region="other"),
        ],
    )
    quotas = keystone.load_quotas(identity, SERVICE, region="here")
    assert quotas.default == Quota(max_size=10 * 1024 * GIB, max_objects=-1)
    assert quotas.overrides == {"big": Quota(max_size=50 * 1024 * GIB, max_objects=-1)}


def test_load_quotas_without_region_refuses_limits_of_several_regions():
    identity = FakeIdentity(defaults("here") + [registered(MAX_SIZE, 1, "other")], [])
    with pytest.raises(LookupError, match=f"want one registered limit {MAX_SIZE} "):
        keystone.load_quotas(identity, SERVICE, region=None)


def test_load_quotas_needs_both_registered_limits():
    identity = FakeIdentity(defaults()[:1], [])
    with pytest.raises(
        LookupError,
        match=f"want one registered limit {MAX_OBJECTS} for service svc, found 0$",
    ):
        keystone.load_quotas(identity, SERVICE, region=None)
    with pytest.raises(LookupError, match="for service svc in region here, found 0"):
        keystone.load_quotas(identity, SERVICE, region="here")


def test_load_quotas_warns_about_hierarchical_model(caplog):
    identity = FakeIdentity(defaults(), [])
    keystone.load_quotas(identity, SERVICE, region=None)
    assert caplog.records == []

    identity.model = "strict_two_level"
    keystone.load_quotas(identity, SERVICE, region=None)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.WARNING,
            (
                "Keystone limits: enforcement model is strict_two_level, but each "
                "project's RGW user gets its own limit, not a share of its parent's"
            ),
        )
    ]


def test_load_quotas_refuses_truncated_list():
    identity = FakeIdentity(defaults(), [])
    identity.truncated = True
    with pytest.raises(RuntimeError, match="truncated the registered_limits list"):
        keystone.load_quotas(identity, SERVICE, region=None)


@pytest.mark.parametrize("uid", ["abc$abc", "abc"])
def test_project_id(uid):
    assert keystone.project_id(uid) == "abc"


@pytest.mark.parametrize("uid", ["abc$def", "abc$", "$abc"])
def test_project_id_refuses_other_uids(uid):
    with pytest.raises(ValueError, match="neither <project> nor"):
        keystone.project_id(uid)


def test_check_scope():
    keystone.check_scope(system_scoped=True)
    with pytest.raises(PermissionError, match="system scoped"):
        keystone.check_scope(system_scoped=False)  # even with admin


CATALOG = [
    {"id": "keystone_id", "type": "identity", "endpoints": [{"id": "ks"}]},
    {
        "id": "rgw_id",
        "type": "object-store",
        "endpoints": [
            {"id": "rgw_public", "region_id": "here"},
            {"id": "rgw_global"},  # region_id isn't required
        ],
    },
]


def test_find_service_by_type():
    assert keystone.find_service(CATALOG, None, "here") == ("rgw_id", "here")
    assert keystone.find_service(CATALOG, None, None) == ("rgw_id", None)
    with pytest.raises(LookupError, match="found 0"):
        keystone.find_service(CATALOG[:1], None, None)
    swift = {"id": "swift_id", "type": "object-store", "endpoints": []}
    with pytest.raises(LookupError, match="found 2: set keystone_endpoint_id"):
        keystone.find_service(CATALOG + [swift], None, None)


def test_find_service_by_endpoint():
    swift = {"id": "swift_id", "type": "object-store", "endpoints": [{"id": "sw"}]}
    catalog = CATALOG + [swift]
    # The endpoint's region, whatever the cloud's region is.
    assert keystone.find_service(catalog, "rgw_public", "other") == ("rgw_id", "here")
    assert keystone.find_service(catalog, "rgw_global", "other") == ("rgw_id", None)
    with pytest.raises(LookupError, match="endpoint nope is not in"):
        keystone.find_service(catalog, "nope", None)
