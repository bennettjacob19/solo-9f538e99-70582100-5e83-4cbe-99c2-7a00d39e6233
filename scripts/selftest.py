#!/usr/bin/env python3
"""End-to-end self-test for the announcement cache proxy.

Drives the proxy and the local origin over HTTP (same surfaces as the Docker
stack) and verifies:

1. cold start -> MISS -> fresh HIT, HIT does not touch the origin
2. stale entry -> conditional revalidation -> 304 keeps body, refreshes TTL
3. edited content (ETag change) -> 200 replaces the cached body
4. origin 503 -> proxy error, stale announcement is never served
5. concurrent requests share one in-flight origin fetch (single-flight)
6. a client disconnecting mid-fetch neither cancels the shared fetch nor
   breaks other waiters
7. purge during a slow fetch: immediate effect, old fetch cannot write back,
   pre-purge waiter still gets its response, post-purge request re-fetches
8. cold restart starts with an empty cache

Usage: python3 -I scripts/selftest.py --proxy http://127.0.0.1:8000 \
        --origin http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  [{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def stats_proxy(client: httpx.Client, base: str) -> dict:
    return client.get(f"{base}/admin/stats").json()


def origin_req_count(client: httpx.Client, base: str) -> int:
    return client.get(f"{base}/admin/state").json()["stats"]["requests"]


def origin_config(client: httpx.Client, base: str, **kw) -> None:
    client.post(f"{base}/admin/config", json=kw).raise_for_status()


def origin_edit(client: httpx.Client, base: str, content: str) -> None:
    client.post(f"{base}/admin/edit", json={"content": content}).raise_for_status()


def proxy_clear(client: httpx.Client, base: str) -> None:
    client.post(f"{base}/admin/cache/clear").raise_for_status()


def get_announcement(client: httpx.Client, base: str, timeout: float = 15.0):
    return client.get(f"{base}/announcement", timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:8000")
    ap.add_argument("--origin", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    proxy, origin = args.proxy, args.origin

    c = httpx.Client()
    for base in (proxy, origin):
        for _ in range(50):
            try:
                if c.get(f"{base}/health").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            print(f"service {base} never became healthy")
            return 2

    print("\n== 1. 冷启动 MISS 与新鲜 HIT ==")
    origin_config(c, origin, delay=0, fail_503=False, max_age=30)
    proxy_clear(c, proxy)
    o0 = origin_req_count(c, origin)
    r = get_announcement(c, proxy)
    check("首次请求回源 MISS", r.status_code == 200 and r.headers.get("x-cache-status") == "MISS",
          f"status={r.status_code} xcs={r.headers.get('x-cache-status')}")
    body_v1 = r.json()["content"]
    r2 = get_announcement(c, proxy)
    check("二次请求新鲜 HIT", r2.headers.get("x-cache-status") == "HIT")
    check("HIT 返回相同正文", r2.json()["content"] == body_v1)
    check("HIT 不回源", origin_req_count(c, origin) == o0 + 1,
          f"origin requests {o0} -> {origin_req_count(c, origin)}")
    s = stats_proxy(c, proxy)["stats"]
    check("命中计数增加", s["hit"] >= 1 and s["origin_requests"] >= 1)

    print("\n== 2. 过期条件回源：304 沿用正文并刷新有效期 ==")
    proxy_clear(c, proxy)
    origin_config(c, origin, max_age=1)  # 短 TTL 必须在回源“之前”配置
    r = get_announcement(c, proxy)
    check("短 TTL 条目先 MISS 入缓存", r.headers.get("x-cache-status") == "MISS")
    time.sleep(1.2)
    st = stats_proxy(c, proxy)
    check("条目已标记过期", st["cache"]["has_entry"] and not st["cache"]["fresh"])
    before = origin_req_count(c, origin)
    r = get_announcement(c, proxy)
    check("过期后回源且 304 沿用正文",
          r.status_code == 200 and r.headers.get("x-cache-status") == "REVALIDATED"
          and r.json()["content"] == body_v1,
          f"xcs={r.headers.get('x-cache-status')}")
    check("源站收到一次条件请求", origin_req_count(c, origin) == before + 1)
    s = stats_proxy(c, proxy)["stats"]
    check("304/条件回源计数正确", s["not_modified"] >= 1 and s["revalidations"] >= 1)
    check("有效期被刷新（重新变新鲜）", stats_proxy(c, proxy)["cache"]["fresh"])

    print("\n== 3. 改稿后 ETag 变化，200 全文替换缓存 ==")
    new_text = f"改稿公告 v2 @ {time.time()}"
    origin_edit(c, origin, new_text)
    time.sleep(1.2)  # 等上一步刷新的 1s TTL 窗口过期
    r = get_announcement(c, proxy)
    check("改稿后回源取 200 全文",
          r.headers.get("x-cache-status") == "MISS" and r.json()["content"] == new_text,
          f"xcs={r.headers.get('x-cache-status')}")
    r2 = get_announcement(c, proxy)
    check("新全文已入缓存并 HIT",
          r2.headers.get("x-cache-status") == "HIT" and r2.json()["content"] == new_text)

    print("\n== 4. 源站 503：返回错误，不提供过期公告 ==")
    proxy_clear(c, proxy)
    origin_config(c, origin, max_age=1, fail_503=False)
    get_announcement(c, proxy)  # 先建立缓存
    time.sleep(1.2)             # 等其过期
    origin_config(c, origin, fail_503=True)
    r = get_announcement(c, proxy)
    check("过期 + 503 -> 代理 5xx 错误", r.status_code >= 500, f"status={r.status_code}")
    check("错误响应不含旧公告正文", new_text not in r.text)
    r_again = get_announcement(c, proxy)
    check("503 持续期间再次请求仍报错，绝不返回过期公告",
          r_again.status_code >= 500 and new_text not in r_again.text)
    check("错误结果未被当成新鲜缓存", not stats_proxy(c, proxy)["cache"]["fresh"])
    origin_config(c, origin, fail_503=False)
    r_recover = get_announcement(c, proxy)
    check("源站恢复后请求重新回源而非 HIT",
          r_recover.status_code == 200
          and r_recover.headers.get("x-cache-status") in ("MISS", "REVALIDATED"),
          f"xcs={r_recover.headers.get('x-cache-status')}")

    print("\n== 5. 并发读取共享一次在途回源（single-flight）==")
    proxy_clear(c, proxy)
    origin_config(c, origin, delay=3, max_age=30, fail_503=False)
    before = origin_req_count(c, origin)
    p_before = stats_proxy(c, proxy)["stats"]["origin_requests"]

    def fetch_one(_):
        with httpx.Client() as cc:
            rr = cc.get(f"{proxy}/announcement", timeout=15)
            return rr.status_code, rr.headers.get("x-cache-status"), rr.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_one, range(8)))
    check("8 个并发请求全部 200", all(x[0] == 200 for x in results))
    check("全部拿到同一正文", len({x[2] for x in results}) == 1)
    check("源站只收到 1 次回源", origin_req_count(c, origin) == before + 1,
          f"origin requests +{origin_req_count(c, origin) - before}")
    s = stats_proxy(c, proxy)["stats"]
    check("代理记录 7 个合并等待者", s["merged_waiters"] >= 7, f"merged={s['merged_waiters']}")
    check("本段仅产生 1 次代理回源", s["origin_requests"] == p_before + 1)
    r = get_announcement(c, proxy)
    check("在途结束后新请求 HIT", r.headers.get("x-cache-status") == "HIT")

    print("\n== 6. 单个客户端断开不影响在途回源与其他等待者 ==")
    proxy_clear(c, proxy)
    origin_config(c, origin, delay=3)

    def slow_fetch(timeout):
        with httpx.Client() as cc:
            try:
                return cc.get(f"{proxy}/announcement", timeout=timeout)
            except httpx.TransportError:
                return "CANCELLED"

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_cancel = pool.submit(slow_fetch, 0.5)
        time.sleep(0.6)  # 在途组建立后再加入等待者
        f_other = pool.submit(slow_fetch, 15)
        cancelled = f_cancel.result()
        other = f_other.result()
    check("短超时客户端被断开", cancelled == "CANCELLED")
    check("其他等待者仍收到 200",
          isinstance(other, httpx.Response) and other.status_code == 200,
          f"other={other if isinstance(other, str) else other.status_code}")
    time.sleep(3.5)
    st = stats_proxy(c, proxy)
    check("在途组最终被清理（无残留）", not st["inflight"]["active"])
    check("断开后正文仍正常入缓存", st["cache"]["has_entry"])

    print("\n== 7. 慢回源期间清缓存：立即生效/旧回源不能写回/等待者收原响应/新请求独立回源 ==")
    proxy_clear(c, proxy)
    origin_config(c, origin, delay=4, max_age=30)
    p_before = stats_proxy(c, proxy)["stats"]["origin_requests"]
    gen_before = stats_proxy(c, proxy)["cache"]["generation"]

    def fetch_resp():
        with httpx.Client() as cc:
            rr = cc.get(f"{proxy}/announcement", timeout=15)
            return rr.status_code, rr.text

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_old = pool.submit(fetch_resp)
        time.sleep(1.0)
        st_mid = stats_proxy(c, proxy)
        check("清缓存前存在在途回源", st_mid["inflight"]["active"])
        t0 = time.monotonic()
        cr = c.post(f"{proxy}/admin/cache/clear")
        purge_ms = (time.monotonic() - t0) * 1000
        check("清除立即返回，不等待慢回源", cr.status_code == 200 and purge_ms < 500,
              f"{purge_ms:.0f}ms")
        check("清除后缓存立即为空", not stats_proxy(c, proxy)["cache"]["has_entry"])
        check("清除使代际递增", stats_proxy(c, proxy)["cache"]["generation"] == gen_before + 1)
        r_new = get_announcement(c, proxy)
        old_status, old_body = f_old.result()
    check("清除前的等待者仍收到原响应", old_status == 200 and bool(old_body))
    check("清除后的新请求独立回源成功", r_new.status_code == 200)
    time.sleep(4.5)  # 等旧的慢回源迟到完成
    st = stats_proxy(c, proxy)
    check("无在途组残留", not st["inflight"]["active"])
    check("迟到的旧响应未填回缓存（缓存条目属于新代际）",
          st["cache"]["has_entry"] and st["cache"]["generation"] == gen_before + 1)
    s = st["stats"]
    check("清缓存计数增加", s["purges"] >= 1)
    check("本段产生且仅产生 2 次相互独立的回源（旧组 + 新请求）",
          s["origin_requests"] == p_before + 2,
          f"origin_requests 本段增量={s['origin_requests'] - p_before}")

    print("\n== 8. 重启后冷启动 ==")
    print("  （进程内状态无法自测重启；重启代理后预期 stats 全归零且首请求 MISS。")
    print("   Docker 下: docker compose restart proxy 后查看 /api/proxy/admin/stats）")
    st = stats_proxy(c, proxy)
    check("运行期统计接口持续可用", "stats" in st and "cache" in st)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败: {failures}")
        return 1
    print("全部关键链路自测通过 ✅（冷启动重启项需按提示手动确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
