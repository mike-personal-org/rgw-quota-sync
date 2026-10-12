# rgw-quota-sync

Makes sure every OpenStack Keystone project's Ceph RGW user has a user quota,
using the RGW Admin Ops REST API.

With `rgw_keystone_implicit_tenants` enabled, each project's RGW user is
`<project_id>$<project_id>`. The tool lists Keystone projects, reads each
project user's quota and:

| Situation | Action |
| --- | --- |
| No RGW user yet (nobody from the project has used object storage) | `skip: no RGW user`. Users are never created; RGW creates them on first Keystone login. |
| Project is in `overrides` | Set the override if enabled, max size or max objects differ. Overrides are enforced exactly, up or down. |
| No override; quota disabled, or enabled but unlimited | `default`: set the default and enable it. |
| No override; quota already equals the default | `ok` |
| No override; project has its own enabled quota | `keep: own quota <size>`. Hand-set quotas are never changed. To manage one, put it in `overrides`. |

RGW users that aren't Keystone projects (backup users, service users, ...)
are never touched, because only Keystone projects are looked up.

A per-user quota that is enabled takes precedence over a global user quota
(`radosgw-admin global quota set --quota-scope=user`), so choose `default`
with the global quota in mind.

If `rgw_user_default_quota_max_size` / `_max_objects` are set in the RGW
config, RGW stamps those values onto new users as an enabled quota. This tool
will report such users as `keep: own quota ...` and leave them alone.

## Install

Python 3.9 or newer.

```
pip install .            # or: pip install -e '.[test]' for development
rgw-quota-sync --help
```

## One-time RGW admin user

On a Ceph admin node, create a user that can read and write user quotas:

```
radosgw-admin user create --uid=quota-sync --display-name="quota sync"
radosgw-admin caps add --uid=quota-sync --caps="users=read,write"
```

Copy its S3 access and secret key from the `user create` output (or
`radosgw-admin user info --uid=quota-sync`) into the environment:

```
export RGW_ACCESS_KEY=...
export RGW_SECRET_KEY=...
```

The keys are only read from these variables, never from the config file.

## Keystone credentials

Keystone auth uses openstacksdk's normal lookup: `--os-cloud NAME` or
`OS_CLOUD` selects an entry in `clouds.yaml`. Without either, the `OS_*`
environment variables are used (e.g. an env file with `OS_AUTH_URL`,
`OS_USERNAME`, ...). The account needs permission to list all projects
(normally the `admin` role); `--domain` also needs to look up domains.

## Config

See [`config.example.yaml`](config.example.yaml):

```yaml
rgw:
  endpoint: https://rgw.example.org:8080   # Admin Ops API base URL, no path
  uid_format: "{id}${id}"                  # implicit tenants (the default)
  # verify: /path/to/ca.pem                # true (default), false or a CA bundle
default:
  max_size: 10T      # bytes, or K/M/G/T/P suffix (binary, 1K = 1024); -1 = unlimited
  max_objects: -1    # -1 = unlimited
overrides:           # optional, keyed by Keystone project ID
  "00000000000000000000000000000000": {max_size: 250T}
```

- Sizes must be whole KiB (the Admin Ops API call takes `max-size-kb`).
- Fields missing from an override fall back to `default`.
- Override keys are read as literal text, so unquoted IDs that look like
  numbers (e.g. all digits with a leading zero) are not mangled. Quoting them
  is still clearer.
- Unknown keys are an error, so a typo like `max-size` fails loudly instead of
  being ignored.
- Override IDs that aren't Keystone projects get a warning and are ignored.

## Usage

```
rgw-quota-sync --config config.yaml [--os-cloud NAME] [--domain NAME_OR_ID] [--apply] [--interval SECONDS]
```

- Without `--apply` it is a dry run: it reads everything and prints what it
  would do, but writes nothing.
- `--domain` limits the run to projects in one Keystone domain (name or ID).
- `--interval SECONDS` keeps the process running and repeats the pass every
  `SECONDS`, re-reading the config each time (so override edits take effect
  without a restart). Without it, the tool runs once and exits.

Output is one line per project, then counts per action:

```
dry run: no changes will be written (use --apply)
<id>  <name>  default  off (unlimited/unlimited objects) -> 10T/unlimited objects
<id>  <name>  keep: own quota 50T  50T/unlimited objects -> 50T/unlimited objects
<id>  <name>  override  10T/unlimited objects -> 250T/unlimited objects
<id>  <name>  skip: no RGW user  - -> -
summary (dry run): default=1, keep=1, override=1, skip=1
```

If a single RGW call fails, the error is logged, the project is counted as
`error`, and the run carries on with the next project.

Exit codes for a single run: `0` success, `1` one or more API calls failed,
`2` bad config or missing RGW keys. With `--interval`, a failing pass is
logged and retried on the next interval.

## Deployment

The tool is a single console script that takes all secrets from the
environment, so it can run as a one-shot job (systemd timer / cron, e.g. a
chi-in-a-box hammer with an env file) or as a long-running container with
`--interval`. Mount the config read-only and pass `--config` to it.

## Development

```
pip install -e '.[test]'
pytest
```

The decision logic (`rgw_quota_sync.quota`) has no Keystone or RGW I/O, so
the tests need no network access.
