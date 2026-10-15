"""Minimal BrickLink store API client: the price guide only.

BrickLink's API (api.bricklink.com/api/store/v1) uses OAuth 1.0 with
HMAC-SHA1 signatures. Four secrets are needed, all from
bricklink.com/v2/api/register_consumer.page: a consumer key and secret for
the application, and an access token and token secret issued for the public
IP address the calls come from. None of them is ever written to disk, put in
a URL or shown in an error message.

The price guide call is one request per part and colour, so responses are
cached on disk for a day; prices do not move faster than that.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BASE_URL = "https://api.bricklink.com/api/store/v1"
ENV = {
    "consumer_key": "BRICKLINK_CONSUMER_KEY",
    "consumer_secret": "BRICKLINK_CONSUMER_SECRET",
    "token": "BRICKLINK_TOKEN",
    "token_secret": "BRICKLINK_TOKEN_SECRET",
}
MIN_INTERVAL = 0.25  # seconds between live requests (the limit is 5000 a day)
CACHE_HOURS = 24
REGIONS = ("", "north_america", "europe", "eu", "asia", "oceania", "south_america", "africa", "middle_east")


class BrickLinkError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    consumer_key: str
    consumer_secret: str
    token: str
    token_secret: str

    @classmethod
    def from_env(cls) -> "Credentials | None":
        values = {name: os.environ.get(var, "").strip() for name, var in ENV.items()}
        return cls(**values) if all(values.values()) else None

    @classmethod
    def from_dict(cls, data: dict) -> "Credentials | None":
        values = {name: str(data.get(name) or "").strip() for name in ENV}
        return cls(**values) if all(values.values()) else None


def default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "brickpipe" / "bricklink"


def _enc(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-._~")


def sign(method: str, url: str, params: dict[str, str], creds: Credentials,
         timestamp: str | None = None, nonce: str | None = None) -> str:
    """The OAuth 1.0 Authorization header for one request (HMAC-SHA1).

    `url` is the endpoint without a query string; `params` are the query
    parameters, which take part in the signature.
    """
    oauth = {
        "oauth_consumer_key": creds.consumer_key,
        "oauth_token": creds.token,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_nonce": nonce or secrets.token_hex(8),
        "oauth_version": "1.0",
    }
    everything = sorted((_enc(k), _enc(v)) for k, v in {**params, **oauth}.items())
    normalised = "&".join(f"{k}={v}" for k, v in everything)
    base = "&".join([method.upper(), _enc(url), _enc(normalised)])
    key = f"{_enc(creds.consumer_secret)}&{_enc(creds.token_secret)}".encode()
    digest = hmac.new(key, base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    header = ", ".join(f'{k}="{_enc(v)}"' for k, v in sorted(oauth.items()))
    return "OAuth realm=\"\", " + header


def http_get(url: str, params: dict[str, str], creds: Credentials) -> dict:
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(full, headers={
        "Authorization": sign("GET", url, params, creds), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")
        try:
            body = json.loads(detail)
        except ValueError:
            raise BrickLinkError(f"BrickLink returned HTTP {err.code}") from None
    except (urllib.error.URLError, TimeoutError) as err:
        raise BrickLinkError(f"Could not reach BrickLink: {err}") from None
    meta = body.get("meta") or {}
    code = int(meta.get("code") or 0)
    if code == 200 or code == 404:
        return body
    description = str(meta.get("description") or meta.get("message") or "")
    if "IP" in description.upper() or "TOKEN_IP" in description.upper():
        raise BrickLinkError(
            "BrickLink refused the access token for this computer's address. Your public "
            "IP address has probably changed: add a token for the new address at "
            "bricklink.com/v2/api/register_consumer.page and paste it into Settings."
        )
    if code in (401, 403) or "OAUTH" in description.upper() or "SIGNATURE" in description.upper():
        raise BrickLinkError(
            f"BrickLink rejected the credentials ({description or code}). Check the four "
            "values in Settings: consumer key and secret, token value and token secret."
        )
    raise BrickLinkError(f"BrickLink error {code}: {description or 'no details'}")


class Client:
    def __init__(
        self,
        creds: Credentials | None = None,
        cache_dir: Path | None = None,
        fetch: Callable[[str, dict], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        """`fetch(url, params)` replaces the network call in tests."""
        if fetch is None:
            if creds is None:
                raise BrickLinkError(
                    "No BrickLink credentials. Set " + ", ".join(ENV.values()) +
                    " (from bricklink.com/v2/api/register_consumer.page)."
                )
            fetch = lambda url, params: http_get(url, params, creds)  # noqa: E731
        self._fetch = fetch
        self._sleep = sleep
        self._cache_dir = cache_dir
        self._last_call = 0.0
        self.live_calls = 0

    def _get(self, path: str, **params) -> dict:
        params = {k: str(v) for k, v in params.items() if v not in (None, "")}
        url = f"{BASE_URL}{path}"
        cache_key = url + "?" + urllib.parse.urlencode(sorted(params.items()))
        cache_path = None
        if self._cache_dir is not None:
            cache_path = self._cache_dir / (hashlib.sha256(cache_key.encode()).hexdigest()[:32] + ".json")
            if cache_path.is_file():
                try:
                    saved = json.loads(cache_path.read_text("utf-8"))
                    if time.time() - saved["fetched"] < CACHE_HOURS * 3600:
                        return saved["body"]
                except (ValueError, KeyError, OSError):
                    pass
        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if self.live_calls and wait > 0:
            self._sleep(wait)
        body = self._fetch(url, params)
        self._last_call = time.monotonic()
        self.live_calls += 1
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"fetched": time.time(), "body": body}), "utf-8")
        return body

    def price(
        self,
        item_no: str,
        colour_id: int | None,
        *,
        item_type: str = "PART",
        guide_type: str = "stock",
        new_or_used: str = "N",
        region: str = "",
        country_code: str = "",
        currency_code: str = "",
    ) -> dict | None:
        """BrickLink's price guide for one item and colour, or None when the
        item is unknown to BrickLink.

        The result has `min_price`, `max_price`, `avg_price`, `qty_avg_price`
        (as strings), `unit_quantity` (lots for sale) and `total_quantity`
        (pieces for sale), plus `currency_code`.
        """
        body = self._get(
            f"/items/{item_type}/{urllib.parse.quote(item_no, safe='')}/price",
            color_id=colour_id, guide_type=guide_type, new_or_used=new_or_used,
            region=region, country_code=country_code, currency_code=currency_code,
        )
        if int((body.get("meta") or {}).get("code") or 0) == 404:
            return None
        return body.get("data") or None
