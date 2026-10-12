import logging
from types import SimpleNamespace

import pytest
import requests

from rgw_quota_sync import cli
from rgw_quota_sync.rgw import NoSuchUser

TIB = 1024**4
CONFIG = """
endpoint: https://rgw:8080
access_key: access
secret_key: secret
max_size: 10995116277760
max_objects: -1
"""


def test_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = cli.Config.load(str(path))
    assert config.max_size == 10 * TIB
    assert config.max_objects == -1
    assert "secret" not in repr(config)


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG + "max-size: 1\n")
    with pytest.raises(TypeError, match="max-size"):
        cli.Config.load(str(path))


def user(user_type, enabled, max_size=-1, max_objects=-1):
    quota = {
        "enabled": enabled,
        "check_on_raw": False,
        "max_size": max_size,
        "max_size_kb": 0,
        "max_objects": max_objects,
    }
    return {"type": user_type, "user_quota": quota}


class FakeRGW:
    def __init__(self, users):
        self.users = users  # uid -> user info as RGW returns it
        self.listed = list(users) + ["deleted$deleted"]  # removed after listing
        self.fail_get = set()  # uids whose get_user fails
        self.fail_set = set()  # uids whose set_quota fails
        self.sets = []

    def list_users(self):
        yield from self.listed

    def get_user(self, uid):
        if uid in self.fail_get:
            raise requests.HTTPError("GET 403 AccessDenied")
        if uid not in self.users:
            raise NoSuchUser("GET 404 NoSuchUser")
        return self.users[uid]

    def set_quota(self, uid, quota):
        if uid in self.fail_set:
            raise requests.HTTPError("PUT 403 AccessDenied")
        self.sets.append((uid, quota))


@pytest.fixture
def env(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG)

    rgw = FakeRGW(
        {
            "new$new": user("keystone", enabled=False),
            "own$own": user("keystone", enabled=True, max_size=50 * TIB),
            "free$free": user("keystone", enabled=True),
            "backup": user("rgw", enabled=False),
        }
    )
    env = SimpleNamespace(config=str(config), rgw=rgw, rgw_args=None)

    def fake_rgw(*args):
        env.rgw_args = args
        return rgw

    monkeypatch.setattr(cli, "RGW", fake_rgw)
    return env


def logged(caplog):
    return [f"{record.levelname} {record.getMessage()}" for record in caplog.records]


def test_dry_run_writes_nothing_and_reports_missing_quotas(env, caplog):
    with pytest.raises(SystemExit) as exit:
        cli.main(["--config", env.config])
    assert exit.value.code == 1
    assert env.rgw_args == ("https://rgw:8080", "access", "secret")
    assert env.rgw.sets == []
    assert logged(caplog) == [
        "INFO dry run: nothing will be written (use --apply)",
        f"WARNING new$new: quota disabled, would set max_size={10 * TIB} max_objects=-1",
        f"INFO own$own: quota enabled, unchanged: max_size={50 * TIB} max_objects=-1",
        "INFO free$free: quota enabled, unchanged: max_size=-1 max_objects=-1",
        "INFO deleted$deleted: user no longer exists",
        "WARNING 1 Keystone users without an enabled quota",
    ]


def test_apply_sets_only_disabled_keystone_quotas(env, caplog):
    cli.main(["--config", env.config, "--apply"])
    assert env.rgw.sets == [
        (
            "new$new",
            {
                "enabled": True,
                "check_on_raw": False,
                "max_size": 10 * TIB,
                "max_objects": -1,
            },
        )
    ]
    assert f"INFO new$new: quota changed: max_size={10 * TIB} max_objects=-1" in (
        logged(caplog)
    )
    assert logged(caplog)[-1] == "INFO all Keystone users have an enabled quota"


def test_failures_are_logged_and_counted(env, caplog):
    env.rgw.fail_get = {"own$own"}
    env.rgw.fail_set = {"new$new"}
    with pytest.raises(SystemExit) as exit:
        cli.main(["--config", env.config, "--apply"])
    assert exit.value.code == 1
    assert logged(caplog) == [
        "ERROR new$new: quota update failed: PUT 403 AccessDenied",
        "ERROR own$own: reading user failed: GET 403 AccessDenied",
        "INFO free$free: quota enabled, unchanged: max_size=-1 max_objects=-1",
        "INFO deleted$deleted: user no longer exists",
        "WARNING 2 Keystone users without an enabled quota",
    ]
