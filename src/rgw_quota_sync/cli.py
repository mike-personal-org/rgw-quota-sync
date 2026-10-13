"""Ensure every RGW user created by Keystone has its quota from Keystone.

Updates quotas that differ from the project's Keystone unified limits.
"""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from typing import Self

import requests
import yaml

from . import keystone
from .keystone import Quota, Quotas, project_id
from .rgw import RGW, NoSuchUser

log = logging.getLogger(__name__)

# oslo.log's default format for messages without a request context.
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(process)d %(levelname)s %(name)s [-] %(message)s"
)
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# The keys allowed in the config file.
CONFIG_KEYS = ("endpoint", "access_key", "secret_key")
# Keys of older versions, which read quotas from the config file.
OLD_QUOTA_KEYS = ("max_size", "max_objects", "project_quotas")


@dataclass
class Config:
    endpoint: str  # Admin Ops API base URL, e.g. https://rgw.example.org:8080
    access_key: str
    secret_key: str = field(repr=False)  # keep it out of logs

    @classmethod
    def load(cls, path: str) -> Self:
        """Read the config file. Raises an error on an unknown key."""
        with open(path) as f:
            data = yaml.safe_load(f)

        for key in data:
            if key in OLD_QUOTA_KEYS:
                raise ValueError(
                    f"config key {key!r}: quotas now come from Keystone "
                    "unified limits, see the README"
                )
            if key not in CONFIG_KEYS:
                raise ValueError(f"unknown config key {key!r}")

        return cls(
            endpoint=data["endpoint"],
            access_key=data["access_key"],
            secret_key=data["secret_key"],
        )


def sync_user(
    rgw: RGW,
    uid: str,
    quotas: Quotas,
    apply: bool,
    keystone_users: dict[str, list[str]],
) -> bool:
    """Make sure one RGW user created by Keystone has its quota from Keystone.

    Logs what happened, and adds a Keystone user's uid to keystone_users under
    its project ID. Returns False if the user is, or may be, left without its
    quota.
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

    try:
        project = project_id(uid)
    except ValueError as e:
        log.error("%s: %s", uid, e)
        return False
    # Keystone deleted the project's limits with it, so there is no quota to
    # give. The user and its buckets stay until an operator removes them.
    if project not in quotas.project_ids:
        log.warning("%s: project %s is not in Keystone, skipped", uid, project)
        return True
    keystone_users.setdefault(project, []).append(uid)

    rgw_quota = user["user_quota"]
    current = Quota(rgw_quota["max_size"], rgw_quota["max_objects"])
    wanted = quotas.quota_for(project)
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
    # Read all limits before changing anything, so a Keystone error stops the run.
    quotas = keystone.load_from_cloud()
    log.info(
        "Keystone limits: default %s, overrides for %d projects",
        quotas.default,
        len(quotas.overrides),
    )

    if not args.apply:
        log.info("dry run: nothing will be written (use --apply)")

    without_quota = 0
    keystone_users: dict[str, list[str]] = {}
    for uid in rgw.list_users():
        if not sync_user(rgw, uid, quotas, args.apply, keystone_users):
            without_quota += 1

    # With rgw_keystone_implicit_tenants set to s3 or swift, a project has a
    # user for each API, and each can fill the project's whole quota.
    for project, uids in keystone_users.items():
        if len(uids) > 1:
            log.warning(
                "project %s has %d RGW users, each with the full quota: %s",
                project,
                len(uids),
                ", ".join(uids),
            )

    if without_quota:
        log.warning("%d Keystone users without their configured quota", without_quota)
        sys.exit(1)
    log.info("all Keystone users have their configured quota")


if __name__ == "__main__":
    main()
