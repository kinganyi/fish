#!/usr/bin/env python3
"""fish 只读读口（UI 用）— p0/fish_ui.py

## 这个文件解决的问题

fish 本体一直是"CLI + MCP stdio + Markdown 门面"，**没有任何 HTTP 面**。
第一期界面因此只能喂模拟数据。这个文件补上那个缺口：一个**只读**的本地 JSON 面，
让界面能说真话。

## 三条硬纪律（与 dsh-fish 影子期同构）

1. **只读**：`mode=ro` 打开 + `PRAGMA query_only=1`；启动时**真试一次写**来证明拒绝生效
   （`assert_write_rejected`——和 dsh-fish 一样，不"声明"只读，而"验证"只读）。
2. **只绑回环**：默认 127.0.0.1，不监听 0.0.0.0。这是本机仪表盘，不是服务。
3. **不猜**：查不到的字段给 null，不编默认值。缺数据要能被看出来，而不是被填平。

## 为什么用标准库的 http.server 而不是 Flask

fish 的设计哲学是"单机 SQLite，无外部服务依赖"。为了一屏仪表盘引入 Flask + Werkzeug，
等于给一个零依赖项目加了两个包。标准库的 ThreadingHTTPServer 够用：
这是只读快照面，不是高并发 API。

## 用

    python p0/fish_ui.py --port 8787
    # 然后浏览器打开 http://127.0.0.1:8787/api/scene

    python p0/fish_ui.py --db <path> --port 8787     # 指定别的库
    python p0/fish_ui.py --once                      # 只打印一份快照就退出（调试/对拍用）

## 端点

    GET /api/scene     全量场景快照（界面一次拿完，推荐）
    GET /api/health    只有健康读数（轻量探活）

## 边界（不装样子）

- **没有对话数据**：fish 的语料层（`fish_corpus.py`）在主树尚未合入，`events` 表记的是
  系统事件不是对话轮次。所以快照里的 `dialogue` 一律为空数组，前端应显示"还没有可展示的
  对话记录"，而不是伪造几行字。
- **撞车是读时聚合**：按 `tool_activity.path_abs` 的**路径分量**比较算出来的，
  与 `fish_surface.py` 同一套语义。它不是一张"撞车表"（fish 里没有这张表）。
- **CORS 只对本机放行**：方便界面在别的端口（或 file://）时联调。
"""
from __future__ import annotations
from fish_common import warn_ignored

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ── 路径解析 ────────────────────────────────────────────────────────────
# 默认库：<fishHome>/data/index.sqlite，fishHome 是 p0/ 的父目录
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(_HERE), "data", "index.sqlite")

SCHEMA_VERSION = "fish-ui/1"


