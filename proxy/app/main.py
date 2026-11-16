"""In-memory caching proxy for a single announcement GET endpoint.

Guarantees implemented here:

* Fresh cache entry -> served directly without touching the origin.
* Stale entry -> conditional revalidation (If-None-Match); 304 keeps the stored
  body and refreshes the validity window, 200 replaces the entry.
* Origin failure (network error, timeout, 4xx/5xx) -> error response; a stale
  entry is NEVER served as fallback.
* Concurrent misses/revalidations share one in-flight origin fetch
  ("single-flight"). A client disconnecting cancels only its own waiter; the
  shared fetch keeps running for the other waiters.
* Cache invalidation is immediate and generation-based: in-flight fetches
  started before a purge cannot write back afterwards, and post-purge requests
  cannot join the old in-flight group. Waiters that joined before the purge
  still receive the response they were waiting for.
* Process-local memory only: a restart is a cold start by design.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

ORIGIN_URL = os.environ.get("ORIGIN_URL", "http://origin:8080/announcement")
ORIGIN_TIMEOUT = float(os.environ.get("ORIGIN_TIMEOUT", "10"))
# A waiter may hang a little longer than the fetch itself (the fetch timeout is
# enforced inside the task); this only guards against lost wake-ups.
WAITER_TIMEOUT = ORIGIN_TIMEOUT + 5.0


def parse_max_age(cache_control: str | None) -> int | None:
    """Parse ``max-age=<seconds>`` from a Cache-Control header value."""
    if not cache_control:
        return None
    for part in cache_control.split(","):
        token = part.strip().lower()
        if token.startswith("max-age="):
            try:
                return max(0, int(token.split("=", 1)[1].strip()))
            except ValueError:
                return None
    return None


@dataclass
class CacheEntry:
    body: bytes
    etag: str | None
    content_type: str | None
    cache_control: str | None
    max_age: int | None
    stored_at: float
    expires_at: float

    def is_fresh(self, now: float | None = None) -> bool:
        if self.max_age is None:
            return False
        return (now or time.monotonic()) < self.expires_at

    def age(self) -> int:
        return max(0, int(time.monotonic() - self.stored_at))


@dataclass
class Inflight:
    """One shared origin fetch, possibly awaited by many clients."""

    generation: int
    waiters: list[asyncio.Future] = field(default_factory=list)
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    result: dict | None = None
    exception: BaseException | None = None


@dataclass
class State:
    entry: CacheEntry | None = None
    inflight: Inflight | None = None
    generation: int = 0
    # statistics
    hit: int = 0
    origin_requests: int = 0
    revalidations: int = 0
    not_modified: int = 0
    refreshed: int = 0
    errors: int = 0
    purges: int = 0
    merged_waiters: int = 0


state = State()
state_lock = asyncio.Lock()
client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global client
    client = httpx.AsyncClient(timeout=ORIGIN_TIMEOUT)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="announcement-cache-proxy", lifespan=lifespan)


def build_cached_response(entry: CacheEntry, status_line: str) -> Response:
    headers = {"X-Cache-Status": status_line, "Age": str(entry.age())}
    if entry.etag:
        headers["ETag"] = entry.etag
    if entry.cache_control:
        headers["Cache-Control"] = entry.cache_control
    return Response(content=entry.body, media_type=entry.content_type, headers=headers)


def origin_error(message: str) -> Response:
    return JSONResponse(
        status_code=502,
        content={"error": "origin_unavailable", "detail": message},
        headers={"X-Cache-Status": "ERROR"},
    )


async def _fetch_from_origin(entry: CacheEntry | None, gen: int) -> dict:
    """Perform the actual origin call and write back only if still current."""
    assert client is not None
    state.origin_requests += 1
    headers = {}
    if entry is not None and entry.etag is not None:
        headers["If-None-Match"] = entry.etag
        state.revalidations += 1

    try:
        upstream = await client.get(ORIGIN_URL, headers=headers)
    except httpx.HTTPError as exc:  # timeout, connect error, ...
        state.errors += 1
        return {"type": "error", "status": 502, "message": f"{type(exc).__name__}: {exc}"}

    if upstream.status_code == 304:
        if entry is None:
            # 304 without a stored body is unusable; treat as an origin error.
            state.errors += 1
            return {"type": "error", "status": 502,
                    "message": "origin returned 304 but proxy has no cached body"}
        state.not_modified += 1
        max_age = parse_max_age(upstream.headers.get("Cache-Control"))
        now = time.monotonic()
        if max_age is not None:
            entry.max_age = max_age
            entry.cache_control = upstream.headers.get("Cache-Control", entry.cache_control)
        entry.stored_at = now
        entry.expires_at = now + entry.max_age if entry.max_age is not None else now
        async with state_lock:
            # Generation check: a purge happened while we were on the wire ->
            # this late response must not fill the cache.
            written = state.generation == gen
            if written:
                state.entry = entry
        return {"type": "not_modified", "entry": entry, "written": written}

    if upstream.status_code != 200:
        state.errors += 1
        # Note: the stale entry (if any) is deliberately retained in memory so
        # its ETag can be reused for a conditional retry after the origin
        # recovers, but it is NEVER served: is_fresh() gates every read, and
        # write-back only happens for 200/304. Failed revalidation thus never
        # turns stale content into a response.
        return {
            "type": "error",
            "status": 502,
            "message": f"origin returned HTTP {upstream.status_code}",
        }

    max_age = parse_max_age(upstream.headers.get("Cache-Control"))
    now = time.monotonic()
    new_entry = CacheEntry(
        body=upstream.content,
        etag=upstream.headers.get("ETag"),
        content_type=upstream.headers.get("content-type"),
        cache_control=upstream.headers.get("Cache-Control"),
        max_age=max_age,
        stored_at=now,
        expires_at=now + max_age if max_age is not None else now,
    )
    state.refreshed += 1
    async with state_lock:
        # Stale generation (purge happened mid-fetch): do not pollute the
        # post-purge cache.
        written = state.generation == gen
        if written:
            state.entry = new_entry
    return {"type": "full", "entry": new_entry, "written": written}


async def _run_inflight(inf: Inflight, entry: CacheEntry | None) -> None:
    """Task wrapper: own the inflight slot's lifecycle and wake every waiter.

    The slot is released by this task itself, so a leader client disconnecting
    cannot strand waiting clients or leave a completed group registered.
    """
    try:
        inf.result = await _fetch_from_origin(entry, inf.generation)
    except BaseException as exc:  # noqa: BLE001 - propagate to every waiter
        inf.exception = exc
    finally:
        async with state_lock:
            if state.inflight is inf:
                state.inflight = None
        inf.done_event.set()
        for waiter in inf.waiters:
            if not waiter.done():
                waiter.set_result(None)


def _result_to_response(result: dict) -> Response:
    rtype = result["type"]
    if rtype == "error":
        return origin_error(result["message"])
    if rtype == "not_modified":
        return build_cached_response(result["entry"], "REVALIDATED")
    return build_cached_response(result["entry"], "MISS")


@app.get("/announcement")
async def get_announcement(request: Request) -> Response:
    async with state_lock:
        entry = state.entry
        if entry is not None and entry.is_fresh():
            state.hit += 1
            return build_cached_response(entry, "HIT")

        inf = state.inflight
        if inf is not None:
            # Join the shared in-flight fetch instead of starting a new one.
            waiter: asyncio.Future = asyncio.get_running_loop().create_future()
            inf.waiters.append(waiter)
            state.merged_waiters += 1
        else:
            inf = Inflight(generation=state.generation)
            inf.task = asyncio.create_task(_run_inflight(inf, entry))
            state.inflight = inf
            waiter = None

    if waiter is not None:
        try:
            await asyncio.wait_for(waiter, timeout=WAITER_TIMEOUT)
        except asyncio.CancelledError:
            # This single client went away. Detach only its own waiter; the
            # shared fetch and the other waiters continue unaffected.
            inf.waiters.remove(waiter)
            raise
        except asyncio.TimeoutError:
            inf.waiters.remove(waiter)
            return origin_error("timed out waiting for shared in-flight origin fetch")
        if inf.exception is not None:
            raise inf.exception
        assert inf.result is not None
        return _result_to_response(inf.result)

    # Leader path: wait on an event, never on the task itself, so cancelling
    # this request (client disconnect) does not cancel the shared fetch.
    try:
        await inf.done_event.wait()
    except asyncio.CancelledError:
        raise
    if inf.exception is not None:
        raise inf.exception
    assert inf.result is not None
    return _result_to_response(inf.result)


@app.post("/admin/cache/clear")
async def clear_cache() -> JSONResponse:
    """Purge immediately. Never awaits any in-flight (possibly slow) fetch."""
    async with state_lock:
        state.entry = None
        state.generation += 1
        state.purges += 1
        # Detach the old in-flight group: post-purge requests start their own
        # fetch and can never join this one. The old task keeps running so its
        # pre-purge waiters still get the response they were waiting for, but
        # its write-back is rejected by the generation check.
        state.inflight = None
    return JSONResponse({"status": "cleared", "generation": state.generation})


@app.get("/admin/stats")
async def get_stats() -> JSONResponse:
    async with state_lock:
        entry = state.entry
        inflight = state.inflight
        payload = {
            "stats": {
                "hit": state.hit,
                "origin_requests": state.origin_requests,
                "revalidations": state.revalidations,
                "not_modified": state.not_modified,
                "refreshed": state.refreshed,
                "errors": state.errors,
                "purges": state.purges,
                "merged_waiters": state.merged_waiters,
            },
            "cache": {
                "generation": state.generation,
                "has_entry": entry is not None,
                "fresh": entry.is_fresh() if entry is not None else False,
                "etag": entry.etag if entry is not None else None,
                "age": entry.age() if entry is not None else None,
                "max_age": entry.max_age if entry is not None else None,
            },
            "inflight": {
                "active": inflight is not None,
                "generation": inflight.generation if inflight is not None else None,
                "waiters": len(inflight.waiters) if inflight is not None else 0,
            },
        }
    return JSONResponse(payload)


@app.get("/health")
async def health() -> PlainTextResponse:
    return PlainTextResponse("ok")
