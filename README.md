# rgw-quota-sync

This tool synchronizes quotas for keystone-based projects in ceph-rgw (Reef).
Quotas are read from a config file: a default, plus optional per-project overrides.

It lists all RGW users and, for each one RGW created on a Keystone login
(user info `type: keystone`), looks at its user quota:

| User quota | Action |
| --- | --- |
| Enabled, equal to the default or the project's override | unchanged |
| Disabled, or enabled with other values | set to the config quota and enabled |

Users that aren't Keystone users (backup, service users, ...) are never
changed.

How this interacts with RGW's own quota settings:

- An enabled per-user quota replaces the global user quota
  (`radosgw-admin global quota set --quota-scope=user`), so for every user
  this tool sets, its quota is what applies.
- To exempt a project, give it an override of `max_size: -1` and
  `max_objects: -1`.
- With `rgw_user_default_quota_max_size` / `_max_objects` set, RGW gives new
  users an enabled quota at first login. The tool updates it if it differs.

## Credentials

`access_key` / `secret_key` in the config, of an RGW user with only these caps:

```
radosgw-admin user create --uid=quota-sync --display-name="quota sync"
radosgw-admin caps add --uid=quota-sync --caps="users=read,write"
```

`users=write` also allows creating, modifying and deleting users, and
`users=read` returns every user's keys, so keep the config file `chmod 600`.
For a custom CA, set `REQUESTS_CA_BUNDLE`.

## Config

```yaml
endpoint: https://rgw.example.org:8080  # Admin Ops API base URL, no path
access_key: ...
secret_key: ...
max_size: 10995116277760  # bytes (10 TiB); -1 = unlimited, 0 blocks all writes
max_objects: -1           # -1 = unlimited, 0 blocks all writes
project_quotas:           # optional, per Keystone project
  "0123456789abcdef0123456789abcdef":  # Keystone project ID, not the RGW uid
    max_size: -1          # unlimited; max_objects stays the default
```

Each key under `project_quotas` is a Keystone project ID, as `openstack project
list` shows it: 32 hex characters, with no dashes and no `$`. Quote it, or YAML
may read an ID of only digits as a number. An override can set `max_size`,
`max_objects` or both. Whatever it leaves out takes the default. The tool refuses
to start on an unknown key.

The tool finds a user's project from its RGW uid. RGW names the user it creates
for a Keystone project `<project_id>$<project_id>` (RGW's `<tenant>$<user>`).
Users created before `rgw_keystone_implicit_tenants` was set are just `<project_id>`.
For example, RGW uids `0123…cdef$0123…cdef` and `0123…cdef` both use the
`0123…cdef` entry above.

## Usage

```
rgw-quota-sync --config config.yaml [--apply] [--log-level DEBUG|INFO|WARNING|ERROR]
```

Dry run unless `--apply`. Logs in oslo.log's format. At the default level, INFO,
every user is logged except Keystone users whose quota is already right:

```
... WARNING rgw_quota_sync.cli [-] <uid>: quota would change from disabled max_size=-1 max_objects=-1 to enabled max_size=... max_objects=...
... INFO rgw_quota_sync.cli [-] <uid>: quota changed from enabled max_size=... max_objects=... to enabled max_size=... max_objects=...
... ERROR rgw_quota_sync.cli [-] <uid>: quota update failed: PUT 403 AccessDenied
... ERROR rgw_quota_sync.cli [-] <uid>: reading user failed: GET 403 AccessDenied
... INFO rgw_quota_sync.cli [-] <uid>: user no longer exists
... INFO rgw_quota_sync.cli [-] <uid>: not a Keystone user (type rgw), skipped
... WARNING rgw_quota_sync.cli [-] project_quotas: no RGW user for project <project_id>
```

The last warning means an override's project ID matches no RGW user: a typo, or
a project that hasn't used object storage yet. `--log-level DEBUG` also logs
`<uid>: quota unchanged: enabled max_size=... max_objects=...`.

The last line is either `<n> Keystone users without their configured quota`
(WARNING, exit `1`) or `all Keystone users have their configured quota`. Users
that a dry run would change, or that couldn't be read, count, so a dry run also
works as a check.

Every enabled quota that differs from the config is overwritten, including one
set by hand with `radosgw-admin`. Before the first `--apply`, read a dry run's
`would change` lines and move any quota you mean to keep into `project_quotas`.

## Container

CI pushes `ghcr.io/mike-personal-org/rgw-quota-sync` for `main` (tag `main`),
version tags (`v1.2.3` → `1.2.3`) and every commit (`sha-<short sha>`). It runs
as uid 65534, so pass your own uid to read a `chmod 600` config:

```
docker run --rm --user "$(id -u)" -v "$PWD/config.yaml:/config.yaml:ro" \
  ghcr.io/mike-personal-org/rgw-quota-sync:main --config /config.yaml
```

## API usage

Requests follow the [Reef Admin Ops docs](https://docs.ceph.com/en/reef/radosgw/adminops/)
(Get User Info, Set User Quota with a JSON body), except listing users, which
the docs don't cover. That uses `GET /admin/user?list`, implemented by
`RGWOp_User_List` in Reef's `rgw_rest_user.cc` and used by the Ceph dashboard.

## Development

```
tox
```

TODO: Propose an upstream Ceph docs update for `GET /admin/user?list`
(`RGWOp_User_List`: `max-entries`, `marker`, response `keys` / `truncated` /
`marker`; needs `users=read`), which exists since Nautilus but isn't in the
Admin Ops docs. Also note that Set User Quota's JSON body must be sent as
`UNSIGNED-PAYLOAD`: Reef answers 501 NotImplemented to a SigV4-signed body
(rgw_rest_s3.cc only allows signed bodies for a fixed list of operations).

TODO: Go rewrite on [go-ceph `rgw/admin`](https://pkg.go.dev/github.com/ceph/go-ceph/rgw/admin)
and [gophercloud](https://github.com/gophercloud/gophercloud), both maintained upstream,
with quotas from Keystone unified limits. Handoff notes are on the
[`go-rewrite`](https://github.com/mike-personal-org/rgw-quota-sync/tree/go-rewrite/go) branch.