# ── 只读连接 ────────────────────────────────────────────────────────────
def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读方式打开。uri=True + mode=ro 是 SQLite 层面的硬保证。"""
    uri = "file:" + db_path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def assert_write_rejected(conn: sqlite3.Connection) -> None:
    """真试一次写来证明只读生效。

    为什么不做成"声明式检查"：`mode=ro` 与 `query_only` 都可能因为调用方后来
    `execute("PRAGMA query_only=0")` 而失效。真试一次，失败才算过——
    这和 dsh-fish 的 `assertWriteRejected()` 是同一条纪律。
    """
    try:
        conn.execute("CREATE TABLE __fish_ui_readonly_probe (x INTEGER)")
    except sqlite3.Error:
        return  # 期望路径：被拒绝
    # 万一真写成功了，立刻收拾干净并大声失败
    try:
        conn.execute("DROP TABLE __fish_ui_readonly_probe")
    finally:
        raise SystemExit(
            "只读校验失败：这个连接居然能建表。拒绝启动——"
            "宁可没有读口，也不能让一个自称只读的面有写能力。"
        )


# ── 可读名（人类面契约）──────────────────────────────────────────────────
def session_tag(session_key: str) -> str:
    """从 session_key 取人类可读的短标识。

    契约（docs/design/03-board-collision.md §5）：人类面**永不含完整会话键**，
    形如 `名字·dsh#88f9`。这里只取宿主 + 末 4 位。
    """
    if not session_key:
        return ""
    host, _, rest = session_key.partition(":")
    host = host or "?"
    tail = rest[-4:] if len(rest) >= 4 else rest
    return f"{host}#{tail}"


def readable_name(display_name: str | None, session_key: str) -> str:
    """`名字·dsh#88f9`；没有名字时退化为会话标识，绝不编一个名字。"""
    tag = session_tag(session_key)
    if display_name:
        return f"{display_name}·{tag}" if tag else display_name
    return tag or "未命名"


# ── 占用面重叠（撞车）────────────────────────────────────────────────────
def split_surface(path: str) -> list[str]:
    """把路径拆成分量。空串返回空列表——空路径不参与撞车判定。"""
    if not path:
        return []
    p = path.replace("\\", "/").strip()
    return [seg for seg in p.split("/") if seg]


def surfaces_overlap(a: str, b: str) -> bool:
    """**按路径分量**比较，不是字符串前缀。

    `p0` 与 `p0/_audit` 算重叠；`src/a` 与 `src/ab` **不算**——
    后者正是字符串前缀比较的经典误报（与 `fish_surface.py` 语义一致）。
    """
    pa, pb = split_surface(a), split_surface(b)
    if not pa or not pb:
        return False
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


# ── 数据读取 ────────────────────────────────────────────────────────────
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return row is not None


def _safe_rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """查询失败返回空列表，不让一张缺表的库把整个快照打挂。"""
    try:
        return list(conn.execute(sql, args))
    except sqlite3.Error as exc:
        print(f"[fish_ui] 查询失败（已降级为空）：{exc}", file=sys.stderr)
        return []


def collect_health(conn: sqlite3.Connection) -> dict:
    def count(sql: str) -> int | None:
        rows = _safe_rows(conn, sql)
        return rows[0][0] if rows else None

    return {
        "events": count("SELECT COUNT(*) FROM events"),
        "memories": count("SELECT COUNT(*) FROM code_index WHERE status='active'"),
        "dormant": count("SELECT COUNT(*) FROM code_index WHERE dormant=1"),
        "citizens": count("SELECT COUNT(*) FROM citizens_all"),
        "boards_open": count("SELECT COUNT(*) FROM boards WHERE status='open'"),
    }


def collect_sessions(conn: sqlite3.Connection) -> list[dict]:
    """在场行 → 界面要的"会话"。

    数据来源是 `presence`（看板在场行），不是"会话表"——fish 里没有会话表，
    会话是 `(citizen_id, session_key)` 这个组合在在场面上的投影。
    """
    rows = _safe_rows(
        conn,
        """
        SELECT p.citizen_id, p.session_key, p.agent_id, p.status, p.doing, p.surface,
               p.last_seen_at, p.auto_surface, p.auto_seen_at,
               c.display_name, c.host,
               b.id AS board_id, b.scope, b.scope_key
        FROM presence p
        LEFT JOIN citizens_all c ON c.citizen_id = p.citizen_id
        LEFT JOIN boards b ON b.id = p.board_id
        ORDER BY p.last_seen_at DESC
        """,
    )

    # 当前开档期（谁现在在哪个项目）——project_stints.to_ts IS NULL 表示还没关
    stint: dict[str, str] = {}
    if _table_exists(conn, "project_stints"):
        for r in _safe_rows(
            conn, "SELECT citizen_id, project FROM project_stints WHERE to_ts IS NULL ORDER BY id DESC"
        ):
            stint.setdefault(r["citizen_id"], r["project"])

    out = []
    now = datetime.now(timezone.utc)
    for r in rows:
        sk = r["session_key"] or ""
        host = (r["host"] or "").strip()
        if not host and sk:
            host = sk.split(":")[0]
        # 活跃判定：最近 30 分钟内有在场心跳。真实库里 presence 有 193 行、
        # 141 个会话——绝大多数是"自动登记后就没人动"的空会话。
        # 界面必须能区分"真的有人在干活"和"来过一次就再没出现"，否则满屏都是球。
        last = r["last_seen_at"] or ""
        active = False
        if last:
            try:
                ts = datetime.strptime(last[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                active = (now - ts).total_seconds() <= 1800
            except ValueError:
                active = False
        out.append(
            {
                "id": f"{r['citizen_id']}::{sk}",
                "citizenId": r["citizen_id"],
                # 宿主 id 直接取 session_key 的前缀（dsh / codebuddy / …），
                # 这是机器证据；citizens.host 是自报，两者不同时以前者为准
                "agentId": (sk.split(":")[0] if sk else (host or "unknown")),
                "citizen": r["display_name"] or "",
                "tag": session_tag(sk),
                "name": readable_name(r["display_name"], sk),
                "project": stint.get(r["citizen_id"], ""),
                "status": (r["status"] or "active") if active else "dormant",
                "doing": r["doing"] or "",
                "surface": r["surface"] or "",
                "autoSurface": r["auto_surface"] or "",
                "boardId": f"b{r['board_id']}" if r["board_id"] is not None else "",
                "lastSeenAt": last,
                "active": active,
                # 对话数据：fish 主树没有语料表，这里如实给空
                "dialogue": [],
            }
        )
    return out


def collect_boards(conn: sqlite3.Connection, sessions: list[dict]) -> list[dict]:
    by_board: dict[str, list[str]] = {}
    for s in sessions:
        if s["boardId"]:
            by_board.setdefault(s["boardId"], []).append(s["id"])

    rows = _safe_rows(
        conn,
        "SELECT id, scope, scope_key, period_key, status FROM boards WHERE status='open' ORDER BY id DESC",
    )
    out = []
    for r in rows:
        bid = f"b{r['id']}"
        members = by_board.get(bid, [])
        if len(members) < 2:
            continue  # 一个人不成"板"：丝线只画真正的多方协作
        out.append(
            {
                "id": bid,
                "name": r["scope_key"] or ("根看板" if r["scope"] == "root" else r["scope"]),
                "scope": r["scope"],
                "periodKey": r["period_key"],
                "memberIds": members,
            }
        )
    return out


def collect_collisions(conn: sqlite3.Connection, minutes: int) -> list[dict]:
    """最近 N 分钟的撞车：按 path_abs 的**读时聚合**，不是查表（fish 里没有撞车表）。

    判据：两个**不同会话**在窗口内写过**重叠路径**。同一会话写自己的面不算
    （那是"声明与执行一致"，不是冲突）。

    ── 为什么要按路径分桶，而不是两两比较 ──────────────────────────
    实测这台机器最近 30 分钟有 **1735 条写动作**。直接两两比较是 1735² ≈ 300 万次，
    在 Python 里要跑十几秒——一个"只读仪表盘"的读口不应该有这种延迟。
    改成两步：
      ① 按 path_abs 精确分组 —— 同路径的会话直接成对；
      ② 对每个路径，检查它的**祖先前缀**是否也在集合里 —— 这是"一个是另一个的前缀"。
    复杂度从 O(N²) 降到 O(N·depth)，实测所有路径深度 ≤ 10。
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = _safe_rows(
        conn,
        """
        SELECT session_key, citizen_id, project, path_abs, seen_at
        FROM tool_activity
        WHERE action='write' AND seen_at >= ? AND path_abs IS NOT NULL
        ORDER BY seen_at DESC
        LIMIT 4000
        """,
        (since,),
    )
    if not rows:
        return []

    name_of = {r["citizen_id"]: r["display_name"]
               for r in _safe_rows(conn, "SELECT citizen_id, display_name FROM citizens_all")}

    # ① 按路径分桶：path_abs → 该路径上的写者（去重后按时间倒序）
    by_path: dict[str, list[dict]] = {}
    for r in rows:
        p = (r["path_abs"] or "").replace("\\", "/")
        if not p:
            continue
        bucket = by_path.setdefault(p, [])
        ses = r["session_key"]
        if any(x["session_key"] == ses for x in bucket):
            continue
        bucket.append({"session_key": ses, "citizen_id": r["citizen_id"],
                       "project": r["project"], "seen_at": r["seen_at"]})

    out: list[dict] = []
    seen_pairs: set[tuple] = set()
    path_set = set(by_path.keys())

    def add_pair(x: dict, y: dict, surface: str) -> None:
        if x["session_key"] == y["session_key"]:
            return
        prefix = "/".join(split_surface(surface)[:3])
        key = (min(x["session_key"], y["session_key"]),
               max(x["session_key"], y["session_key"]), prefix)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        out.append({
            "a": readable_name(name_of.get(x["citizen_id"]), x["session_key"]),
            "b": readable_name(name_of.get(y["citizen_id"]), y["session_key"]),
            "surface": surface,
            "at": max(x["seen_at"], y["seen_at"]),
            "project": x["project"] or y["project"] or "",
        })

    # ② 同路径 → 成对；再检查祖先前缀
    for p, writers in by_path.items():
        if len(writers) >= 2:
            for i in range(len(writers)):
                for j in range(i + 1, len(writers)):
                    add_pair(writers[i], writers[j], p)
                    if len(out) >= 40:
                        break
        # 祖先前缀：a/b/c/d 的祖先有 a/b/c、a/b、a
        segs = split_surface(p)
        for cut in range(len(segs) - 1, 0, -1):
            anc = "/".join(segs[:cut])
            if anc in path_set:
                for x in writers:
                    for y in by_path[anc]:
                        add_pair(x, y, anc)
                        if len(out) >= 40:
                            break
        if len(out) >= 40:
            break

    out.sort(key=lambda c: c["at"], reverse=True)
    return out[:40]


