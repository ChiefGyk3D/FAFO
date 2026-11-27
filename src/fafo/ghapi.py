"""GitHub REST client for Actions runs and jobs. Read-only, polite, token-safe.

- The token is read from the environment at call time and used only for the
  `Authorization: Bearer` header of one request to the configured https base host. It is
  never stored on the instance, never logged and never placed in an exception: error text is
  built from fixed words, status codes and exception class names only.
- Redirects are never followed; a 3xx is an error (no credential carry to another host).
- Conditional requests: an ETag per URL (at most 256 entries), `If-None-Match`, and a 304
  returns the cached body. A per-URL minimum spacing (the larger of the constructor floor and
  `x-poll-interval`) serves the cache without any request.
- Rate limits: when `x-ratelimit-remaining` drops below 300, or a 403 or 429 arrives, the
  client records a not-before time (retry-after, else x-ratelimit-reset) and raises
  RateLimited without sending anything until then.
- Responses are capped at 2 MB and their JSON nesting at 16 levels.

Only the endpoints for workflow runs and the jobs of a run are used ("Actions: read").
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_BODY = 2 * 1024 * 1024
MAX_DEPTH = 16
MAX_CACHE = 256
LOW_REMAINING = 300
PER_PAGE = 30
REQUEST_TIMEOUT_S = 10.0
_MAX_BACKOFF_S = 3600.0
_DEFAULT_BACKOFF_S = 60.0
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}")
_STATUSES = frozenset({"queued", "in_progress", "completed", "waiting", "requested", "pending"})

_log = logging.getLogger("fafo.ghapi")

# (status, lower-case headers, body bytes capped at MAX_BODY + 1)
Response = "tuple[int, Mapping[str, str], bytes]"
Http = Callable[[str, str, Mapping[str, str], float], "tuple[int, Mapping[str, str], bytes]"]


class GhError(Exception):
    """Any client failure. The message never contains the token or response text."""


class RateLimited(GhError):
    def __init__(self, not_before: float) -> None:
        super().__init__("github rate limited; not retrying until the not-before time")
        self.not_before = not_before


@dataclass
class _Entry:
    etag: str
    body: list
    fetched: float
    spacing: float


def _httpx_http(
    method: str, url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Default transport: httpx, no redirects, no proxy env, body streamed up to the cap."""
    import httpx

    buf = bytearray()
    try:
        with (
            httpx.Client(follow_redirects=False, timeout=timeout, trust_env=False) as client,
            client.stream(method, url, headers=dict(headers)) as resp,
        ):
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > MAX_BODY:
                    break
            return resp.status_code, {k.lower(): v for k, v in resp.headers.items()}, bytes(buf)
    except httpx.HTTPError as exc:
        raise OSError(type(exc).__name__) from None


def _depth_ok(obj: object) -> bool:
    stack = [(obj, 1)]
    while stack:
        cur, d = stack.pop()
        if d > MAX_DEPTH:
            return False
        if isinstance(cur, dict):
            stack.extend((v, d + 1) for v in cur.values())
        elif isinstance(cur, list):
            stack.extend((v, d + 1) for v in cur)
    return True


def _num(value: object) -> float | None:
    try:
        n = float(str(value).strip())
    except ValueError:
        return None
    return n if math.isfinite(n) else None


