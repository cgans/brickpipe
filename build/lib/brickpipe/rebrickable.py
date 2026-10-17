"""Minimal Rebrickable API v3 client.

Only what the availability check needs: the colour table, part lookup and the
colours each part was produced in.

The API key is read from the REBRICKABLE_API_KEY environment variable and sent
in the Authorization header. It is never put in a URL, the cache or an error
message.

Rebrickable asks for about one request per second. Requests are spaced out,
HTTP 429 is honoured, and every response is cached on disk so a second run on
the same model makes no calls at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

BASE_URL = "https://rebrickable.com/api/v3"
KEY_ENV = "REBRICKABLE_API_KEY"
MIN_INTERVAL = 1.1  # seconds between live requests
CACHE_DAYS = 30
BATCH = 100  # part numbers per list request


class RebrickableError(RuntimeError):
    pass


class NotFound(RebrickableError):
    pass


def default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "brickpipe" / "rebrickable"


def http_get(url: str, key: str, data: bytes | None = None) -> dict:
    """One live GET (or POST when `data` is given). Raises NotFound on 404,
    RebrickableError otherwise."""
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"Authorization": f"key {key}", "Accept": "application/json",
                 **({"Content-Type": "application/x-www-form-urlencoded"} if data is not None else {})},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise NotFound(url) from None
            if err.code == 429 and attempt < 3:
                detail = err.read().decode("utf-8", "replace")
                match = re.search(r"(\d+)\s*second", detail)
                time.sleep((int(match.group(1)) if match else 5) + 1)
                continue
            if err.code in (401, 403):
                raise RebrickableError(
                    f"Rebrickable rejected the API key (HTTP {err.code}). "
                    f"Check {KEY_ENV}."
                ) from None
            raise RebrickableError(f"Rebrickable returned HTTP {err.code} for {url}") from None
        except (urllib.error.URLError, TimeoutError) as err:
            raise RebrickableError(f"Could not reach Rebrickable: {err}") from None
    raise RebrickableError(f"Rebrickable kept throttling {url}")


class Client:
    def __init__(
        self,
        key: str | None = None,
        cache_dir: Path | None = None,
        fetch: Callable[[str], dict] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        post: Callable[[str, bytes], dict] | None = None,
    ):
        """`fetch` replaces the network call; tests use it to serve fixtures.
        `post` likewise for the one POST (signing in)."""
        if fetch is None:
            key = key or os.environ.get(KEY_ENV, "").strip()
            if not key:
                raise RebrickableError(
                    f"No API key. Set the {KEY_ENV} environment variable "
                    "(free key: rebrickable.com > Account > Settings > API)."
                )
            fetch = lambda url: http_get(url, key)  # noqa: E731
            post = post or (lambda url, data: http_get(url, key, data))
        self._fetch = fetch
        self._post = post or (lambda url, data: {})
        self._sleep = sleep
        self._cache_dir = cache_dir
        self._last_call = 0.0
        self.live_calls = 0

    # -- transport ---------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _get(self, url: str, cache: bool = True) -> dict:
        path = self._cache_path(url) if cache else None
        if path and path.is_file():
            try:
                saved = json.loads(path.read_text("utf-8"))
                if time.time() - saved["fetched"] < CACHE_DAYS * 86400:
                    return saved["body"]
            except (ValueError, KeyError, OSError):
                pass  # unreadable cache entry: fetch again

        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if self.live_calls and wait > 0:
            self._sleep(wait)
        try:
            body = self._fetch(url)
        except NotFound:
            body = {"__not_found__": True}
        self._last_call = time.monotonic()
        self.live_calls += 1

        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"url": url, "fetched": time.time(), "body": body}), "utf-8"
            )
        return body

    def _list(self, path: str, _cache: bool = True, **params) -> list[dict]:
        """Fetch every page of a list endpoint."""
        params.setdefault("page_size", 1000)
        url: str | None = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        results: list[dict] = []
        while url:
            body = self._get(url, cache=_cache)
            if body.get("__not_found__"):
                break
            if "results" not in body:
                raise RebrickableError(
                    f"Unexpected response from {path}: keys {sorted(body)[:8]}"
                )
            results.extend(body["results"])
            url = body.get("next")
        return results

    # -- endpoints ---------------------------------------------------------

    def colours(self) -> list[dict]:
        return self._list("/lego/colors/")

    def parts_by_number(self, part_nums: list[str]) -> list[dict]:
        found: list[dict] = []
        for i in range(0, len(part_nums), BATCH):
            chunk = part_nums[i:i + BATCH]
            found += self._list(
                "/lego/parts/", part_nums=",".join(chunk), inc_part_details=1
            )
        return found

    def parts_by_ldraw_id(self, ldraw_id: str) -> list[dict]:
        return self._list("/lego/parts/", ldraw_id=ldraw_id, inc_part_details=1)

    # -- the user's own collection (never cached: it changes) -------------

    def sign_in(self, username: str, password: str) -> str:
        """Trade a Rebrickable username and password for a user token. The
        password is sent once to Rebrickable and not kept."""
        data = urllib.parse.urlencode({"username": username, "password": password}).encode()
        body = self._post(f"{BASE_URL}/users/_token/", data)
        token = body.get("user_token")
        if not token:
            raise RebrickableError("Rebrickable did not return a user token; check the username and password.")
        return str(token)

    def owned_parts(self, user_token: str) -> list[dict]:
        """Everything in the user's part lists and set lists."""
        quoted = urllib.parse.quote(user_token, safe="")
        return self._list(f"/users/{quoted}/allparts/", _cache=False)

    def part_colours(self, part_num: str) -> list[dict]:
        quoted = urllib.parse.quote(part_num, safe="")
        return self._list(f"/lego/parts/{quoted}/colors/")