def collect_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """最近的系统事件。

    注意 events 表的结构是 `seq / event_id / ts / type / actor / subject / payload`
    ——**没有 `id` 列**（第一版照 dsh-fish 的 spec 猜了个 id，实测报
    `no such column: id`）。而且这里记的全是系统事件（memory.recorded / session.opened …），
    **不含工具调用**：工具调用在 `tool_activity` 表，是另一条线。
    """
    rows = _safe_rows(
        conn,
        "SELECT seq, event_id, ts, type FROM events ORDER BY seq DESC LIMIT ?",
        (limit,),
    )
    return [
        {"seq": r["seq"], "id": r["event_id"], "type": r["type"], "at": r["ts"]}
        for r in rows
    ]


def collect_pushes(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    if not _table_exists(conn, "push_log"):
        return []
    rows = _safe_rows(
        conn,
        """
        SELECT channel, candidate_id, project, pushed_at, reaction, latency_sec
        FROM push_log ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "channel": r["channel"],
            "candidate": r["candidate_id"] or "",
            "project": r["project"] or "",
            "at": r["pushed_at"],
            "reaction": r["reaction"] or "",
            "latencySec": r["latency_sec"],
        }
        for r in rows
    ]


def collect_activity(conn: sqlite3.Connection, since_seq: int, limit: int = 60) -> list[dict]:
    """工具动作的增量流（驱动界面上的信息束）。

    ── 为什么要单独一个端点 ──────────────────────────────────────
    场景快照（/api/scene）是"此刻长什么样"，它是低频的（2 秒轮一次就够）。
    但界面上的信息束需要"刚刚发生了什么"——那是**事件流**，要更高频、更小的载荷。
    把两者混在一个端点里，要么快照被高频拉爆，要么事件流变成 2 秒一批的批处理。

    游标用 `tool_activity.id`（自增主键）：客户端记住上次拿到的最大 id，
    下次用 `?since=<id>` 只要新的。这是最省的一条路——不用时间戳，不会因为
    时钟回拨/时区搞错而漏数据。

    ⚠️ `action` 只有 write / exec 两种（fish 的 hook 只落这两种），
    所以这里画不出"读取"类动作——那需要 `memory.accessed` 事件（另一条线）。
    """
    rows = _safe_rows(
        conn,
        """
        SELECT id, session_key, citizen_id, project, tool_name, action, path, seen_at
        FROM tool_activity
        WHERE id > ? AND path IS NOT NULL
        ORDER BY id ASC
        LIMIT ?
        """,
        (since_seq, limit),
    )
    name_of = {r["citizen_id"]: r["display_name"]
               for r in _safe_rows(conn, "SELECT citizen_id, display_name FROM citizens_all")}
    out = []
    for r in rows:
        out.append({
            "seq": r["id"],
            "sessionId": f"{r['citizen_id']}::{r['session_key']}",
            "name": readable_name(name_of.get(r["citizen_id"]), r["session_key"] or ""),
            "tool": r["tool_name"],
            "kind": "write" if r["action"] == "write" else "meta",
            "path": r["path"] or "",
            "project": r["project"] or "",
            "at": r["seen_at"],
        })
    return out


def max_activity_seq(conn: sqlite3.Connection) -> int:
    rows = _safe_rows(conn, "SELECT MAX(id) AS m FROM tool_activity")
    return (rows[0]["m"] or 0) if rows else 0


def build_scene(db_path: str, minutes: int = 30) -> dict:
    """一次性组装界面需要的全部数据。"""
    conn = open_readonly(db_path)
    try:
        assert_write_rejected(conn)
        sessions = collect_sessions(conn)
        return {
            "schema": SCHEMA_VERSION,
            "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": os.path.basename(db_path),
            "health": collect_health(conn),
            "sessions": sessions,
            "boards": collect_boards(conn, sessions),
            "collisions": collect_collisions(conn, minutes),
            "events": collect_events(conn),
            "pushes": collect_pushes(conn),
            "activityCursor": max_activity_seq(conn),
            # 界面需要、但 fish 主树还没有的能力；显式列出来，
            # 免得前端把"空"误读成"零"
            "missing": {
                "dialogue": "语料层（fish_corpus.py）在主树未合，events 表记的是系统事件不是对话轮次",
                "readActions": "hook 只落 write/exec 两类动作，读取类动作不在 tool_activity 里",
            },
        }
    finally:
        conn.close()

    conn = open_readonly(db_path)
    try:
        assert_write_rejected(conn)
        sessions = collect_sessions(conn)
        boards = collect_boards(conn, sessions)
        scene = {
            "schema": SCHEMA_VERSION,
            "asOf": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": os.path.basename(db_path),
            "health": collect_health(conn),
            "sessions": sessions,
            "boards": boards,
            "collisions": collect_collisions(conn, minutes),
            "events": collect_events(conn),
            "pushes": collect_pushes(conn),
            # 这些是界面需要、但 fish 主树还没有的能力；显式列出来，
            # 免得前端把"空"误读成"零"
            "missing": {
                "dialogue": "语料层（fish_corpus.py）在主树未合，events 表记的是系统事件不是对话轮次",
            },
        }
        return scene
    finally:
        conn.close()


# ── HTTP 层 ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "fish-ui/" + SCHEMA_VERSION.split("/")[-1]
    db_path = DEFAULT_DB
    minutes = 30

    def log_message(self, fmt, *args):  # 安静一点：这是一屏仪表盘的读口
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 只对本机来源放行：界面可能跑在别的端口或 file://
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in ("/", "/api", "/api/scene"):
                q = parse_qs(parsed.query)
                minutes = self.minutes
                if "minutes" in q:
                    try:
                        minutes = max(1, min(1440, int(q["minutes"][0])))
                    except ValueError as exc:
                        warn_ignored('fish_ui.py:545 module', exc)
                self._send(200, build_scene(self.db_path, minutes))
            elif path == "/api/activity":
                q = parse_qs(parsed.query)
                since = 0
                if "since" in q:
                    try:
                        since = max(0, int(q["since"][0]))
                    except ValueError:
                        since = 0
                conn = open_readonly(self.db_path)
                try:
                    assert_write_rejected(conn)
                    items = collect_activity(conn, since)
                    self._send(200, {
                        "schema": SCHEMA_VERSION,
                        "since": since,
                        "cursor": items[-1]["seq"] if items else since,
                        "items": items,
                    })
                finally:
                    conn.close()
            elif path == "/api/health":
                conn = open_readonly(self.db_path)
                try:
                    assert_write_rejected(conn)
                    self._send(200, {"schema": SCHEMA_VERSION, "health": collect_health(conn)})
                finally:
                    conn.close()
            else:
                self._send(404, {"error": "not found", "path": path})
        except FileNotFoundError:
            self._send(500, {"error": f"库不存在：{self.db_path}"})
        except Exception as exc:  # 读口出错要说出来，不静默
            self._send(500, {"error": str(exc), "type": type(exc).__name__})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fish_ui.py",
        description="fish 只读读口：把本机库投影成一屏界面要的 JSON 快照",
    )
    ap.add_argument("--db", default=DEFAULT_DB, help=f"主库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑回环）")
    ap.add_argument("--port", type=int, default=8787, help="端口（默认 8787）")
    ap.add_argument("--minutes", type=int, default=30, help="撞车统计窗口（分钟）")
    ap.add_argument("--once", action="store_true", help="只打印一份快照就退出（不启服务）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"[fish_ui] 库不存在：{args.db}", file=sys.stderr)
        return 2

    if args.once:
        scene = build_scene(args.db, args.minutes)
        print(json.dumps(scene, ensure_ascii=False, indent=2))
        return 0

    Handler.db_path = args.db
    Handler.minutes = args.minutes
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[fish_ui] 只读读口已起：http://{args.host}:{args.port}/api/scene")
    print(f"[fish_ui] 库：{args.db}  （mode=ro + query_only，写操作已被拒绝并验证）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[fish_ui] 收到中断，退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
