"""Ensure every RGW user created by Keystone has its configured quota.

Updates quotas that differ from the default or per-project override.
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Self

import requests
import yaml

from .rgw import RGW, NoSuchUser

log = logging.getLogger(__name__)

# oslo.log's default format for messages without a request context.
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(process)d %(levelname)s %(name)s [-] %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# The keys allowed in the config file, and in each of its project_quotas.
CONFIG_KEYS = (
    "endpoint",
    "access_key",
    "secret_key",
    "max_size",
    "max_objects",
    "project_quotas",
)
QUOTA_KEYS = ("max_size", "max_objects")


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
    def from_config(cls, max_size: object, max_objects: object) -> Self:
        """Return the quota from config file values, which must be integers."""
        for value in (max_size, max_objects):
            if not isinstance(value, int):
                raise TypeError(f"quota values must be integers, got {value!r}")
        return cls(max_size, max_objects)

    def __str__(self) -> str:
        return f"max_size={self.max_size} max_objects={self.max_objects}"


@dataclass
class Config:
    endpoint: str  # Admin Ops API base URL, e.g. https://rgw.example.org:8080
    access_key: str
    secret_key: str = field(repr=False)  # keep it out of logs
    default_quota: Quota
    project_quotas: dict[str, Quota]  # Keystone project ID (not the RGW uid) -> quota

    @classmethod
    def load(cls, path: str) -> Self:
        """Read the config file. Raises an error on an unknown key or bad value."""
        with open(path) as f:
            data = yaml.safe_load(f)

        for key in data:
            if key not in CONFIG_KEYS:
                raise ValueError(f"unknown config key {key!r}")
        default_quota = Quota.from_config(data["max_size"], data["max_objects"])

        project_quotas = {}
        for project, override in data.get("project_quotas", {}).items():
            for key in override:
                if key not in QUOTA_KEYS:
                    raise ValueError(f"project_quotas.{project}: unknown key {key!r}")
            # A field the override leaves out keeps the default.
            project_quotas[project] = Quota.from_config(
                override.get("max_size", default_quota.max_size),
                override.get("max_objects", default_quota.max_objects),
            )

        return cls(
            endpoint=data["endpoint"],
            access_key=data["access_key"],
            secret_key=data["secret_key"],
            default_quota=default_quota,
            project_quotas=project_quotas,
        )

    def quota_for(self, rgw_uid: str) -> Quota:
        """Return the quota for an RGW user: its project's, or the default."""
        return self.project_quotas.get(project_id(rgw_uid), self.default_quota)


def sync_user(rgw: RGW, uid: str, config: Config, apply: bool) -> bool:
    """Make sure one RGW user created by Keystone has its configured quota.

    Logs what happened. Returns False if the user is, or may be, left without
    its configured quota.
    """
    try:
        user = rgw.get_user(uid)
    except NoSuchUser:
        log.info("%s: user no longer exists", uid)  # deleted since it was listed
        return True
    except requests.RequestException as e:
        log.error("%s: reading user failed: %s", uid, e)
        return False

    if user["type"] != "keystone":
        log.info("%s: not a Keystone user (type %s), skipped", uid, user["type"])
        return True

    rgw_quota = user["user_quota"]
    current = Quota(rgw_quota["max_size"], rgw_quota["max_objects"])
    wanted = config.quota_for(uid)
    if rgw_quota["enabled"] and current == wanted:
        log.debug("%s: quota unchanged: enabled %s", uid, current)
        return True

    state = "enabled" if rgw_quota["enabled"] else "disabled"
    if not apply:
        log.warning(
            "%s: quota would change from %s %s to enabled %s",
            uid,
            state,
            current,
            wanted,
        )
        return False

    new_rgw_quota = {
        "enabled": True,
        "max_size": wanted.max_size,
        "max_objects": wanted.max_objects,
        # The request replaces the whole quota, so keep this setting as it was.
        "check_on_raw": rgw_quota["check_on_raw"],
    }
    try:
        rgw.set_quota(uid, new_rgw_quota)
    except requests.RequestException as e:
        log.error("%s: quota update failed: %s", uid, e)
        return False
    log.info("%s: quota changed from %s %s to enabled %s", uid, state, current, wanted)
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes (default: dry run)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="default: INFO",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT
    )
    config = Config.load(args.config)
    rgw = RGW(config.endpoint, config.access_key, config.secret_key)

    if not args.apply:
        log.info("dry run: nothing will be written (use --apply)")

    without_quota = 0
    projects_with_users = set()
    for uid in rgw.list_users():
        projects_with_users.add(project_id(uid))
        if not sync_user(rgw, uid, config, args.apply):
            without_quota += 1

    # Likely a typo in the project ID, or a project with no RGW user yet.
    for project in config.project_quotas:
        if project not in projects_with_users:
            log.warning("project_quotas: no RGW user for project %s", project)

    if without_quota:
        log.warning("%d Keystone users without their configured quota", without_quota)
        sys.exit(1)
    log.info("all Keystone users have their configured quota")


if __name__ == "__main__":
    main()
