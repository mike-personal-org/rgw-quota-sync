"""rgw-quota-sync: give every Keystone project's RGW user a user quota."""
import argparse
import logging
import os
import sys
import time
from collections import Counter
from urllib.parse import quote, urlparse

import openstack
from rgwadmin import RGWAdmin
from rgwadmin.exceptions import NoSuchUser

from .quota import (
    ConfigError,
    Quota,
    decide,
    describe,
    format_quota,
    load_config,
)

log = logging.getLogger("rgw-quota-sync")

RGW_TIMEOUT = 30  # seconds per Admin Ops request


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="rgw-quota-sync",
        description="Make sure every Keystone project's Ceph RGW user has a user quota. "
        "Dry run unless --apply is given.",
        epilog="RGW admin keys are read from RGW_ACCESS_KEY and RGW_SECRET_KEY.",
    )
    p.add_argument("--config", required=True, metavar="PATH", help="YAML config file")
    p.add_argument(
        "--os-cloud",
        default=os.environ.get("OS_CLOUD"),
        metavar="NAME",
        help="cloud in clouds.yaml (default: $OS_CLOUD; without either, OS_* env vars are used)",
    )
    p.add_argument("--apply", action="store_true", help="write quota changes (default: dry run)")
    p.add_argument("--domain", metavar="NAME_OR_ID", help="only projects in this Keystone domain")
    p.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help="keep running and repeat every SECONDS (default: run once and exit)",
    )
    args = p.parse_args(argv)
    if args.interval is not None and args.interval < 1:
        p.error("--interval must be at least 1")
    return args


def connect_rgw(cfg):
    access_key = os.environ.get("RGW_ACCESS_KEY")
    secret_key = os.environ.get("RGW_SECRET_KEY")
    if not access_key or not secret_key:
        raise ConfigError("RGW_ACCESS_KEY and RGW_SECRET_KEY must be set")
    url = urlparse(cfg.endpoint)
    return RGWAdmin(
        access_key=access_key,
        secret_key=secret_key,
        server=url.netloc,
        secure=url.scheme == "https",
        verify=cfg.verify is not False,
        ca_bundle=cfg.verify if isinstance(cfg.verify, str) else None,
        timeout=RGW_TIMEOUT,
    )


def list_projects(conn, domain):
    query = {}
    if domain:
        query["domain_id"] = conn.identity.find_domain(domain, ignore_missing=False).id
    return sorted(conn.identity.projects(**query), key=lambda p: (p.name or "", p.id))


def sync(cfg, conn, rgw, apply, domain=None):
    """Run one pass. Returns a Counter of actions; "error" counts failures."""
    counts = Counter()
    try:
        projects = list_projects(conn, domain)
    except Exception as e:
        log.error("listing Keystone projects failed: %s", e)
        counts["error"] += 1
        return counts

    project_ids = {p.id for p in projects}
    for project_id in sorted(set(cfg.overrides) - project_ids):
        where = f" in domain {domain}" if domain else ""
        log.warning("override %s is not a Keystone project%s; ignoring it", project_id, where)

    for project in projects:
        # rgwadmin puts uid into the query string as is; quote it so "$" (and
        # anything else a custom uid_format produces) arrives intact.
        uid = quote(cfg.uid(project.id), safe="")
        is_override = project.id in cfg.overrides
        desired = cfg.overrides.get(project.id, cfg.default)
        prefix = f"{project.id}  {project.name}"

        try:
            current = Quota.from_rgw(rgw.get_user_quota(uid=uid))
        except NoSuchUser:
            current = None
        except Exception as e:
            log.error("%s: reading quota for %s failed: %s", project.id, cfg.uid(project.id), e)
            print(f"{prefix}  error: read failed", flush=True)
            counts["error"] += 1
            continue

        action, new = decide(current, desired, is_override)
        if new is not None and apply:
            try:
                rgw.set_user_quota(
                    uid=uid,
                    quota_type="user",
                    # Config sizes are whole KiB; -1 (unlimited) stays negative.
                    max_size_kb=new.max_size // 1024 if new.max_size >= 0 else -1,
                    max_objects=new.max_objects,
                    enabled=True,
                )
            except Exception as e:
                log.error("%s: setting quota for %s failed: %s", project.id, cfg.uid(project.id), e)
                print(f"{prefix}  error: set failed  {format_quota(current)}", flush=True)
                counts["error"] += 1
                continue

        after = format_quota(new if new is not None else current)
        print(f"{prefix}  {describe(action, current)}  {format_quota(current)} -> {after}", flush=True)
        counts[action] += 1
    return counts


def run_once(args):
    """Load config, connect and sync once. Returns the exit code."""
    try:
        with open(args.config) as f:
            cfg = load_config(f.read())
        rgw = connect_rgw(cfg)
    except (OSError, ConfigError) as e:
        log.error("%s", e)
        return 2
    try:
        conn = openstack.connect(cloud=args.os_cloud)
    except Exception as e:
        log.error("connecting to Keystone failed: %s", e)
        return 1

    if not args.apply:
        print("dry run: no changes will be written (use --apply)", flush=True)
    counts = sync(cfg, conn, rgw, args.apply, args.domain)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no projects"
    print(f"summary{'' if args.apply else ' (dry run)'}: {summary}", flush=True)
    return 1 if counts["error"] else 0


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # openstacksdk and urllib3 are chatty at INFO.
    logging.getLogger("openstack").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.interval is None:
        return run_once(args)

    # Long-running mode (e.g. a service container): re-read the config each
    # pass so override edits apply without a restart, and keep going on errors.
    while True:
        rc = run_once(args)
        if rc:
            log.warning("pass finished with errors (exit code %d); retrying in %ds", rc, args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
