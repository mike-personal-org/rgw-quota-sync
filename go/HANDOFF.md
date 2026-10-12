# Handoff: Go rewrite with Keystone unified limits

Status: not started. This doc is the starting point. The Go module goes in this
directory (`go/`), and the Python tool in `../src` stays as it is until the Go
version has been checked on a real cluster.

## Goal

Rewrite `rgw-quota-sync` in Go on two upstream-maintained libraries:

- [go-ceph `rgw/admin`](https://pkg.go.dev/github.com/ceph/go-ceph/rgw/admin) for the RGW Admin Ops API.
  It replaces our own SigV4 client, `../src/rgw_quota_sync/rgw.py`.
- [gophercloud v2](https://github.com/gophercloud/gophercloud) for Keystone.

The new part is where quotas come from. Each project's quota comes from Keystone
**unified limits**: a registered limit is the default, and a per-project limit
overrides it. The config file no longer holds `max_size` / `max_objects`.

## What the Python tool does today (read before designing)

Code: `../src/rgw_quota_sync/cli.py` and `rgw.py`. Docs: `../README.md`.

1. List all RGW users (`GET /admin/user?list`, paginated with `marker`).
2. Get each user's info. Skip users whose `type` isn't `keystone` (backup and service
   users). A user that was deleted after it was listed is logged and counts as OK.
3. If `user_quota.enabled` is true, leave it alone, whatever its values are. An enabled
   unlimited quota is how a user is exempted today.
4. If it's disabled, a dry run logs a WARNING that it would set the quota.
   With `--apply`, it `PUT`s the configured quota with `enabled: true`. The JSON body
   replaces the whole quota, so it copies `check_on_raw` over unchanged.
5. Log in oslo.log's format:
   `%Y-%m-%d %H:%M:%S.mmm <pid> LEVEL rgw_quota_sync.cli [-] <uid>: ...`.
   The README lists the exact message shapes.
6. Exit 1 if any Keystone user is left without an enabled quota. That includes a user
   a dry run would set and a user that couldn't be read, so a dry run doubles as a
   check. When nothing is wrong, the last line is "all Keystone users have an enabled quota".

RGW credentials: `access_key` / `secret_key` in the config, belonging to a user
with the caps `users=read,write`.

## Recommended design

The design below is a recommendation. Confirm the open decisions with Mike before building on it.

```
go/
  go.mod        module rgw-quota-sync; gophercloud/v2, go-ceph, gopkg.in/yaml.v3
  main.go       flags, config, wiring, exit code
  limits.go     Keystone unified limits -> map[projectID]quota
  sync.go       per-user decision (pure logic + quotaAPI interface)
  *_test.go
```

**Iterate over RGW users, not Keystone projects.** This keeps today's behavior:
every Keystone-type user is checked, and a project that was deleted from Keystone
but still has an RGW user isn't missed. Keystone is then only read for limits, so
its credentials need no `list_projects`. Get the project ID from the uid: with
`rgw_keystone_implicit_tenants` the uid is `<project_id>$<project_id>`, so take the
part before `$`. Check that this holds for every `type: keystone` user on the target
cluster. If a user doesn't match, log an ERROR and count it as failed. Don't guess.

**Limits (`limits.go`).** Fetch once at startup, with two list calls in total:
- `registeredlimits.List(identity, registeredlimits.ListOpts{ServiceID, ResourceName})`: `DefaultLimit`.
- `limits.List(identity, limits.ListOpts{ServiceID, ResourceName})`: `ProjectID` and `ResourceLimit`.

Do this for each of the two resources (proposed names: `rgw_user_max_size_*` and
`rgw_user_max_objects`). Look up the `object-store` service ID with
`services.List` (filter by type), or put it in the config. If a registered limit
is missing, fail at startup: there's no default to fall back on.

**Units.** Check this early. Keystone stores `default_limit` / `resource_limit` as
integers with a maximum of 2^31-1 (as far as we recall; check `keystone/limit/schema.py`).
That can't hold bytes for a multi-TiB quota. A likely fix is a size limit in GiB, for
example `rgw_user_max_size_gib`, converted to bytes before setting. `-1` means unlimited
and must pass through as `-1`, not be multiplied.

**Per-user logic (`sync.go`).** Keep it a pure function behind a small interface, so
it can be tested with a fake:

```go
type quotaAPI interface {
    GetUser(ctx context.Context, u admin.User) (admin.User, error)
    SetUserQuota(ctx context.Context, q admin.QuotaSpec) error
}
type quota struct{ MaxSize, MaxObjects int64 } // bytes / count; -1 = unlimited
```

`errors.Is(err, admin.ErrNoSuchUser)` means the user was deleted after it was listed.

**Keystone auth (`main.go`).** If `OS_CLOUD` is set, use `clouds.Parse()` followed by
`config.NewProviderClient(ctx, ao, config.WithTLSConfig(tls))`. Otherwise use
`openstack.AuthOptionsFromEnv()` followed by `openstack.AuthenticatedClient`. Then call
`openstack.NewIdentityV3`. Check the exact v2 signatures (whether they take `ctx`) in the version you pin.

**Logging and exit code.** Keep the oslo format, the message shapes and the exit
semantics from the Python tool, so existing log parsing and alerting keep working.
Logger name: `rgw_quota_sync.cli`.

## Open decisions (ask Mike)

1. **Overwrite or only fill in?** Today an enabled quota is never touched. With
   Keystone as the source of truth, the natural rule is "enabled quota ≠ limit →
   set it". Exemptions then become explicit Keystone limits of `-1`. This is the most
   important decision. Before `--apply` is ever run, a dry run should show how many
   users would change.
2. **Listing users.** go-ceph's `GetUsers` calls `GET /admin/metadata/user`. It isn't
   paginated, and it needs an extra `metadata=read` cap beyond today's `users=read,write`.
   The alternatives are to accept that cap, or to call `GET /admin/user?list`
   ourselves as `rgw.py` does. go-ceph doesn't expose that call.
3. **Resource names, units and service.** Agree on the names and the unit of the limits,
   and on whether `region_id` is used.

## go-ceph details to check against the source

- `admin.New(endpoint, accessKey, secretKey, httpClient)`: check whether `endpoint` should
  include `/admin`. The README tells users "Admin Ops API base URL, no path". Keep the
  config that way and add `/admin` in code if go-ceph needs it.
- `SetUserQuota` sends query parameters (`enabled`, `max-size`, `max-size-kb`,
  `max-objects`; nil pointers are left out) and no JSON body. In Reef's
  `RGWOp_Quota_Set` (`src/rgw/driver/rados/rgw_rest_user.cc`), a request with no body
  should default unspecified fields to their old values, which keeps `check_on_raw`.
  Confirm this in the source, and on a test user (`radosgw-admin user info`) before relying on it.
- `admin.User.Type` (`json:"type"`) exists. `Tenant` has no JSON tag and won't be filled in,
  which is another reason to get the project ID from the uid.
- Import only `rgw/admin`, which is pure Go, so `CGO_ENABLED=0 go build` should work. Check that it does.

## Verification

1. `go vet ./... && go test ./...`. Table tests over the per-user logic: not a Keystone user;
   deleted after it was listed; enabled; disabled in a dry run and with apply; a uid that
   doesn't match; a Get or Set error. Also cover limits merging (override vs default, `-1`, unit conversion).
2. `CGO_ENABLED=0 go build`.
3. On a test cluster:
   - Set the registered limits and one project override in Keystone.
   - Do a dry run and compare it with the Python dry run. Only the users whose project has
     an override should differ.
   - Run `--apply` on one test user, then check the quota and `check_on_raw` with
     `radosgw-admin user info --uid='<id>$<id>'`.

## Environment note

The original session ran on a Mac with no Go toolchain, so nothing here has been
compiled. API names come from the go-ceph and gophercloud docs as of 2026-10-12.
