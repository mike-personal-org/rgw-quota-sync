"""Give every RGW user created by Keystone an enabled quota.

Users whose quota is disabled get the configured one. Enabled quotas,
including enabled-unlimited exemptions, are never touched. Exits 1 if any
Keystone user is left without an enabled quota (in a dry run: would be).
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

    @classmethod
    def load(cls, path: str) -> Self:
        with open(path) as f:
            data = yaml.safe_load(f)
        # A missing or unknown key raises TypeError.
        return cls(**data)


def sync_user(rgw: RGW, uid: str, config: Config, apply: bool) -> bool:
    """Make sure one RGW user created by Keystone has an enabled quota.

    Logs what happened. Returns False if the user is, or may be, left without
    an enabled quota.
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

    quota = user["user_quota"]
    if quota["enabled"]:
        log.info(
            "%s: quota enabled, unchanged: max_size=%s max_objects=%s",
            uid,
            quota["max_size"],
            quota["max_objects"],
        )
        return True

    if not apply:
        log.warning(
            "%s: quota disabled, would set max_size=%s max_objects=%s",
            uid,
            config.max_size,
            config.max_objects,
        )
        return False

    new_quota = {
        "enabled": True,
        # The request replaces the whole quota, so keep this setting as it was.
        "check_on_raw": quota["check_on_raw"],
        "max_size": config.max_size,
        "max_objects": config.max_objects,
    }
    try:
        rgw.set_quota(uid, new_quota)
    except requests.RequestException as e:
        log.error("%s: quota update failed: %s", uid, e)
        return False
    log.info(
        "%s: quota changed: max_size=%s max_objects=%s",
        uid,
        config.max_size,
        config.max_objects,
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
        log.warning("%d Keystone users without an enabled quota", without_quota)
        sys.exit(1)
    log.info("all Keystone users have an enabled quota")


if __name__ == "__main__":
    main()
