"""Ensure every RGW user has a configured quota.

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


@dataclass
class Config:
    endpoint: str  # Admin Ops API base URL, e.g. https://rgw.example.org:8080
    access_key: str
    secret_key: str = field(repr=False)  # keep it out of logs
    max_size: int  # bytes; -1 = unlimited, 0 blocks all writes
    max_objects: int  # -1 = unlimited, 0 blocks all writes
    # Keystone project ID (not the RGW uid) -> fields to override,
    # e.g. {"0123...cdef": {"max_size": -1}}
    project_quotas: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> Self:
        with open(path) as f:
            data = yaml.safe_load(f)
        # A missing or unknown key raises TypeError.
        return cls(**data)

    def quota_for(self, rgw_uid: str) -> dict[str, int]:
        """Return the default quota, updated with the user's project override.

        rgw_uid is "<project_id>$<project_id>" (RGW's "<tenant>$<user>"), or
        just "<project_id>" for users created before
        rgw_keystone_implicit_tenants.
        """
        project_id = rgw_uid.split("$")[0]
        override = self.project_quotas.get(project_id, {})
        return {
            "max_size": override.get("max_size", self.max_size),
            "max_objects": override.get("max_objects", self.max_objects),
        }


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
        log.debug("%s: not a Keystone user, skipped", uid)
        return True

    configured = config.quota_for(uid)
    current = user["user_quota"]
    if (
        current["enabled"]
        and current["max_size"] == configured["max_size"]
        and current["max_objects"] == configured["max_objects"]
    ):
        log.info(
            "%s: quota enabled, unchanged: max_size=%s max_objects=%s",
            uid,
            current["max_size"],
            current["max_objects"],
        )
        return True

    if not apply:
        log.warning(
            "%s: quota %s, would set max_size=%s max_objects=%s",
            uid,
            "enabled" if current["enabled"] else "disabled",
            configured["max_size"],
            configured["max_objects"],
        )
        return False

    new_quota = {
        "enabled": True,
        # The request replaces the whole quota, so keep this setting as it was.
        "check_on_raw": current["check_on_raw"],
        "max_size": configured["max_size"],
        "max_objects": configured["max_objects"],
    }
    try:
        rgw.set_quota(uid, new_quota)
    except requests.RequestException as e:
        log.error("%s: quota update failed: %s", uid, e)
        return False
    log.info(
        "%s: quota changed: max_size=%s max_objects=%s",
        uid,
        configured["max_size"],
        configured["max_objects"],
    )
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes (default: dry run)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    config = Config.load(args.config)
    rgw = RGW(config.endpoint, config.access_key, config.secret_key)

    if not args.apply:
        log.info("dry run: nothing will be written (use --apply)")

    without_quota = 0
    for uid in rgw.list_users():
        if not sync_user(rgw, uid, config, args.apply):
            without_quota += 1

    if without_quota:
        log.warning("%d Keystone users without their configured quota", without_quota)
        sys.exit(1)
    log.info("all Keystone users have their configured quota")


if __name__ == "__main__":
    main()
