"""Local origin serving a single mutable announcement.

Features used to exercise the proxy:

* GET /announcement emits ETag and Cache-Control: max-age=<n>, supports
  If-None-Match and answers 304 with refreshed max-age when unchanged.
* Admin endpoints can edit the content, change max-age, add a response delay
  (applied to every announcement response, including 304/503) and switch the
  origin into 503 mode.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi import FastAPI

app = FastAPI(title="announcement-origin")

# Mutable origin state. All admin mutations are synchronous so no locking is
# needed; the delay await happens after the state snapshot is taken.
state = {
    "content": "站点将于本周三 02:00-04:00 进行例行维护，期间服务可能短暂不可用。",
    "max_age": 30,
    "delay": 0.0,
    "fail_503": False,
    "revisions": 0,
    "revision_history": [],
}

stats = {"requests": 0, "not_modified": 0, "full_200": 0, "errors_503": 0}


def current_etag() -> str:
    digest = hashlib.sha1(f"{state['revisions']}:{state['content']}".encode()).hexdigest()[:16]
    return f'"{digest}"'


@app.get("/announcement")
async def get_announcement(request: Request):
    stats["requests"] += 1
    # Snapshot state first so delay + response are internally consistent even
    # if an admin edit arrives while we sleep.
    content = state["content"]
    max_age = state["max_age"]
    delay = state["delay"]
    fail = state["fail_503"]
    etag = current_etag()
    if delay:
        await asyncio.sleep(delay)

    if fail:
        stats["errors_503"] += 1
        return PlainTextResponse(
            "origin unavailable (simulated 503)",
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    cached_etags = request.headers.get("if-none-match")
    if cached_etags and etag in [t.strip() for t in cached_etags.split(",")]:
        stats["not_modified"] += 1
        # 304 still carries the refreshed validity window.
        return PlainTextResponse(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": f"max-age={max_age}"},
        )

    stats["full_200"] += 1
    return JSONResponse(
        {
            "id": 1,
            "revision": state["revisions"],
            "content": content,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        headers={"ETag": etag, "Cache-Control": f"max-age={max_age}"},
    )


@app.post("/admin/edit")
async def edit(payload: dict):
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"error": "content must be a non-empty string"}, status_code=400)
    state["content"] = content
    state["revisions"] += 1
    state["revision_history"].append({"revision": state["revisions"], "content": content})
    return {"revision": state["revisions"], "etag": current_etag()}


@app.post("/admin/config")
async def config(payload: dict):
    changed = {}
    if "max_age" in payload:
        max_age = payload["max_age"]
        if not isinstance(max_age, int) or max_age < 0:
            return JSONResponse({"error": "max_age must be a non-negative int"}, status_code=400)
        state["max_age"] = max_age
        changed["max_age"] = max_age
    if "delay" in payload:
        delay = payload["delay"]
        if not isinstance(delay, (int, float)) or delay < 0:
            return JSONResponse({"error": "delay must be a non-negative number"}, status_code=400)
        state["delay"] = float(delay)
        changed["delay"] = float(delay)
    if "fail_503" in payload:
        flag = payload["fail_503"]
        if not isinstance(flag, bool):
            return JSONResponse({"error": "fail_503 must be a boolean"}, status_code=400)
        state["fail_503"] = flag
        changed["fail_503"] = flag
    return {"config": changed}


@app.get("/admin/state")
async def get_state():
    return {
        "revision": state["revisions"],
        "etag": current_etag(),
        "max_age": state["max_age"],
        "delay": state["delay"],
        "fail_503": state["fail_503"],
        "content": state["content"],
        "stats": dict(stats),
    }


@app.get("/health")
async def health():
    return PlainTextResponse("ok")
