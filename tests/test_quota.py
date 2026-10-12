import pytest

from rgw_quota_sync.quota import (
    DEFAULT,
    KEEP,
    OK,
    OVERRIDE,
    SKIP,
    ConfigError,
    Quota,
    decide,
    describe,
    format_quota,
    format_size,
    load_config,
    parse_size,
)

T = 1024**4
DEFAULT_Q = Quota(True, 10 * T, -1)


# --- parse_size / format_size ------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10T", 10 * T),
        ("10t", 10 * T),
        ("1K", 1024),
        ("3M", 3 * 1024**2),
        ("512G", 512 * 1024**3),
        ("2P", 2 * 1024**5),
        ("4096", 4096),
        (4096, 4096),
        (0, 0),
        ("-1", -1),
        (-1, -1),
        (" 5G ", 5 * 1024**3),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "10X", "1.5T", "T", "-2", -2, "10 T", True, None, 1.5])
def test_parse_size_rejects(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


@pytest.mark.parametrize(
    "n,text", [(10 * T, "10T"), (1536, "1536"), (2048, "2K"), (-1, "unlimited"), (0, "0"), (100, "100")]
)
def test_format_size(n, text):
    assert format_size(n) == text


def test_format_size_round_trips():
    for s in ("10T", "250T", "1K", "7M", "3G", "1P"):
        assert format_size(parse_size(s)) == s


def test_format_quota():
    assert format_quota(None) == "-"
    assert format_quota(Quota(True, 10 * T, -1)) == "10T/unlimited objects"
    assert format_quota(Quota(False, -1, 5)) == "off (unlimited/5 objects)"


# --- Quota.from_rgw -----------------------------------------------------------


def test_from_rgw_pacific_response():
    data = {"enabled": True, "check_on_raw": False, "max_size": 10 * T,
            "max_size_kb": 10 * T // 1024, "max_objects": -1}
    assert Quota.from_rgw(data) == Quota(True, 10 * T, -1)


def test_from_rgw_kb_only():
    assert Quota.from_rgw({"enabled": False, "max_size_kb": 4, "max_objects": 9}) == Quota(False, 4096, 9)


# --- decide: one test per branch -----------------------------------------------


def test_decide_no_rgw_user():
    assert decide(None, DEFAULT_Q, is_override=False) == (SKIP, None)
    assert decide(None, DEFAULT_Q, is_override=True) == (SKIP, None)


def test_decide_override_matches():
    q = Quota(True, 250 * T, -1)
    assert decide(Quota(True, 250 * T, -1), q, is_override=True) == (OK, None)


@pytest.mark.parametrize(
    "current",
    [
        Quota(False, 250 * T, -1),      # disabled
        Quota(True, 100 * T, -1),       # smaller size
        Quota(True, 500 * T, -1),       # bigger size: overrides are exact, so lower it
        Quota(True, 250 * T, 1000),     # object count differs
        Quota(False, -1, -1),           # fresh user
    ],
)
def test_decide_override_differs(current):
    desired = Quota(True, 250 * T, -1)
    assert decide(current, desired, is_override=True) == (OVERRIDE, desired)


@pytest.mark.parametrize(
    "current",
    [
        Quota(False, -1, -1),        # fresh user, quota disabled
        Quota(False, 5 * T, -1),     # disabled with stale values
        Quota(True, -1, -1),         # enabled but unlimited
        Quota(True, -1024, -1),      # unlimited as stored after max-size-kb=-1
    ],
)
def test_decide_default_when_disabled_or_unlimited(current):
    assert decide(current, DEFAULT_Q, is_override=False) == (DEFAULT, DEFAULT_Q)


def test_decide_default_already_set():
    assert decide(Quota(True, 10 * T, -1), DEFAULT_Q, is_override=False) == (OK, None)


@pytest.mark.parametrize(
    "current",
    [
        Quota(True, 50 * T, -1),     # bigger than default
        Quota(True, 1 * T, -1),      # smaller than default: never lowered or raised
        Quota(True, -1, 1000),       # only an object limit
    ],
)
def test_decide_keeps_own_quota(current):
    assert decide(current, DEFAULT_Q, is_override=False) == (KEEP, None)


def test_negative_sizes_compare_as_unlimited():
    desired = Quota(True, -1, 10)
    assert decide(Quota(True, -1024, 10), desired, is_override=True) == (OK, None)


def test_describe():
    assert describe(SKIP, None) == "skip: no RGW user"
    assert describe(KEEP, Quota(True, 50 * T, -1)) == "keep: own quota 50T"
    assert describe(DEFAULT, Quota(False, -1, -1)) == "default"


# --- load_config ---------------------------------------------------------------

EXAMPLE = """
rgw:
  endpoint: https://rgw.example.org:8080
  uid_format: "{id}${id}"
default:
  max_size: 10T
  max_objects: -1
overrides:
  00000000000000000000000000000000: {max_size: 250T}
  01234567012345670123456701234567: {max_objects: 5}
  "abcdef": {}
"""


def test_load_config():
    cfg = load_config(EXAMPLE)
    assert cfg.endpoint == "https://rgw.example.org:8080"
    assert cfg.uid("abc") == "abc$abc"
    assert cfg.verify is True
    assert cfg.default == DEFAULT_Q
    # Unquoted IDs that look like numbers keep their exact text.
    assert cfg.overrides == {
        "0" * 32: Quota(True, 250 * T, -1),
        "01234567" * 4: Quota(True, 10 * T, 5),
        "abcdef": DEFAULT_Q,
    }


def test_example_config_file_loads():
    import pathlib

    text = (pathlib.Path(__file__).parent.parent / "config.example.yaml").read_text()
    cfg = load_config(text)
    assert cfg.overrides["0" * 32] == Quota(True, 250 * T, -1)


def test_uid_format_defaults_to_implicit_tenants():
    cfg = load_config("rgw: {endpoint: 'http://rgw:8080'}\ndefault: {max_size: 1G, max_objects: 10}\n")
    assert cfg.uid("p") == "p$p"
    assert cfg.overrides == {}


@pytest.mark.parametrize(
    "text,msg",
    [
        ("default: {max_size: 1G, max_objects: 1}", "rgw.endpoint"),
        ("rgw: {endpoint: 'rgw:8080'}\ndefault: {max_size: 1G, max_objects: 1}", "rgw.endpoint"),
        ("rgw: {endpoint: 'https://rgw/admin'}\ndefault: {max_size: 1G, max_objects: 1}", "path"),
        ("rgw: {endpoint: 'https://rgw', uid_format: '{project}'}\ndefault: {max_size: 1G, max_objects: 1}",
         "uid_format"),
        ("rgw: {endpoint: 'https://rgw'}", "default"),
        ("rgw: {endpoint: 'https://rgw'}\ndefault: {max_size: 1G}", "max_objects"),
        ("rgw: {endpoint: 'https://rgw'}\ndefault: {max_size: 1000, max_objects: 1}", "multiple of 1024"),
        ("rgw: {endpoint: 'https://rgw'}\ndefault: {max_size: 1Q, max_objects: 1}", "max_size"),
        ("rgw: {endpoint: 'https://rgw'}\ndefault: {max-size: 1G, max_objects: 1}", "unknown key"),
        ("rgw: {endpoint: 'https://rgw'}\ndefault: {max_size: 1G, max_objects: 1}\n"
         "overrides: {abc: {max_size: 1G}, abc: {max_size: 2G}}", "duplicate key"),
        ("rgw: {endpoint: 'https://rgw', access_key: x}\ndefault: {max_size: 1G, max_objects: 1}", "unknown key"),
        ("rgw: [", "invalid YAML"),
    ],
)
def test_load_config_errors(text, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(text)
