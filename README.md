# rgw-quota-sync

This tool synchronizes quotas for keystone-based projects in ceph-rgw (Reef).
Quotas are read from [Keystone unified limits](https://docs.openstack.org/keystone/latest/admin/unified-limits.html):
a registered limit is the default, and a project limit overrides it.

It lists all RGW users and, for each one RGW created on a Keystone login
(user info `type: keystone`), looks at its user quota:

| User quota | Action |
| --- | --- |
| Enabled, equal to the project's Keystone limits | unchanged |
| Disabled, or enabled with other values | set to the Keystone limits and enabled |

Users that aren't Keystone users (backup, service users, ...) are never
changed.

How this interacts with RGW's own quota settings:

- An enabled per-user quota replaces the global user quota
  (`radosgw-admin global quota set --quota-scope=user`), so for every user
  this tool sets, its quota is what applies.
- To exempt a project, give it project limits of `-1` for both resources.
- With `rgw_user_default_quota_max_size` / `_max_objects` set, RGW gives new
  users an enabled quota at first login. The tool updates it if it differs.

## Keystone limits

Register both resources on the `object-store` service. Each needs a registered
limit, or the tool refuses to start:

| Resource | Unit | RGW quota field |
| --- | --- | --- |
| `rgw_user_max_size_gib` | GiB (2^30 bytes) | `max_size` |
| `rgw_user_max_objects` | objects | `max_objects` |

`-1` is unlimited. An object limit of `0` blocks all writes, but a size limit of
`0` still allows empty objects. The size is in GiB because Keystone
stores limits as 32-bit integers (at most 2147483647), too small for bytes.

```
openstack registered limit create --service object-store \
  --default-limit 10240 rgw_user_max_size_gib     # 10 TiB
openstack registered limit create --service object-store \
  --default-limit -1 rgw_user_max_objects
openstack limit create --service object-store --project <project> \
  --resource-limit -1 rgw_user_max_size_gib       # unlimited; objects stay the default
```

A project limit for one resource leaves the other at its default. Domain limits
are ignored, and so is a hierarchical (`strict_two_level`) enforcement model:
each project gets its own limit or the default.

If a cloud region is set (`region_name` in `clouds.yaml`, or `OS_REGION_NAME`),
only that region's limits are read, as oslo.limit does, so create them with
`--region`. Otherwise the tool reads limits of every region, and refuses to
start if a project or registered limit exists for more than one.

## Credentials

### Keystone

The tool authenticates like the `openstack` CLI: a `clouds.yaml` entry named by
`OS_CLOUD`, or `OS_*` variables. Keystone only shows a token the limits of
all projects if it is system scoped or has the `admin` or `service` role.
Otherwise it silently leaves out other projects, so the tool refuses to run.
Reading needs no more than the reader role on the system:

```
openstack role add --user quota-sync --user-domain Default --system all reader
```

```yaml
# clouds.yaml
clouds:
  quota-sync:
    auth:
      auth_url: https://keystone.example.org:5000/v3
      username: quota-sync
      password: ...
      user_domain_name: Default
      system_scope: all
    region_name: RegionOne  # optional, see above
```

### RGW

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
```

The tool refuses to start on an unknown key. Older versions read quotas from
`max_size`, `max_objects` and `project_quotas`; move those into Keystone limits.

The tool finds a user's project from its RGW uid. RGW names the user it creates
for a Keystone project `<project_id>$<project_id>` (RGW's `<tenant>$<user>`).
Users created before `rgw_keystone_implicit_tenants` was set are just `<project_id>`.
For example, RGW uids `0123…cdef$0123…cdef` and `0123…cdef` both use the
`0123…cdef` project's limits. RGW keeps using such a legacy user after
`rgw_keystone_implicit_tenants` is enabled, so the tool syncs both kinds.

Keystone users that don't fit either form are inconsistent, and so are users
whose project is gone or projects with more than one user:

| Keystone user | Action |
| --- | --- |
| uid `<a>$<b>` with `a` ≠ `b` | ERROR, not changed, counts as without quota |
| Project deleted from Keystone (which deletes its limits) | WARNING, not changed; the user and its buckets stay until you remove them |
| Its project has more than one RGW user | WARNING after the run; each user can fill the whole quota |

A project gets a user per API (`<id>$<id>` for one, `<id>` for the other) when
`rgw_keystone_implicit_tenants` is `s3` or `swift`, or when a legacy user was
copied rather than renamed.

## Usage

```
OS_CLOUD=quota-sync rgw-quota-sync --config config.yaml [--apply] [--log-level DEBUG|INFO|WARNING|ERROR]
```

Dry run unless `--apply`. All limits are read from Keystone before any RGW user
is changed, so a Keystone error stops the run with nothing written. Logs in oslo.log's format. At the default level, INFO,
every user is logged except Keystone users whose quota is already right:

```
... INFO rgw_quota_sync.cli [-] Keystone limits: default max_size=... max_objects=..., overrides for <n> projects
... WARNING rgw_quota_sync.cli [-] <uid>: quota would change from disabled max_size=-1 max_objects=-1 to enabled max_size=... max_objects=...
... INFO rgw_quota_sync.cli [-] <uid>: quota changed from enabled max_size=... max_objects=... to enabled max_size=... max_objects=...
... ERROR rgw_quota_sync.cli [-] <uid>: quota update failed: PUT 403 AccessDenied
... ERROR rgw_quota_sync.cli [-] <uid>: reading user failed: GET 403 AccessDenied
... INFO rgw_quota_sync.cli [-] <uid>: user no longer exists
... INFO rgw_quota_sync.cli [-] <uid>: not a Keystone user (type rgw), skipped
... WARNING rgw_quota_sync.cli [-] <uid>: project <project_id> is not in Keystone, skipped
... ERROR rgw_quota_sync.cli [-] <uid>: uid is neither <project> nor <project>$<project>
... WARNING rgw_quota_sync.cli [-] project <project_id> has 2 RGW users, each with the full quota: <uid>, <uid>
```

`--log-level DEBUG` also logs
`<uid>: quota unchanged: enabled max_size=... max_objects=...`.

The last line is either `<n> Keystone users without their configured quota`
(WARNING, exit `1`) or `all Keystone users have their configured quota`, where
"configured" means set in Keystone. Users
that a dry run would change, that couldn't be read, or whose uid doesn't fit,
count, so a dry run also works as a check.

Every enabled quota that differs from Keystone is overwritten, including one
set by hand with `radosgw-admin`. Before the first `--apply`, read a dry run's
`would change` lines and create a project limit for any quota you mean to keep.

## Container

CI pushes `ghcr.io/mike-personal-org/rgw-quota-sync` for `main` (tag `main`),
version tags (`v1.2.3` → `1.2.3`) and every commit (`sha-<short sha>`). It runs
as uid 65534, so pass your own uid to read a `chmod 600` config and `clouds.yaml`:

```
docker run --rm --user "$(id -u)" -e OS_CLOUD=quota-sync \
  -v "$PWD/config.yaml:/config.yaml:ro" \
  -v "$PWD/clouds.yaml:/etc/openstack/clouds.yaml:ro" \
  ghcr.io/mike-personal-org/rgw-quota-sync:main --config /config.yaml
```

## API usage

Keystone: `GET /v3/registered_limits` and `GET /v3/limits`, filtered by
`service_id`, `resource_name` and (if set) `region_id`, and `GET /v3/projects`,
through openstacksdk's identity adapter. The `object-store` service ID comes
from the token's catalog. Keystone can't page these lists. If it marks one
`truncated` (cut short by a `list_limit` option), the tool refuses to run
rather than miss a limit or a project.

RGW: requests follow the [Reef Admin Ops docs](https://docs.ceph.com/en/reef/radosgw/adminops/)
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
and [gophercloud](https://github.com/gophercloud/gophercloud), both maintained upstream.
The Python tool now reads Keystone unified limits, so the Go version should
match its resource names, units and checks. Handoff notes are on the
[`go-rewrite`](https://github.com/mike-personal-org/rgw-quota-sync/tree/go-rewrite/go) branch.
