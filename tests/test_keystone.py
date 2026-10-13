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

    def __init__(self, registered_limits, limits):
        self.collections = {
            "registered_limits": registered_limits,
            "limits": limits,
        }
        self.truncated = False

    def get(self, url, *, params):
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
    assert quotas.projects == {
        "big": Quota(max_size=50 * 1024 * GIB, max_objects=-1),
        "unlimited": Quota(max_size=-1, max_objects=-1),  # -1 isn't multiplied
        "read_only": Quota(max_size=0, max_objects=0),
        # A resource without a project limit keeps the default.
        "objects_only": Quota(max_size=10 * 1024 * GIB, max_objects=1000),
    }
    # The uid's part before "$" is the project ID; a uid without "$" is all of it.
    assert quotas.quota_for("big$big") == quotas.projects["big"]
    assert quotas.quota_for("big") == quotas.projects["big"]
    assert quotas.quota_for("new$new") == quotas.default


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
    assert quotas.projects == {"big": Quota(max_size=50 * 1024 * GIB, max_objects=-1)}


def test_load_quotas_without_region_refuses_limits_of_several_regions():
    identity = FakeIdentity(defaults("here") + [registered(MAX_SIZE, 1, "other")], [])
    with pytest.raises(LookupError, match=f"want one registered limit {MAX_SIZE} "):
        keystone.load_quotas(identity, SERVICE, region=None)

    identity = FakeIdentity(
        defaults(),
        [
            limit(MAX_SIZE, 1, project="big", region="here"),
            limit(MAX_SIZE, 2, project="big", region="other"),
        ],
    )
    with pytest.raises(LookupError, match=f"project big has several {MAX_SIZE} "):
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


def test_load_quotas_refuses_truncated_list():
    identity = FakeIdentity(defaults(), [])
    identity.truncated = True
    with pytest.raises(RuntimeError, match="truncated the registered_limits list"):
        keystone.load_quotas(identity, SERVICE, region=None)


@pytest.mark.parametrize(
    "system_scoped, roles",
    [(True, ["reader"]), (False, ["admin", "member", "reader"]), (False, ["service"])],
)
def test_check_scope_allows_tokens_that_see_all_projects(system_scoped, roles):
    keystone.check_scope(system_scoped, roles)


def test_check_scope_refuses_tokens_that_see_some_projects():
    with pytest.raises(PermissionError, match="system scoped"):
        keystone.check_scope(False, ["member", "reader"])


def test_service_id():
    catalog = [
        {"id": "keystone_id", "type": "identity", "endpoints": []},
        {"id": "rgw_id", "type": "object-store", "endpoints": []},
    ]
    assert keystone.service_id(catalog) == "rgw_id"
    with pytest.raises(LookupError, match="found 0"):
        keystone.service_id(catalog[:1])
    with pytest.raises(LookupError, match="found 2"):
        keystone.service_id(catalog + catalog[1:])
