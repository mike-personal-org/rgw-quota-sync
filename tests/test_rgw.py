import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests

from rgw_quota_sync.rgw import RGW, NoSuchUser, sign

# Example credentials and date from the AWS SigV4 docs.
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
NOW = datetime(2013, 5, 24, tzinfo=UTC)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

ENDPOINT = "https://rgw.example.org:8080"
QUOTA = {
    "enabled": True,
    "check_on_raw": False,
    "max_size": 10995116277760,
    "max_objects": -1,
}


def signature(headers):
    return headers["Authorization"].split("Signature=")[1]


@pytest.mark.parametrize(
    "query,expected",
    [
        # "GET Bucket Lifecycle" and "GET Bucket (List Objects)" examples from
        # https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
        (
            "lifecycle=",
            "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543",
        ),
        (
            "max-keys=2&prefix=J",
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_sign_matches_aws_examples(query, expected):
    host = "examplebucket.s3.amazonaws.com"
    headers = sign("GET", host, "/", query, EMPTY_SHA256, ACCESS_KEY, SECRET_KEY, NOW)
    assert signature(headers) == expected


def test_sign_matches_requests_aws4auth():
    # Computed once with requests-aws4auth 1.4.0 for the request set_quota sends.
    payload_hash = hashlib.sha256(json.dumps(QUOTA).encode()).hexdigest()
    headers = sign(
        "PUT",
        "rgw.example.org:8080",
        "/admin/user",
        "format=json&quota=&quota-type=user&uid=abc%24abc",
        payload_hash,
        ACCESS_KEY,
        SECRET_KEY,
        NOW,
    )
    expected = "a9b52c916ee6cebf6233dc2cda006dc9634a783fb3dbfb25407d4f1da4b9811c"
    assert signature(headers) == expected


class FakeSession:
    """Records requests and answers them with the given responses, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)  # (status, body) pairs
        self.requests = []

    def request(self, method, url, headers, data, timeout):
        self.requests.append(
            SimpleNamespace(method=method, url=url, headers=headers, data=data)
        )
        status, body = self.responses.pop(0)
        return SimpleNamespace(
            ok=status < 400,
            status_code=status,
            reason="",
            json=lambda: body,
            request=SimpleNamespace(method=method),
        )


def make_rgw(session):
    rgw = RGW(ENDPOINT, ACCESS_KEY, SECRET_KEY)
    rgw.session = session
    return rgw


@pytest.mark.parametrize(
    "endpoint",
    [
        "rgw.example.org:8080",  # no scheme
        "https://rgw.example.org:8080/rgw",  # requests are signed for /admin/user
    ],
)
def test_rejects_bad_endpoint(endpoint):
    with pytest.raises(ValueError, match="endpoint must be"):
        RGW(endpoint, ACCESS_KEY, SECRET_KEY)


def test_accepts_trailing_slash():
    assert RGW(f"{ENDPOINT}/", ACCESS_KEY, SECRET_KEY).endpoint == ENDPOINT


def test_list_users_follows_markers():
    session = FakeSession(
        (200, {"keys": ["a", "b"], "truncated": True, "marker": "m1"}),
        (200, {"keys": ["c"], "truncated": False}),
    )
    assert list(make_rgw(session).list_users()) == ["a", "b", "c"]
    assert [request.url for request in session.requests] == [
        f"{ENDPOINT}/admin/user?list&format=json&max-entries=1000",
        f"{ENDPOINT}/admin/user?list&format=json&max-entries=1000&marker=m1",
    ]


def test_list_users_refuses_repeated_marker():
    session = FakeSession(
        (200, {"keys": ["a"], "truncated": True, "marker": "m1"}),
        (200, {"keys": ["b"], "truncated": True, "marker": "m1"}),
    )
    with pytest.raises(RuntimeError, match="bad marker"):
        list(make_rgw(session).list_users())


def test_get_user():
    user = {"user_id": "abc$abc", "type": "keystone", "user_quota": QUOTA}
    session = FakeSession((200, user))
    assert make_rgw(session).get_user("abc$abc") == user

    [request] = session.requests
    assert request.method == "GET"
    assert request.url == f"{ENDPOINT}/admin/user?format=json&uid=abc%24abc"
    assert request.headers["Host"] == "rgw.example.org:8080"


def test_set_quota_sends_json_body():
    session = FakeSession((200, None))
    make_rgw(session).set_quota("abc$abc", QUOTA)

    [request] = session.requests
    assert request.method == "PUT"
    assert request.url == (
        f"{ENDPOINT}/admin/user?quota&format=json&uid=abc%24abc&quota-type=user"
    )
    assert json.loads(request.data) == QUOTA
    assert request.headers["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"


def test_missing_user_raises_no_such_user():
    session = FakeSession((404, {"Code": "NoSuchUser"}))
    with pytest.raises(NoSuchUser):
        make_rgw(session).get_user("abc$abc")


@pytest.mark.parametrize("status,code", [(403, "AccessDenied"), (404, "NoSuchKey")])
def test_other_errors_raise_http_error(status, code):
    session = FakeSession((status, {"Code": code}))
    with pytest.raises(requests.HTTPError, match=f"GET {status} {code}") as error:
        make_rgw(session).get_user("abc$abc")
    assert not isinstance(error.value, NoSuchUser)
