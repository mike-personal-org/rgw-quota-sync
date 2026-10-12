"""Pure helpers: size parsing, config loading and the per-project decision.

Nothing in here talks to Keystone or RGW, so it can be unit tested offline.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import urlparse

import yaml

UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_SIZE_RE = re.compile(r"^(\d+)([KMGTP]?)$", re.IGNORECASE)

# Actions returned by decide().
SKIP = "skip"          # project has no RGW user yet
OK = "ok"              # quota already matches what we want
OVERRIDE = "override"  # set the project's override
DEFAULT = "default"    # set the default (quota was disabled or unlimited)
KEEP = "keep"          # project has its own enabled quota; leave it alone


@dataclass(frozen=True)
class Quota:
    """An RGW user quota. Negative max_size / max_objects mean unlimited."""

    enabled: bool
    max_size: int     # bytes
    max_objects: int

    @classmethod
    def from_rgw(cls, data: Dict[str, Any]) -> "Quota":
        """Build from the JSON returned by GET /admin/user?quota."""
        if "max_size" in data:
            max_size = int(data["max_size"])
        else:  # older RGW only reports KiB
            max_size = int(data["max_size_kb"]) * 1024
        return cls(bool(data["enabled"]), max_size, int(data["max_objects"]))

    @property
    def unlimited(self) -> bool:
        return self.max_size < 0 and self.max_objects < 0

    def same_as(self, other: "Quota") -> bool:
        # RGW treats any negative value as unlimited (e.g. max-size-kb=-1 is
        # stored as max_size=-1024), so compare those as equal.
        return (
            self.enabled == other.enabled
            and max(self.max_size, -1) == max(other.max_size, -1)
            and max(self.max_objects, -1) == max(other.max_objects, -1)
        )


def parse_size(value: Union[int, str]) -> int:
    """Parse a size such as 10T, 512G or 1048576 into bytes (1K = 1024).

    -1 means unlimited and is returned as is.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid size: {value!r}")
    if isinstance(value, int):
        if value < -1:
            raise ValueError(f"invalid size: {value!r}")
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid size: {value!r}")
    text = value.strip()
    if text == "-1":
        return -1
    m = _SIZE_RE.match(text)
    if not m:
        raise ValueError(f"invalid size: {value!r} (use bytes or a K/M/G/T/P suffix)")
    return int(m.group(1)) * UNITS[m.group(2).upper()]


def format_size(n: int) -> str:
    """Inverse of parse_size, using the largest suffix that divides evenly."""
    if n < 0:
        return "unlimited"
    for suffix in ("P", "T", "G", "M", "K"):
        if n and n % UNITS[suffix] == 0:
            return f"{n // UNITS[suffix]}{suffix}"
    return str(n)


def format_quota(q: Optional[Quota]) -> str:
    if q is None:
        return "-"
    objects = "unlimited" if q.max_objects < 0 else str(q.max_objects)
    text = f"{format_size(q.max_size)}/{objects} objects"
    return text if q.enabled else f"off ({text})"


def decide(
    current: Optional[Quota], desired: Quota, is_override: bool
) -> Tuple[str, Optional[Quota]]:
    """Return (action, quota to set or None).

    current is None when the RGW user doesn't exist.
    """
    if current is None:
        return SKIP, None
    if current.same_as(desired):
        return OK, None
    if is_override:
        return OVERRIDE, desired
    if not current.enabled or current.unlimited:
        return DEFAULT, desired
    return KEEP, None


def describe(action: str, current: Optional[Quota]) -> str:
    """Human-readable action column for the report."""
    if action == SKIP:
        return "skip: no RGW user"
    if action == KEEP:
        return f"keep: own quota {format_size(current.max_size)}"
    return action


# --- config -----------------------------------------------------------------


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    endpoint: str
    uid_format: str
    default: Quota
    verify: Union[bool, str] = True
    overrides: Dict[str, Quota] = field(default_factory=dict)

    def uid(self, project_id: str) -> str:
        return self.uid_format.format(id=project_id)


class _Loader(yaml.SafeLoader):
    """SafeLoader that keeps mapping keys as the literal text.

    Keystone project IDs are hex strings, and unquoted ones can look like
    numbers to YAML (all-digit IDs become ints, and with a leading 0
    they are read as octal, so 000...0 would turn into 0).
    """


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    out = {}
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode):
            key = key_node.value
        else:
            key = loader.construct_object(key_node, deep=True)
        if key in out:
            raise ConfigError(f"duplicate key {key!r} (line {key_node.start_mark.line + 1})")
        out[key] = loader.construct_object(value_node, deep=True)
    return out


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _mapping(value, where: str, allowed) -> Dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    return value


def _quota(value, where: str, base: Optional[Quota]) -> Quota:
    value = _mapping(value, where, ("max_size", "max_objects"))
    if base is None:
        missing = {"max_size", "max_objects"} - set(value)
        if missing:
            raise ConfigError(f"{where}: missing {', '.join(sorted(missing))}")
    try:
        max_size = parse_size(value["max_size"]) if "max_size" in value else base.max_size
    except ValueError as e:
        raise ConfigError(f"{where}.max_size: {e}") from None
    # The Admin Ops API call takes KiB, so only whole KiB can be set exactly.
    if max_size > 0 and max_size % 1024:
        raise ConfigError(f"{where}.max_size: must be a multiple of 1024 bytes")
    max_objects = value.get("max_objects", base.max_objects if base else None)
    if isinstance(max_objects, bool) or not isinstance(max_objects, int) or max_objects < -1:
        raise ConfigError(f"{where}.max_objects: expected an integer >= -1")
    return Quota(True, max_size, max_objects)


def load_config(text: str) -> Config:
    try:
        data = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from None
    data = _mapping(data, "config", ("rgw", "default", "overrides"))

    rgw = _mapping(data.get("rgw"), "rgw", ("endpoint", "uid_format", "verify"))
    endpoint = rgw.get("endpoint")
    if not isinstance(endpoint, str):
        raise ConfigError("rgw.endpoint: required")
    url = urlparse(endpoint)
    if url.scheme not in ("http", "https") or not url.netloc:
        raise ConfigError("rgw.endpoint: expected http(s)://host[:port]")
    if url.path not in ("", "/") or url.query or url.fragment:
        raise ConfigError("rgw.endpoint: must not contain a path (the API lives under /admin)")

    uid_format = rgw.get("uid_format", "{id}${id}")
    try:
        if not isinstance(uid_format, str) or "{id}" not in uid_format:
            raise ValueError
        uid_format.format(id="x")
    except (ValueError, KeyError, IndexError):
        raise ConfigError("rgw.uid_format: must be a string using only the {id} field") from None

    verify = rgw.get("verify", True)
    if not isinstance(verify, (bool, str)):
        raise ConfigError("rgw.verify: expected true, false or a CA bundle path")

    if "default" not in data:
        raise ConfigError("default: required")
    default = _quota(data["default"], "default", None)

    overrides = {}
    raw_overrides = data.get("overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ConfigError("overrides: expected a mapping of project ID to quota")
    for project_id, value in raw_overrides.items():
        overrides[str(project_id)] = _quota(value, f"overrides.{project_id}", default)

    return Config(
        endpoint=f"{url.scheme}://{url.netloc}",
        uid_format=uid_format,
        default=default,
        verify=verify,
        overrides=overrides,
    )
