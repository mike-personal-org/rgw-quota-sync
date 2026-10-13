import logging
from types import SimpleNamespace

import pytest
import requests

from rgw_quota_sync import cli, keystone
from rgw_quota_sync.keystone import Quota, Quotas
from rgw_quota_sync.rgw import NoSuchUser

TIB = 1024**4
CONFIG = """
endpoint: https://rgw:8080
access_key: access
secret_key: secret
"""
# As read from Keystone. Project IDs match the uids in env below.
QUOTAS = Quotas(
    default=Quota(max_size=10 * TIB, max_objects=-1),
    overrides={
        "big": Quota(max_size=50 * TIB, max_objects=-1),
        "unlimited": Quota(max_size=-1, max_objects=-1),
        "untenanted": Quota(max_size=20 * TIB, max_objects=-1),
        "objects_only": Quota(max_size=10 * TIB, max_objects=1000),
        "nobody": Quota(max_size=-1, max_objects=-1),  # no RGW user
    },
    # Not "gone": deleted from Keystone, but its RGW user is left.
    project_ids={
        "new",
        "big",
        "unlimited",
        "untenanted",
        "objects_only",
        "split",
        "nobody",
    },
)


def test_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG)
    config = cli.Config.load(str(path))
    assert config == cli.Config("https://rgw:8080", "access", "secret")
    assert "secret" not in repr(config)


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG + "endpiont: https://rgw:8080\n")
    with pytest.raises(ValueError, match="unknown config key 'endpiont'"):
        cli.Config.load(str(path))


@pytest.mark.parametrize("key", ["max_size", "max_objects", "project_quotas"])
def test_config_rejects_quota_keys(tmp_path, key):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG + f"{key}: 1\n")
    with pytest.raises(ValueError, match="quotas now come from Keystone"):
        cli.Config.load(str(path))


def user(user_type, enabled, max_size=-1, max_objects=-1, check_on_raw=False):
    quota = {
        "enabled": enabled,
        "check_on_raw": check_on_raw,
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
            "big$big": user("keystone", enabled=True, max_size=50 * TIB),
            "unlimited$unlimited": user("keystone", enabled=True),
            "untenanted": user("keystone", enabled=True, max_size=20 * TIB),
            "objects_only$objects_only": user(
                "keystone", enabled=True, max_size=10 * TIB, check_on_raw=True
            ),
            "backup": user("rgw", enabled=False),
            "gone$gone": user("keystone", enabled=False),
            "one$other": user("keystone", enabled=False),
            # rgw_keystone_implicit_tenants = s3 or swift
            "split$split": user("keystone", enabled=True, max_size=10 * TIB),
            "split": user("keystone", enabled=True, max_size=10 * TIB),
        }
    )
    env = SimpleNamespace(config=str(config), rgw=rgw, rgw_args=None)

    def fake_rgw(*args):
        env.rgw_args = args
        return rgw

    monkeypatch.setattr(cli, "RGW", fake_rgw)
    monkeypatch.setattr(keystone, "load_from_cloud", lambda: QUOTAS)
    return env


def logged(caplog):
    return [f"{record.levelname} {record.getMessage()}" for record in caplog.records]


def test_dry_run_writes_nothing_and_reports_mismatched_quotas(env, caplog):
    with pytest.raises(SystemExit) as exit:
        cli.main(["--config", env.config])
    assert exit.value.code == 1
    assert env.rgw_args == ("https://rgw:8080", "access", "secret")
    assert env.rgw.sets == []
    assert logged(caplog) == [
        (
            f"INFO Keystone limits: default max_size={10 * TIB} max_objects=-1, "
            "overrides for 5 projects"
        ),
        "INFO dry run: nothing will be written (use --apply)",
        (
            "WARNING new$new: quota would change from disabled max_size=-1 "
            f"max_objects=-1 to enabled max_size={10 * TIB} max_objects=-1"
        ),
        (
            "WARNING objects_only$objects_only: quota would change from enabled "
            f"max_size={10 * TIB} max_objects=-1 to enabled max_size={10 * TIB} "
            "max_objects=1000"
        ),
        "INFO backup: not a Keystone user (type rgw), skipped",
        "WARNING gone$gone: project gone is not in Keystone, skipped",
        "ERROR one$other: uid is neither <project> nor <project>$<project>",
        "INFO deleted$deleted: user no longer exists",
        (
            "WARNING project split has 2 RGW users, each with the full quota: "
            "split$split, split"
        ),
        "WARNING 3 Keystone users without their configured quota",
    ]


def test_apply_sets_mismatched_keystone_quotas(env, caplog):
    with pytest.raises(SystemExit):  # one$other can't be synced
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
        ),
        (
            "objects_only$objects_only",
            {
                "enabled": True,
                "check_on_raw": True,  # kept as it was
                "max_size": 10 * TIB,
                "max_objects": 1000,
            },
        ),
    ]
    assert (
        "INFO new$new: quota changed from disabled max_size=-1 max_objects=-1 "
        f"to enabled max_size={10 * TIB} max_objects=-1"
    ) in logged(caplog)
    assert logged(caplog)[-1] == (
        "WARNING 1 Keystone users without their configured quota"
    )


def test_failures_are_logged_and_counted(env, caplog):
    env.rgw.fail_get = {"big$big"}
    env.rgw.fail_set = {"new$new"}
    with pytest.raises(SystemExit) as exit:
        cli.main(["--config", env.config, "--apply"])
    assert exit.value.code == 1
    assert logged(caplog)[1:] == [
        "ERROR new$new: quota update failed: PUT 403 AccessDenied",
        "ERROR big$big: reading user failed: GET 403 AccessDenied",
        (
            "INFO objects_only$objects_only: quota changed from enabled "
            f"max_size={10 * TIB} max_objects=-1 to enabled max_size={10 * TIB} "
            "max_objects=1000"
        ),
        "INFO backup: not a Keystone user (type rgw), skipped",
        "WARNING gone$gone: project gone is not in Keystone, skipped",
        "ERROR one$other: uid is neither <project> nor <project>$<project>",
        "INFO deleted$deleted: user no longer exists",
        (
            "WARNING project split has 2 RGW users, each with the full quota: "
            "split$split, split"
        ),
        "WARNING 3 Keystone users without their configured quota",
    ]
