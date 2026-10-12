"""Main-loop tests with fake Keystone and RGW clients (no network)."""
from types import SimpleNamespace

import pytest
from rgwadmin.exceptions import AccessDenied, NoSuchUser

from rgw_quota_sync import cli

T = 1024**4
UNLIMITED = {"enabled": False, "max_size": -1, "max_size_kb": 0, "max_objects": -1}

CONFIG = """
rgw: {endpoint: "https://rgw.example.org:8080"}
default: {max_size: 10T, max_objects: -1}
overrides:
  "p-over": {max_size: 1K}
  "not-a-project": {max_size: 1G}
"""


class FakeRGW:
    def __init__(self, quotas, fail_get=(), fail_set=()):
        self.quotas = quotas  # uid -> quota dict
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.sets = []

    def get_user_quota(self, uid):
        if uid in self.fail_get:
            raise AccessDenied("AccessDenied")
        if uid not in self.quotas:
            raise NoSuchUser("NoSuchUser")
        return self.quotas[uid]

    def set_user_quota(self, uid, quota_type, max_size_kb, max_objects, enabled):
        if uid in self.fail_set:
            raise AccessDenied("AccessDenied")
        self.sets.append((uid, quota_type, max_size_kb, max_objects, enabled))


class FakeIdentity:
    def __init__(self, projects):
        self._projects = projects
        self.queries = []

    def projects(self, **query):
        self.queries.append(query)
        return iter(self._projects)

    def find_domain(self, name_or_id, ignore_missing=True):
        return SimpleNamespace(id="dom-id")


def project(pid, name):
    return SimpleNamespace(id=pid, name=name)


PROJECTS = [
    project("p-new", "fresh"),
    project("p-own", "handset"),
    project("p-over", "tiny"),
    project("p-none", "never-logged-in"),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RGW_ACCESS_KEY", "access")
    monkeypatch.setenv("RGW_SECRET_KEY", "secret")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    rgw = FakeRGW({
        "p-new%24p-new": UNLIMITED,
        "p-own%24p-own": {"enabled": True, "max_size": 50 * T, "max_objects": -1},
        "p-over%24p-over": {"enabled": True, "max_size": 10 * T, "max_objects": -1},
    })
    identity = FakeIdentity(PROJECTS)
    made = {}

    def fake_rgwadmin(**kwargs):
        made.update(kwargs)
        return rgw

    monkeypatch.setattr(cli, "RGWAdmin", fake_rgwadmin)
    monkeypatch.setattr(cli.openstack, "connect", lambda cloud=None: SimpleNamespace(identity=identity))
    return SimpleNamespace(config=str(cfg), rgw=rgw, identity=identity, rgwadmin_kwargs=made)


def test_dry_run_writes_nothing(env, capsys, caplog):
    assert cli.main(["--config", env.config]) == 0
    assert env.rgw.sets == []
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "p-new  fresh  default  off (unlimited/unlimited objects) -> 10T/unlimited objects" in out
    assert "p-own  handset  keep: own quota 50T  50T/unlimited objects -> 50T/unlimited objects" in out
    assert "p-over  tiny  override  10T/unlimited objects -> 1K/unlimited objects" in out
    assert "p-none  never-logged-in  skip: no RGW user  - -> -" in out
    assert "summary (dry run): default=1, keep=1, override=1, skip=1" in out
    assert "not-a-project is not a Keystone project" in caplog.text
    assert env.rgwadmin_kwargs["server"] == "rgw.example.org:8080"
    assert env.rgwadmin_kwargs["secure"] is True


def test_apply_sets_default_and_override(env, capsys):
    assert cli.main(["--config", env.config, "--apply"]) == 0
    assert sorted(env.rgw.sets) == [
        ("p-new%24p-new", "user", 10 * T // 1024, -1, True),
        ("p-over%24p-over", "user", 1, -1, True),
    ]
    assert "summary: default=1, keep=1, override=1, skip=1" in capsys.readouterr().out


def test_errors_are_logged_and_counted(env, capsys, caplog):
    env.rgw.fail_get = ("p-own%24p-own",)
    env.rgw.fail_set = ("p-new%24p-new",)
    assert cli.main(["--config", env.config, "--apply"]) == 1
    # The other projects were still processed.
    assert env.rgw.sets == [("p-over%24p-over", "user", 1, -1, True)]
    out = capsys.readouterr().out
    assert "summary: error=2, override=1, skip=1" in out
    assert "reading quota for p-own$p-own failed" in caplog.text
    assert "setting quota for p-new$p-new failed" in caplog.text


def test_domain_filter(env):
    assert cli.main(["--config", env.config, "--domain", "Default"]) == 0
    assert env.identity.queries == [{"domain_id": "dom-id"}]


def test_missing_rgw_keys(env, monkeypatch, caplog):
    monkeypatch.delenv("RGW_SECRET_KEY")
    assert cli.main(["--config", env.config]) == 2
    assert "RGW_ACCESS_KEY and RGW_SECRET_KEY" in caplog.text


def test_bad_config(env, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("rgw: {}\n")
    assert cli.main(["--config", str(bad)]) == 2


def test_interval_repeats(env, monkeypatch):
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--config", env.config, "--interval", "60"])
    assert calls == [60, 60]
    assert len(env.identity.queries) == 2
