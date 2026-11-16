# 公告缓存代理（FastAPI + 单飞回源 + 代际化清缓存）

运维发布新公告后，需要一键清掉代理上的旧缓存，并保证"慢回源"不会把已发布前的
旧公告重新写回缓存。本项目用 FastAPI 实现了单公告 GET 接口的**内存缓存代理**、
可控的本地源站，以及一个可直接操作的网页。

## 架构与端口

```
浏览器 (http://localhost:8088)
   │  静态页面 + /api/proxy/* + /api/origin/*
   ▼
 web (nginx:alpine, 容器内 :80 → 宿主 8088)
   ├── /api/proxy/*  → proxy:8000   缓存代理（本项目核心）
   └── /api/origin/* → origin:8080  本地源站（可改稿/延迟/503）

 proxy ──(条件/普通 GET)──▶ origin :8080/announcement
```

| 服务   | 宿主端口 | 职责 |
|--------|----------|------|
| origin | 8080     | 源站：提供 ETag、`Cache-Control: max-age`，支持改稿、响应延迟、503 开关 |
| proxy  | 8000     | 内存缓存代理：新鲜直返、过期条件回源、单飞、代际化清缓存 |
| web    | 8088     | nginx 操作页：读公告、清缓存、看命中/回源统计、控制源站 |

## 启动（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8088
```

三个服务都会实际启动，并带 healthcheck；`proxy` 等 `origin` 健康后启动，
`web` 等两者健康后启动。重启即冷启动：缓存与统计全部归零（纯进程内存，无持久化）。

## 接口一览

代理（proxy:8000）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/announcement` | 读公告。响应头 `X-Cache-Status`：`HIT` 新鲜命中 / `MISS` 回源取全文 / `REVALIDATED` 过期条件回源 304 沿用正文 / `ERROR` 回源失败 |
| POST | `/admin/cache/clear` | 立即清缓存（不等待任何在途慢回源），返回新代际号 |
| GET  | `/admin/stats` | 命中、回源、304、200、错误、合并等待者、清缓存次数，以及缓存/在途状态 |
| GET  | `/health` | 健康检查 |

源站（origin:8080）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/announcement` | 带 ETag、max-age；支持 `If-None-Match`，未改时返回 304 并刷新 max-age |
| POST | `/admin/edit` | `{"content": "..."}` 改稿（ETag 随之变化） |
| POST | `/admin/config` | `{"max_age": 秒, "delay": 秒, "fail_503": bool}` 任意子集 |
| GET  | `/admin/state` | 当前修订、ETag、配置与源站自身计数 |

## 缓存与并发语义（对应需求逐条）

1. **新鲜直返**：未超过源站 `max-age` 的条目直接返回，完全不回源（`HIT`）。
2. **过期条件回源**：过期后带 `If-None-Match` 回源。
   - `304`：沿用缓存正文，按响应里的新 `max-age` 刷新有效期（`REVALIDATED`）；
   - `200`：用新正文/新 ETag 整体替换条目（`MISS`）。
3. **失败不兜底旧公告**：网络错误、超时、4xx/5xx 一律返回 5xx 错误响应；
   过期条目**永远不会**被服务（读取只认新鲜条目）。旧条目仅保留其 ETag，
   便于源站恢复后继续条件回源，错误结果本身绝不入缓存。
4. **单飞（single-flight）**：同一时刻的多个 miss/重验证请求合并为一次回源，
   其余请求作为等待者挂到同一个在途组，统计里可看到 `merged_waiters`。
5. **单客户端断开无连带影响**：等待者取消只摘除自己；发起回源的后台任务
   独立运行，leader 断开也不会取消任务，其他等待者照常收到响应；在途组槽位
   由后台任务自身在结束时释放，不会残留。
6. **清缓存立即生效且与慢回源隔离（代际机制）**：
   - `clear` 只做内存操作（清空条目、代际 +1、摘挂在途组），毫秒级返回，
     **绝不 await 慢回源**；
   - 清除后新请求无法加入旧在途组，会独立发起新回源；
   - 清除前已在途的回源即使迟到完成，写回时发现代际已变，**拒绝填回缓存**；
   - 清除前已等待的客户端仍会收到它当初等待的那份响应（响应与缓存写回分离）。
7. **冷启动**：状态仅存于进程内存，重启后缓存为空、统计归零，首请求必为 MISS。

## 本地自测

仓库自带端到端自测脚本 `scripts/selftest.py`，覆盖上述 1~7 条关键链路
（冷启动重启项给出手动确认步骤）。不依赖 Docker，直接对两个 uvicorn 进程跑：

```bash
# 终端 A：源站
pip install -r origin/requirements.txt
uvicorn app.main:app --app-dir origin --host 127.0.0.1 --port 8080

# 终端 B：代理（ORIGIN_URL 可覆盖默认的 Docker 内网地址）
pip install -r proxy/requirements.txt
ORIGIN_URL=http://127.0.0.1:8080/announcement \
  uvicorn app.main:app --app-dir proxy --host 127.0.0.1 --port 8000

# 终端 C：自测
python scripts/selftest.py
```

网页手工验证流程：把源站 `delay` 设为 5 秒 → 连点两次"读取公告"观察单飞 →
请求在途时点"清除缓存"（立即返回）→ 再点"读取公告"会独立回源 → 等待数秒后
刷新统计，确认旧迟到响应没有写回（代际号保持清除后的值）。

## 目录结构

```
docker-compose.yml
proxy/            缓存代理（核心逻辑见 proxy/app/main.py）
origin/           可控本地源站
web/              nginx 镜像与操作页（web/html/index.html）
scripts/selftest.py  端到端自测
```