class GhClient:
    def __init__(
        self,
        token_env: str,
        base_url: str = "https://api.github.com",
        http: Http | None = None,
        clock: Callable[[], float] = time.time,
        min_interval_s: float = 10.0,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError("base_url must be an https url without credentials")
        self._token_env = token_env
        self._base = base_url.rstrip("/")
        self._http: Http = http or _httpx_http
        self._clock = clock
        self._min_interval = max(0.0, float(min_interval_s))
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._not_before = 0.0
        self._requests = 0
        self._remaining: int | None = None

    def __repr__(self) -> str:
        return f"GhClient(base={self._base!r})"

    # -- public ------------------------------------------------------------------------------

    @property
    def requests_made(self) -> int:
        return self._requests

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    @property
    def not_before(self) -> float:
        return self._not_before

    def budget_text(self) -> str:
        left = "unknown" if self._remaining is None else str(self._remaining)
        text = f"requests {self._requests}, rate remaining {left}"
        if self._not_before > self._clock():
            text += f", paused until {int(self._not_before)}"
        return text

    def runs(self, repo: str, status: str) -> list[dict]:
        self._check_repo(repo)
        if status not in _STATUSES:
            raise ValueError("invalid run status")
        path = f"/repos/{repo}/actions/runs?status={status}&per_page={PER_PAGE}"
        return self._get(path, "workflow_runs")

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        self._check_repo(repo)
        if type(run_id) is not int or run_id < 0:
            raise ValueError("invalid run id")
        path = f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"
        return self._get(path, "jobs")

    # -- internals ---------------------------------------------------------------------------

    @staticmethod
    def _check_repo(repo: str) -> None:
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or ".." in repo:
            raise ValueError("repo must be owner/name")

    def _set_not_before(self, headers: Mapping[str, str], now: float) -> float:
        retry = _num(headers.get("retry-after", ""))
        reset = _num(headers.get("x-ratelimit-reset", ""))
        if retry is not None and retry > 0:
            until = now + retry
        elif reset is not None and reset > now:
            until = reset
        else:
            until = now + _DEFAULT_BACKOFF_S
        until = min(until, now + _MAX_BACKOFF_S)
        self._not_before = max(self._not_before, until)
        return self._not_before

    def _get(self, path: str, key: str) -> list[dict]:
        now = self._clock()
        if now < self._not_before:
            raise RateLimited(self._not_before)
        entry = self._cache.get(path)
        if entry is not None and now - entry.fetched < entry.spacing:
            self._cache.move_to_end(path)
            return copy.deepcopy(entry.body)
        token = os.environ.get(self._token_env)
        if not token:
            raise GhError("token environment variable is not set")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fafo",
            "Authorization": f"Bearer {token}",
        }
        if entry is not None and entry.etag:
            headers["If-None-Match"] = entry.etag
        self._requests += 1
        try:
            status, resp_headers, body = self._http("GET", self._base + path, headers, REQUEST_TIMEOUT_S)
        except (OSError, ValueError, RuntimeError) as exc:
            raise GhError(f"github request failed ({type(exc).__name__})") from None
        finally:
            del token, headers
        hdr = {str(k).lower(): str(v) for k, v in resp_headers.items()}
        now = self._clock()
        _log.debug("github GET status=%s", status)

        remaining = _num(hdr.get("x-ratelimit-remaining", ""))
        if remaining is not None:
            self._remaining = int(remaining)
        if status in (403, 429):
            raise RateLimited(self._set_not_before(hdr, now))
        if remaining is not None and remaining < LOW_REMAINING:
            self._set_not_before({"x-ratelimit-reset": hdr.get("x-ratelimit-reset", "")}, now)
        if 300 <= status < 400 and status != 304:
            raise GhError(f"github redirect refused (status {status})")
        if status == 304:
            if entry is None:
                raise GhError("github returned 304 without a cached body")
            entry.fetched = now
            self._cache.move_to_end(path)
            return copy.deepcopy(entry.body)
        if status != 200:
            raise GhError(f"github returned status {status}")
        items = self._parse(body, key)
        poll = _num(hdr.get("x-poll-interval", ""))
        spacing = max(self._min_interval, poll or 0.0)
        self._cache[path] = _Entry(hdr.get("etag", ""), items, now, spacing)
        self._cache.move_to_end(path)
        while len(self._cache) > MAX_CACHE:
            self._cache.popitem(last=False)
        return copy.deepcopy(items)

    @staticmethod
    def _parse(body: bytes, key: str) -> list[dict]:
        if len(body) > MAX_BODY:
            raise GhError("github response too large")
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            raise GhError("github response is not valid json") from None
        if not _depth_ok(data):
            raise GhError("github response nested too deeply")
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise GhError("github response has an unexpected shape")
        return items
