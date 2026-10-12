"""Minimal RGW Admin Ops client for Ceph Reef: list users, read and set quotas.

Admin Ops API: https://docs.ceph.com/en/reef/radosgw/adminops/
Server side:   https://github.com/ceph/ceph/blob/reef/src/rgw/driver/rados/rgw_rest_user.cc

Admin Ops uses S3 authentication, so requests are signed with AWS SigV4. A
signing bug makes RGW reject the request (403); it cannot change what gets
written.
"""

import hashlib
import hmac
import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

import requests

TIMEOUT = 30  # seconds per request
REGION = "us-east-1"  # RGW only uses it to derive the signing key
SIGNED_HEADERS = "host;x-amz-content-sha256;x-amz-date"
LIST_PAGE_SIZE = 1000


class NoSuchUser(requests.HTTPError):
    pass


def sign(
    method: str,
    host: str,
    path: str,
    query: str,
    payload_hash: str,
    access_key: str,
    secret_key: str,
    now: datetime,
) -> dict[str, str]:
    """Return the SigV4 headers for a request.

    query must already be canonical: URI-encoded and sorted by key, with
    value-less parameters written as "name=". payload_hash is the hex SHA-256
    of the request body.
    https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
    """
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = timestamp[:8]
    scope = f"{date}/{REGION}/s3/aws4_request"

    canonical_headers = (
        f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{timestamp}\n"
    )
    canonical_request = (
        f"{method}\n"
        f"{path}\n"
        f"{query}\n"
        f"{canonical_headers}\n"
        f"{SIGNED_HEADERS}\n"
        f"{payload_hash}"
    )
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        f"{timestamp}\n"
        f"{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    date_key = _hmac(f"AWS4{secret_key}".encode(), date)
    region_key = _hmac(date_key, REGION)
    service_key = _hmac(region_key, "s3")
    signing_key = _hmac(service_key, "aws4_request")
    signature = _hmac(signing_key, string_to_sign).hex()

    return {
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": timestamp,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
        ),
    }


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _uri_encode(value: str) -> str:
    # SigV4 encodes everything except the unreserved characters A-Z a-z 0-9 -_.~
    return quote(value, safe="-_.~")


def _raise_for_error(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        code = response.json()["Code"]  # e.g. AccessDenied
    except (ValueError, KeyError, TypeError):
        code = response.reason
    message = f"{response.request.method} {response.status_code} {code}"
    if code == "NoSuchUser":
        raise NoSuchUser(message)
    raise requests.HTTPError(message)


class RGW:
    def __init__(self, endpoint: str, access_key: str, secret_key: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        parts = urlsplit(self.endpoint)
        # Requests are signed for /admin/user, so a path prefix would get a 403.
        if parts.scheme not in ("http", "https") or parts.path:
            raise ValueError(f"endpoint must be http(s)://host[:port]: {endpoint!r}")
        self.host = parts.netloc
        self.access_key = access_key
        self.secret_key = secret_key
        self.session = requests.Session()

    def _request(
        self,
        method: str,
        subresource: str | None,
        params: Mapping[str, str | int],
        body: bytes = b"",
    ) -> requests.Response:
        """Send a signed request to /admin/user, e.g. ?quota&format=json&uid=...

        The subresource goes first without a value, as in the docs' syntax.
        """
        params = {"format": "json", **params}
        encoded = {_uri_encode(k): _uri_encode(str(v)) for k, v in params.items()}

        query = "&".join(f"{k}={v}" for k, v in encoded.items())
        if subresource:
            query = f"{subresource}&{query}"
            encoded[subresource] = ""
        canonical_query = "&".join(f"{k}={v}" for k, v in sorted(encoded.items()))

        headers = sign(
            method,
            self.host,
            "/admin/user",
            canonical_query,
            "UNSIGNED-PAYLOAD",  # Reef answers 501 to a signed body on Admin Ops calls
            self.access_key,
            self.secret_key,
            datetime.now(UTC),
        )
        response = self.session.request(
            method,
            f"{self.endpoint}/admin/user?{query}",
            headers=headers,
            data=body,
            timeout=TIMEOUT,
        )
        _raise_for_error(response)
        return response

    def list_users(self) -> Iterator[str]:
        """Yield every user ID, fetching a page at a time.

        Not in the Admin Ops docs. Implemented by RGWOp_User_List in
        rgw_rest_user.cc (Nautilus onwards) and used by the Ceph dashboard
        (RgwUser.list in src/pybind/mgr/dashboard/controllers/rgw.py).
        """
        params: dict[str, str | int] = {"max-entries": LIST_PAGE_SIZE}
        while True:
            page = self._request("GET", "list", params).json()
            yield from page["keys"]
            if not page["truncated"]:
                return
            # Like the dashboard, refuse to loop on a missing or repeated marker.
            if not page.get("marker") or page["marker"] == params.get("marker"):
                raise RuntimeError(f"RGW user list returned bad marker: {page}")
            params["marker"] = page["marker"]

    def get_user(self, uid: str) -> dict[str, Any]:
        """Return the user's info, including "type" and "user_quota".

        The response also contains the user's keys, so never log it.
        Raises NoSuchUser.
        https://docs.ceph.com/en/reef/radosgw/adminops/#get-user-info
        """
        return self._request("GET", None, {"uid": uid}).json()

    def set_quota(self, uid: str, quota: dict[str, Any]) -> None:
        """Replace the user quota with quota, shaped like get_user's "user_quota".

        https://docs.ceph.com/en/reef/radosgw/adminops/#set-user-quota
        """
        params = {"uid": uid, "quota-type": "user"}
        self._request("PUT", "quota", params, json.dumps(quota).encode())
