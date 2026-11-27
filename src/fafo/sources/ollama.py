"""Ollama / local LLM source: loaded models from /api/ps, queue from an optional log.

Read only. The base URL comes from configuration; no host or port is built in.
Everything fetched is untrusted: size capped, depth capped, strings sanitized.
Any network or parse failure raises, and the collector turns that into stale or
down while keeping the previous rows. An unreadable admission log is not an error.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ..collector import backoff_delay
from ..model import DEFAULT_LLM_NAME, MAX_LLM_SERVERS, Inflight, LlmServer, LoadedModel
from ..redact import sanitize_text
from ..source import Ctx, SourceResult

TIMEOUT_S = 3.0
MAX_BODY = 256 * 1024
MAX_DEPTH = 16
MAX_MODELS = 64
MAX_NAME = 100
LOG_TAIL_BYTES = 64 * 1024
LOG_TAIL_LINES = 200
MAX_QUEUE = 1000
MAX_INT = 2**62
MAX_TS = 4e9


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


# (url, total timeout seconds, max body bytes) -> response. Must not follow redirects.
HttpGet = Callable[[str, float, int], HttpResponse]


READ_TIMEOUT_S = 1.0


def default_http_get(url: str, timeout: float, max_bytes: int) -> HttpResponse:
    """GET with no redirects, no environment proxies or credentials, capped body and time.

    One monotonic deadline covers the whole call. Per-phase timeouts are min(timeout,
    remaining) with a short read timeout, so a stalled or slow-drip server cannot hold
    the call past the deadline plus one read timeout.
    """
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("ollama request deadline exceeded")
        return left

    left = remaining()
    client = httpx.Client(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=left, read=min(READ_TIMEOUT_S, left), write=left, pool=left
        ),
        headers={"Accept": "application/json"},
    )
    with client, client.stream("GET", url) as resp:
        body = bytearray()
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > max_bytes:
                raise ValueError("response too large")
            remaining()
        return HttpResponse(resp.status_code, bytes(body))


def _check_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("ollama url: invalid characters")
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        raise ValueError("ollama url: not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("ollama url: only http and https are accepted")
    if not parts.hostname:
        raise ValueError("ollama url: host missing")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ValueError("ollama url: credentials in the URL are not accepted")
    if parts.query or parts.fragment:
        raise ValueError("ollama url: query and fragment are not accepted")
    return url.rstrip("/")


def _check_depth(body: bytes) -> None:
    """Reject nesting deeper than MAX_DEPTH by scanning brackets outside strings."""
    depth = 0
    in_str = False
    esc = False
    for b in body:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError("json nesting too deep")
        elif b in (0x5D, 0x7D):
            depth -= 1


def _parse_json(body: bytes) -> object:
    if len(body) > MAX_BODY:
        raise ValueError("response too large")
    _check_depth(body)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("response is not valid json") from None


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _parse_expiry(value: object) -> float:
    """RFC 3339 with an offset to epoch seconds; anything else is 0.0."""
    if not isinstance(value, str) or len(value) > 64:
        return 0.0
    try:
        dt = datetime.fromisoformat(_FRACTION.sub(r"\1", value))
        if dt.tzinfo is None:
            return 0.0
        ts = dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0
    return ts if ts > 0 else 0.0


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INT:
        return 0
    return value


def _model(entry: object) -> LoadedModel | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        return None
    return LoadedModel(
        name=sanitize_text(entry["name"], MAX_NAME),
        vram=_int(entry.get("size_vram")),
        context=_int(entry.get("context_length")),
        expires=_parse_expiry(entry.get("expires_at")),
    )


def _read_tail(path: Path) -> bytes:
    """Last LOG_TAIL_BYTES of a regular file, or b"" on any problem."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return b""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return b""
        start = max(0, st.st_size - LOG_TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        data = os.read(fd, LOG_TAIL_BYTES)
        if start > 0:
            data = data.partition(b"\n")[2]  # first line is cut mid-way
        return data
    except OSError:
        return b""
    finally:
        os.close(fd)


def _ts(value: object) -> float | None:
    """Seconds in 0..MAX_TS as a float, else None (the record is skipped)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        ts = float(value)
    except (OverflowError, ValueError):
        return None
    return ts if 0.0 <= ts <= MAX_TS else None  # NaN fails both comparisons


def _admission(path: Path | None) -> tuple[int, list[Inflight]]:
    """Derive (queue, inflight) from the log tail. Best effort, never raises."""
    if path is None:
        return 0, []
    lines = _read_tail(path).splitlines()[-LOG_TAIL_LINES:]
    busy: dict[tuple[str, int | None], int] = {}
    inflight: dict[tuple[str, int | None], Inflight] = {}
    for raw in lines:
        try:
            rec = json.loads(raw) if len(raw) <= 4096 else None
        except (ValueError, RecursionError):
            continue
        if not isinstance(rec, dict):
            continue
        event, model, pid = rec.get("event"), rec.get("model"), rec.get("session_pid")
        if not isinstance(model, str):
            model = ""
        model = sanitize_text(model, MAX_NAME)
        if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= MAX_INT:
            pid = None
        ts = _ts(rec.get("ts"))
        if ts is None:
            continue
        key = (model, pid)
        if event == "busy":
            busy[key] = busy.get(key, 0) + 1
        elif event == "admit":
            if busy.get(key, 0) > 1:
                busy[key] -= 1
            else:
                busy.pop(key, None)
            inflight[key] = Inflight(model=model, session_pid=pid, started=ts)
        elif event == "release":
            inflight.pop(key, None)
    queue = min(sum(busy.values()), MAX_QUEUE)
    return queue, list(inflight.values())[:MAX_MODELS]


@dataclass(frozen=True)
class ServerSpec:
    """One server to poll: a label, a base URL and an optional admission log."""

    name: str
    url: str
    admission_log: Path | None = None


@dataclass
class _Server:
    """A server and its own health: last good rows, failure count, next allowed poll."""

    spec: ServerSpec
    url: str  # validated, no trailing slash
    last: LlmServer | None = None
    failures: int = 0
    next_try: float = 0.0
    error: Exception | None = None
    pending: Future | None = None


class OllamaSource:
    """Polls every configured server, each with its own backoff, in parallel.

    One server failing never blanks the others: its last good rows are kept and flagged
    unreachable (their last_ok stays old, so they age), and the rest stay fresh. Only when
    every server fails in the same call does collect() raise, as a single server always did,
    so the collector marks the source down and keeps the previous rows.
    """

    name = "ollama"

    def __init__(
        self,
        url: str | None = None,
        admission_log: Path | None = None,
        http: HttpGet | None = None,
        *,
        servers: Sequence[ServerSpec] | None = None,
    ) -> None:
        if servers is None:
            if url is None:
                raise ValueError("ollama: a url or servers is needed")
            servers = [ServerSpec(DEFAULT_LLM_NAME, url, admission_log)]
        if not 1 <= len(servers) <= MAX_LLM_SERVERS:
            raise ValueError(f"ollama: 1 to {MAX_LLM_SERVERS} servers")
        names = [s.name.casefold() for s in servers]
        if len(set(names)) != len(names):
            raise ValueError("ollama: duplicate server name")
        self._servers = [_Server(s, _check_url(s.url)) for s in servers]
        self._http: HttpGet = http or default_http_get

    @property
    def servers(self) -> list[ServerSpec]:
        return [s.spec for s in self._servers]

    @property
    def _url(self) -> str:
        return self._servers[0].url

    @property
    def _log(self) -> Path | None:
        return self._servers[0].spec.admission_log

    def _get(self, base: str, path: str) -> object:
        resp = self._http(base + path, TIMEOUT_S, MAX_BODY)
        if resp.status != 200:
            raise RuntimeError(f"ollama {path}: HTTP {resp.status}")
        return _parse_json(resp.body)

    def _poll_one(self, srv: _Server, now: float) -> LlmServer:
        data = self._get(srv.url, "/api/ps")
        self._get(srv.url, "/api/version")
        listed = data.get("models") if isinstance(data, dict) else None
        if not isinstance(listed, list):
            raise TypeError("ollama /api/ps: unexpected shape")
        models = []
        for entry in listed[: MAX_MODELS * 2]:
            m = _model(entry)
            if m is not None:
                models.append(m)
            if len(models) >= MAX_MODELS:
                break
        queue, inflight = _admission(srv.spec.admission_log)
        return LlmServer(
            models=models, queue=queue, inflight=inflight, gpu={},
            name=srv.spec.name, last_ok=now, reachable=True,
        )

    def _fail(self, srv: _Server, exc: Exception, now: float, interval: float) -> None:
        srv.failures += 1
        srv.error = exc
        srv.next_try = now + backoff_delay(interval, srv.failures)

    def collect(self, ctx: Ctx) -> SourceResult:
        now = ctx.clock()
        interval = ctx.cfg.interval_s
        polled: list[_Server] = []
        pool = ThreadPoolExecutor(max_workers=len(self._servers), thread_name_prefix="fafo-ollama")
        try:
            for srv in self._servers:
                if srv.pending is not None and not srv.pending.done():
                    self._fail(srv, TimeoutError("previous poll still running"), now, interval)
                    continue
                if now < srv.next_try:
                    continue  # backing off: keep its last rows, poll the others
                srv.pending = pool.submit(self._poll_one, srv, now)
                polled.append(srv)
            wait([s.pending for s in polled if s.pending], timeout=TIMEOUT_S + 1.0)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        for srv in polled:
            fut = srv.pending
            if fut is None or not fut.done():
                self._fail(srv, TimeoutError("ollama poll timed out"), now, interval)
                continue
            srv.pending = None
            try:
                srv.last = fut.result()
                srv.failures = 0
                srv.error = None
                srv.next_try = 0.0
            except Exception as exc:  # noqa: BLE001 - any one server's failure is only its own
                self._fail(srv, exc, now, interval)
        errors = [s.error for s in self._servers if s.error is not None]
        if len(errors) == len(self._servers):
            raise errors[0]
        rows = []
        for srv in self._servers:
            if srv.last is None:
                rows.append(LlmServer([], 0, [], {}, name=srv.spec.name, reachable=False))
            elif srv.error is not None:
                rows.append(dataclasses.replace(srv.last, reachable=False))
            else:
                rows.append(srv.last)
        down = any(s.error is not None for s in self._servers)
        return SourceResult(llms=rows, llm=rows[0], status="stale" if down else None)
