# rgw-quota-sync

This tool synchronizes quotas for keystone-based projects in ceph-rgw (Reef).
Currently, quotas are default only, and read from a config file.

It lists all RGW users and, for each one RGW created on a Keystone login
(user info `type: keystone`), looks at its user quota:

| User quota | Action |
| --- | --- |
| Enabled, any value (including unlimited) | unchanged |
| Disabled | set to the config quota and enabled |

Users that aren't Keystone users (backup, service users, ...) are never
changed. Changing `max_size` later does not update users already set.

How this interacts with RGW's own quota settings:

- An enabled per-user quota replaces the global user quota
  (`radosgw-admin global quota set --quota-scope=user`), so for every user
  this tool sets, its quota is what applies.
- To exempt a user, enable an unlimited quota on it; the tool leaves it.
- With `rgw_user_default_quota_max_size` / `_max_objects` set, RGW gives new
  users an enabled quota at first login, so the tool leaves those too.

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
```

## Usage

```
rgw-quota-sync --config config.yaml [--apply]
```

Dry run unless `--apply`. Logs one line per Keystone user in oslo.log's format:

```
... INFO rgw_quota_sync.cli [-] <uid>: quota enabled, unchanged: max_size=... max_objects=...
... WARNING rgw_quota_sync.cli [-] <uid>: quota disabled, would set max_size=... max_objects=...
... INFO rgw_quota_sync.cli [-] <uid>: quota changed: max_size=... max_objects=...
... ERROR rgw_quota_sync.cli [-] <uid>: quota update failed: PUT 403 AccessDenied
```

It ends with how many Keystone users are without an enabled quota, and exits
`1` if there are any (including users that would be set in a dry run, or that
couldn't be read), so a dry run also works as a check.

## API usage

Requests follow the [Reef Admin Ops docs](https://docs.ceph.com/en/reef/radosgw/adminops/)
(Get User Info, Set User Quota with a JSON body), except listing users, which
the docs don't cover. That uses `GET /admin/user?list`, implemented by
`RGWOp_User_List` in Reef's `rgw_rest_user.cc` and used by the Ceph dashboard.

## Development

```
tox
```

TODO: Use Keystone unified limits to set these.

TODO: Propose an upstream Ceph docs update for `GET /admin/user?list`
(`RGWOp_User_List`: `max-entries`, `marker`, response `keys` / `truncated` /
`marker`; needs `users=read`), which exists since Nautilus but isn't in the
Admin Ops docs.

TODO: Go rewrite on [go-ceph `rgw/admin`](https://pkg.go.dev/github.com/ceph/go-ceph/rgw/admin)
and [gophercloud](https://github.com/gophercloud/gophercloud), both maintained upstream,
with quotas from Keystone unified limits. See [go/HANDOFF.md](go/HANDOFF.md).
