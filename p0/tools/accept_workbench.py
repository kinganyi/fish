#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A-1~A-13 的可执行验收（`docs/96-工作台-产品规格-20261123.md` §十）—— 评审 R-02 的落点

## 这份脚本回答什么

`verify_workbench_fixes.py` 覆盖的是 QA 的 F-1~F-6（深翻页/性能/折叠/项目归属/只读证据）。
`docs/96` §十那 13 条产品判据（A-1~A-13）此前**一条机检都没有**（评审 R-02 原文：
"96 的 A-1~A-13 没有一条机检"）。这个脚本把 13 条逐条落成断言：

    A-1  五源都在首屏        五源计数 > 0，且首屏 40 条里 ≥3 种类型
    A-2  无假数据            读口不可达时整屏只有说明，可见文本里**没有数字**
    A-3  同毫秒顺序稳定       同一请求跑 3 次 key 序列完全一致，且 sk 严格递减
    A-4  游标无重无漏         limit=40 翻到底：rawSum == 本刀条数、零重复、游标单调
    A-5  「在线」不撒谎       窗口 30→1440 分钟数字必须变；量词必须是「会话」不是「人」
    A-6  不假装有读动作       全量条目文本里没有「读了/读取/阅读」这类编出来的动词
    A-7  点击联动             深链选中态右栏出现该条的字段组；无选中时不出现
    A-8  左栏筛选真生效       scope=project:CZHY 的每条 project 都是 CZHY（再点取消=all 会混）
    A-9  折叠可展开           折叠条带得动明细（count==len(items) 且可展开）
    A-10 空态四句不同         四句两两不同；①在渲染后的页面里，②③④在构建产物里
    A-11 首屏与滚动性能       首屏服务端调用中位 <1s；虚拟滚动下 DOM 行节点 <200 而流里有上千条
    A-12 每个数字有出处       页签一每块都有「来源+口径」入口（ⓘ），机检 DOM 计数 ≥5
    A-13 宿主来自会话键前缀   库内 host 列与前缀不一致时，界面/接口必须用前缀；大小写归一

## 变异自证（判据必须能红）

每条能变异的判据都配一个**变异运行**：把同一断言喂给一个已知为坏的输入，要求它红；
红不了就说明这条判据没有判别力，本脚本会把它记为 `MUTATION-FAIL`（不是 PASS）。
变异只发生在进程内存里（猴子补丁 / 构造字符串），**不改源码、不动库**。

## 用法

    python -X utf8 p0\\tools\\accept_workbench.py --api http://127.0.0.1:8790
    python -X utf8 p0\\tools\\accept_workbench.py --api http://127.0.0.1:8790 --db <库路径> --static http://127.0.0.1:8799
    python -X utf8 p0\\tools\\accept_workbench.py --api ... --mutate a08     # 故意破坏一条 → 必须红

`--static` 缺失或浏览器起不来时，DOM 类判据（A-2/A-7/A-11 的 DOM 半边/A-12）报 **SKIP**
并写明"需要什么"，**不冒充通过**（SKIP 与 PASS 是两件事）。

退出码：全 PASS 0；有 FAIL 1；全 SKIP 也算 1（"没验"不等于"通过"）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
if _P0 not in sys.path:
    sys.path.insert(0, _P0)

DEFAULT_DB = r"I:\app\fish-data\data\index.sqlite"
PROJECT = "CZHY"
UI_DIR = os.path.join(_REPO, "ui")
CODES_PATH = os.path.join(UI_DIR, "codes.json")
DIST_DIR = os.path.join(UI_DIR, "dist")

#: 空态文案的唯一真相（`ui/codes.json`）。A-2/A-10 用它，所以放模块级。
CODES: dict = json.load(open(CODES_PATH, encoding="utf-8")) if os.path.exists(CODES_PATH) else {}

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

RESULTS: list[tuple[str, str, str]] = []


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(cid: str, status: str, detail: str) -> None:
    RESULTS.append((cid, status, detail))
    print(f"{status:<4}  {cid}  {detail}")


# ── HTTP 小工具（只读）──────────────────────────────────────────────────
def http_json(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── DOM 探针：无头浏览器 + CDP（本机 Edge 的 --dump-dom 输出 0 字节）─────────
def find_browser() -> str | None:
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    return None


class _Ws:
    """极简 WebSocket 客户端（只为 CDP 的 `Runtime.evaluate` 服务，无第三方依赖）。

    为什么自己写：本机没有 websocket-client；而 `--dump-dom` 在 Edge 158 上输出 0 字节。
    CDP 的 HTTP `/json/*` 面不提供求值能力，`Runtime.evaluate` 只能走 WS。
    """

    def __init__(self, url: str, timeout: float = 25.0) -> None:
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手时连接断开")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0]
        if b" 101 " not in status:
            raise RuntimeError("WS 握手失败：" + status.decode("latin1"))

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        while True:
            if len(self.buf) >= 2:
                opcode = self.buf[0] & 0x0F
                length = self.buf[1] & 0x7F
                offset = 2
                if length == 126:
                    if len(self.buf) < 4:
                        self.buf += self.sock.recv(65536)
                        continue
                    length = struct.unpack(">H", self.buf[2:4])[0]
                    offset = 4
                elif length == 127:
                    if len(self.buf) < 10:
                        self.buf += self.sock.recv(65536)
                        continue
                    length = struct.unpack(">Q", self.buf[2:10])[0]
                    offset = 10
                if len(self.buf) < offset + length:
                    self.buf += self.sock.recv(65536)
                    continue
                payload = self.buf[offset:offset + length]
                self.buf = self.buf[offset + length:]
                if opcode == 0x8:
                    raise RuntimeError("WS 被对端关闭")
                if opcode in (0x1, 0x2):
                    return payload.decode("utf-8", "replace")
                continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接断开")
            self.buf += chunk

    def call(self, msg_id: int, method: str, params: dict | None = None) -> dict:
        self.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.recv_text())
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} 失败：{message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


#: 一次页面探测要取的三个量（都是机检判据的输入）。
_PROBES = {
    "text": "document.body ? document.body.innerText : ''",
    "rows": "document.querySelectorAll('[data-row]').length",
    "origins": "document.querySelectorAll('details summary[aria-label$=\"来源与口径\"]').length",
}


def dom_probe(url: str, wait_ms: int = 6000, attempts: int = 3) -> dict:
    """渲染一个页面并取回 `{text, rows, origins}`；起不来抛 RuntimeError（调用方判 SKIP）。

    为什么要重试：本机无头 Edge 偶发"调试端口 15 秒内没起来"（连续跑多条判据时更容易撞上），
    一次失败就判 SKIP 会让"环境抖一下"看起来像"功能没有"。三次都起不来才算真起不来。
    """
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            return _dom_probe_once(url, wait_ms)
        except RuntimeError as exc:
            errors.append(f"第{attempt}次：{exc}")
            time.sleep(1.5)
    raise RuntimeError("；".join(errors))


def _dom_probe_once(url: str, wait_ms: int) -> dict:
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到无头浏览器（Edge/Chrome）")
    profile = tempfile.mkdtemp(prefix="fish-wb-accept-")
    port = _free_port()
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    pages = [t for t in json.loads(resp.read().decode()) if t.get("type") == "page"]
                    if pages:
                        target = pages[0]
                        break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("CDP 目标没起来（调试端口绑不上）")
        ws = _Ws(target["webSocketDebuggerUrl"])
        try:
            ws.call(1, "Page.enable")
            ws.call(2, "Runtime.enable")
            ws.call(3, "Page.navigate", {"url": url})
            time.sleep(wait_ms / 1000.0)
            out = {}
            for index, (name, expression) in enumerate(_PROBES.items(), start=10):
                result = ws.call(index, "Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
                out[name] = result.get("result", {}).get("value")
            if not (out.get("text") or "").strip():
                raise RuntimeError("页面文本为空")
            return out
        finally:
            ws.close()
    finally:
        _kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)


def _free_port() -> int:
    """拿一个当前空闲的本地端口（临时读口/假读口都绑这个）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _kill_tree(proc: subprocess.Popen) -> None:
    """无头浏览器会派生一堆子进程；只 terminate 父进程会留下占端口的幽灵。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def visible_numbers(text: str) -> list[str]:
    """把「命令里的数字」排除后的数字——A-2 的机检口径（命令里的 `p0` 不算读数）。"""
    without_command = text.replace(CODES["unreachable"], "")
    return re.findall(r"\d", without_command)


# ── 上下文 ───────────────────────────────────────────────────────────────
def build_ctx(args) -> dict:
    import fish_workbench_api as A

    A._CURRENT_DB = args.db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    conn = A.open_conn(args.db)
    codes = json.load(open(CODES_PATH, encoding="utf-8"))
    return {"A": A, "conn": conn, "now": now, "since": since, "args": args,
            "codes": codes, "api": args.api.rstrip("/"), "static": (args.static or "").rstrip("/"),
            "db": args.db}


# ── 通用断言（返回 (ok, detail)；变异运行时同一个函数吃坏输入）───────────────
def _stream(ctx, **kwargs) -> dict:
    A = ctx["A"]
    params = {"scope_kind": "all", "project": None, "board_id": None, "win": "24h",
              "since": ctx["since"], "limit": 40, "num_cursor": None, "direction": "older"}
    params.update(kwargs)
    return A.collect_stream(ctx["conn"], **params)


def walk_pages(ctx, limit: int = 40, max_pages: int = 200) -> dict:
    """翻到底，返回覆盖读数（与 verify 的 R1 同口径）。"""
    A = ctx["A"]
    out = _stream(ctx, limit=limit)
    return _walk_from(ctx, out, limit, max_pages)


def _walk_from(ctx, out: dict, limit: int, max_pages: int) -> dict:
    A = ctx["A"]
    counts_sql, params = A._count_sql("all", None, None, ctx["since"])
    total = sum(int(r[0]) for r in ctx["conn"].execute(counts_sql, params).fetchall())
    items = out["data"]["items"]
    cursor = out["data"]["cursor"]
    keys: set[str] = set()
    raw = dup = pages = 0
    monotonic = True
    while True:
        pages += 1
        for item in items:
            if item["key"] in keys:
                dup += 1
            keys.add(item["key"])
            raw += item["fold"]["count"] if item.get("fold") else 1
        if not items or not cursor or pages >= max_pages:
            break
        nxt = _stream(ctx, limit=limit, num_cursor=cursor)
        if nxt["data"]["cursor"] and not (nxt["data"]["cursor"] < cursor):
            monotonic = False
        items, cursor = nxt["data"]["items"], nxt["data"]["cursor"]
    return {"pages": pages, "rawSum": raw, "total": total, "dup": dup, "monotonic": monotonic}


# ── A-1 ─────────────────────────────────────────────────────────────────
def a01(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    sql, params = A._count_sql("all", None, None, ctx["since"])
    rows = ctx["conn"].execute(sql, params).fetchall()
    counts = dict(zip(("tool", "post", "push", "event"), (int(r[0]) for r in rows)))
    first = _stream(ctx)["data"]["items"]
    first_cls = {item["cls"] for item in first}
    memory = ctx["conn"].execute(
        "SELECT COUNT(*) FROM events WHERE ts>=? AND type IN ('memory.recorded',"
        "'memory.accessed','memory.retired')", (ctx["since"],)).fetchone()[0]
    system = ctx["conn"].execute(
        "SELECT COUNT(*) FROM events WHERE ts>=? AND type NOT IN ('memory.recorded',"
        "'memory.accessed','memory.retired')", (ctx["since"],)).fetchone()[0]
    ok = (all(v > 0 for v in counts.values()) and memory > 0 and system > 0
          and len(first_cls) >= 3)
    detail = json.dumps({"counts": counts, "memoryEvents": int(memory),
                         "systemEvents": int(system), "firstPageCls": sorted(first_cls)},
                        ensure_ascii=False)
    return ok, detail


def a01_mut(ctx) -> tuple[bool, str]:
    """变异：把 board_posts 从合流里摘掉（评审原文的做法）→ 五源断言必须红。"""
    A = ctx["A"]
    original = A._count_sql

    def broken(*args, **kwargs):
        sql, params = original(*args, **kwargs)
        parts = sql.split(" UNION ALL ")
        # 计数串按 tool/post/push/event 四段排；第 2 段就是 board_posts，换成恒 0。
        # `scope=all` 时每段正好一个 `since` 参数，所以同步删掉第 2 个参数（否则绑定数不匹配）。
        if len(parts) >= 2 and len(params) >= 4:
            parts[1] = "SELECT 0"
            params = [p for index, p in enumerate(params) if index != 1]
        return " UNION ALL ".join(parts), params

    A._count_sql = broken
    try:
        ok, detail = a01(ctx)
    finally:
        A._count_sql = original
    return ok, detail


# ── A-2 ─────────────────────────────────────────────────────────────────
def a02(ctx) -> tuple[bool, str]:
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（需要一个静态托管地址，如 http://127.0.0.1:8799）")
    probe = dom_probe(f"{static}/?api=http://127.0.0.1:9", wait_ms=5000)
    text = probe["text"]
    has_banner = CODES["unreachable"][:6] in text
    numbers = visible_numbers(text)
    ok = has_banner and not numbers and "重试" in text
    return ok, json.dumps({"hasBanner": has_banner, "visibleNumbers": numbers,
                           "chars": len(text)}, ensure_ascii=False)


def a02_mut(ctx) -> tuple[bool, str]:
    """变异：给整屏文案里塞一个数字（评审原文的"标题里塞 1,234"）→ 必须红。"""
    text = CODES["unreachable"] + " 今天有 1,234 条行为"
    numbers = visible_numbers(text)
    # 返回的是**同一条断言**（"可见文本里没有数字"）在坏输入下的结果：必须为 False。
    return (not numbers), json.dumps({"visibleNumbers": numbers,
                                      "note": "变异方向：数字必须被检出 → 断言必须红"},
                                     ensure_ascii=False)


# ── A-3 ─────────────────────────────────────────────────────────────────
def _sk_strictly_descending(items: list[dict]) -> bool:
    keys = [item["sk"] for item in items if item.get("sk")]
    return all(a > b for a, b in zip(keys, keys[1:]))


def a03(ctx) -> tuple[bool, str]:
    runs = []
    for _ in range(3):
        out = _stream(ctx, limit=40)
        runs.append([item["key"] for item in out["data"]["items"]])
    same = runs[0] == runs[1] == runs[2]
    order_ok = _sk_strictly_descending(_stream(ctx, limit=40)["data"]["items"])
    ok = same and order_ok and len(runs[0]) > 0
    detail = json.dumps({"returned": len(runs[0]), "identical3Runs": same,
                         "skStrictlyDescending": order_ok}, ensure_ascii=False)
    return ok, detail


def a03_mut(ctx) -> tuple[bool, str]:
    """变异：把同毫秒的 tiebreak 打成随机 → 3 次顺序必须不再一致。"""
    A = ctx["A"]
    original = A._stub_item

    def scrambled(row, labels):
        item = original(row, labels)
        if item.get("sk"):
            item["sk"] = f"{item['ts']}|{item['sk'].split('|')[1]}|{random.randint(0, 999999):012d}"
        return item

    A._stub_item = scrambled
    try:
        runs = []
        for _ in range(3):
            out = _stream(ctx, limit=40)
            runs.append([item["key"] for item in out["data"]["items"]])
        ok = runs[0] == runs[1] == runs[2]
    finally:
        A._stub_item = original
    return ok, json.dumps({"identicalUnderMutation": ok}, ensure_ascii=False)


# ── A-4 ─────────────────────────────────────────────────────────────────
def a04(ctx) -> tuple[bool, str]:
    result = walk_pages(ctx, limit=40)
    ok = (result["dup"] == 0 and result["monotonic"]
          and result["rawSum"] == result["total"])
    return ok, json.dumps(result, ensure_ascii=False)


def a04_mut(ctx) -> tuple[bool, str]:
    """变异：把游标放回并集外（FISH_WB_CURSOR_PUSHDOWN=0）→ 覆盖必须明显不足。"""
    out = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child", "walk",
         "--db", ctx["db"]],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
        env={**os.environ, "FISH_WB_CURSOR_PUSHDOWN": "0"})
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"变异子进程没有输出：{out.stderr[-400:]}")
    result = json.loads(lines[-1])
    ok = (result["dup"] == 0 and result["rawSum"] == result["total"])
    return ok, json.dumps(result, ensure_ascii=False)


# ── A-5 ─────────────────────────────────────────────────────────────────
def _motion(ctx, window_min: int | None) -> dict:
    A = ctx["A"]
    original = A.MOTION_WINDOW_MIN
    if window_min is not None:
        A.MOTION_WINDOW_MIN = window_min
    try:
        return A.collect_stats(ctx["conn"], days=1, top=5)["data"]["motion"]
    finally:
        A.MOTION_WINDOW_MIN = original


def a05(ctx) -> tuple[bool, str]:
    base = _motion(ctx, 30)
    wide = _motion(ctx, 1440)
    ok = (base["quantifier"] == "会话" and base["windowMinutes"] == 30
          and base["inMotion"] != wide["inMotion"])
    return ok, json.dumps({"inMotion30": base["inMotion"], "inMotion1440": wide["inMotion"],
                           "quantifier": base["quantifier"],
                           "windowMinutes": base["windowMinutes"]}, ensure_ascii=False)


def a05_mut(ctx) -> tuple[bool, str]:
    """变异：把「在跑」写死成 1（评审原文"写死 1 → 红"）→ 两窗口必须不再有差。"""
    A = ctx["A"]
    original = A.collect_stats

    def pinned(*args, **kwargs):
        result = original(*args, **kwargs)
        result["data"]["motion"]["inMotion"] = 1
        return result

    A.collect_stats = pinned
    try:
        base, wide = _motion(ctx, 30), _motion(ctx, 1440)
    finally:
        A.collect_stats = original
    ok = base["inMotion"] != wide["inMotion"]
    return ok, json.dumps({"inMotion30": base["inMotion"], "inMotion1440": wide["inMotion"]},
                          ensure_ascii=False)


# ── A-6 ─────────────────────────────────────────────────────────────────
READ_WORDS = ("读了", "读取", "阅读", "打开文件", "查看了文件")


def _assert_no_read_verbs(items: list[dict]) -> list[str]:
    hits = []
    for item in items:
        text = item.get("text") or ""
        for word in READ_WORDS:
            if word in text:
                hits.append(f"{item.get('key')}:{word}")
        if item.get("src") == "read":
            hits.append(f"{item.get('key')}:src=read")
    return hits


def a06(ctx) -> tuple[bool, str]:
    items: list[dict] = []
    out = _stream(ctx, limit=40)
    pages = 1
    while pages <= 8:
        items.extend(out["data"]["items"])
        cursor = out["data"]["cursor"]
        if not cursor or not out["data"]["items"]:
            break
        out = _stream(ctx, limit=40, num_cursor=cursor)
        pages += 1
    hits = _assert_no_read_verbs(items)
    ok = not hits and len(items) > 0
    return ok, json.dumps({"scanned": len(items), "hits": hits[:5]}, ensure_ascii=False)


def a06_mut(ctx) -> tuple[bool, str]:
    """变异：给一条工具动作编上"读取"（评审原文"给 bash 条目加读取 → 红"）。"""
    fake = [{"key": "tool:mock", "src": "tool", "text": "某人 读取了配置文件 .env"},
            {"key": "tool:mock2", "src": "tool", "text": "某人 执行了命令"}]
    hits = _assert_no_read_verbs(fake)
    # 同一条断言在坏输入下的结果：必须为 False（有编出来的"读"就红）。
    return (not hits), json.dumps({"hits": hits}, ensure_ascii=False)


# ── A-7 ─────────────────────────────────────────────────────────────────
#: 页签二的字段组（96 §八 的八类字段，机器可检的是这些**标签**是否出现）。
LEDGER_LABELS = ["这一条", "这个人", "他的账本", "类型", "操作面 / 目标", "结果", "时间",
                 "会话", "宿主", "在不在跑", "正在干什么", "声明面", "实际面"]


def _ledger_labels_present(text: str) -> list[str]:
    return [label for label in LEDGER_LABELS if label in text]


def a07(ctx) -> tuple[bool, str]:
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（A-7 要在浏览器里看选中态的右栏）")
    # 取一个**能进详情**的 key：`fold:*` 是折叠条的显示键，`/api/entry` 只解析
    # tool|post|push|event:<id>（折叠条本身点开会展开、不会拿组键去请求详情）。
    page = http_json(f"{ctx['api']}/api/stream?win=24h&limit=20")
    candidates = [item["key"] for item in page["data"]["items"] if not item.get("fold")]
    if not candidates:
        raise RuntimeError("首屏 20 条全是折叠条，取不到可进详情的 key")
    key = candidates[0]
    url = f"{static}/?api={ctx['api']}#/w?win=24h&sel={key}&tab=ledger"
    probe = dom_probe(url, wait_ms=7000)
    present = _ledger_labels_present(probe["text"])
    ok = len(present) >= 10
    return ok, json.dumps({"key": key, "labelsPresent": len(present),
                           "missing": [x for x in LEDGER_LABELS if x not in present]},
                          ensure_ascii=False)


def a07_mut(ctx) -> tuple[bool, str]:
    """变异：指向一个不存在的 key（等于"没选中"）→ 字段组必须不出现。"""
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static")
    url = f"{static}/?api={ctx['api']}#/w?win=24h&sel=tool:99999999&tab=ledger"
    probe = dom_probe(url, wait_ms=7000)
    present = _ledger_labels_present(probe["text"])
    ok = len(present) >= 10
    return ok, json.dumps({"labelsPresentUnderMutation": len(present)}, ensure_ascii=False)


# ── A-8 ─────────────────────────────────────────────────────────────────
def a08(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
    items = out["data"]["items"]
    total = out["data"]["counts"]["total"]
    wrong = [item["key"] for item in items if item["project"] != PROJECT]
    posts = [item for item in items if item["src"] == "post"]
    posts_ok = bool(posts) and all(item["project"] == PROJECT for item in posts)
    # R-04 的第二半边：facets 的 CZHY 计数必须**包含**这些登记，且与流水"本刀 N 条"一致
    # （四处用同一个表达式，才不会出现"筛得到行、左栏数字却少一块"）。
    facets = A.collect_facets(ctx["conn"], win="24h", since=ctx["since"])
    czhy = next((p for p in facets["data"]["projects"] if p["name"] == PROJECT), None)
    facet_ok = (czhy is not None and czhy["count"] == total
                and czhy["bySource"].get("post", 0) > 0)
    ok = bool(items) and not wrong and posts_ok and facet_ok
    return ok, json.dumps({"returned": len(items), "posts": len(posts), "total": total,
                           "wrongProject": wrong[:5],
                           "facetCount": None if czhy is None else czhy["count"],
                           "facetBySource": None if czhy is None else czhy["bySource"],
                           "facetEqualsStreamTotal": facet_ok}, ensure_ascii=False)


def a08_mut(ctx) -> tuple[bool, str]:
    """变异：把看板登记的项目归属**改回 null**（评审 R-04 原文的现场）→ 必须红。

    为什么把变异放在**显示列**而不是把 SQL 整段改回 NULL：评审实测的症状是
    "`scope=project:CZHY` 的 30 行里 25 行 `project=None`"——**行还在、项目是空**。
    如果连作用域谓词一起改回 NULL，行会直接筛不出来，那就变成另一个 bug（"筛不到"），
    测不出原症状。这里只把 post 条目的项目字段抹成 None，作用域与计数都不动，
    正好复现"筛得到、显示不出来"。
    """
    A = ctx["A"]
    original = A.make_item

    def blanked(row, labels, board_names):
        item = original(row, labels, board_names)
        if item.get("src") == "post":
            item["project"] = None
        return item

    A.make_item = blanked
    try:
        ok, mutated_detail = a08(ctx)
        out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
        posts = [item for item in out["data"]["items"] if item["src"] == "post"]
        blank = [item["key"] for item in posts if item["project"] is None]
    finally:
        A.make_item = original
    return ok, json.dumps({"note": "看板登记的 project 改回 null（行仍在、项目为空）",
                           "postsReturned": len(posts),
                           "postsWithNullProject": len(blank),
                           "mutatedCheck": json.loads(mutated_detail)},
                          ensure_ascii=False)


# ── A-9 ─────────────────────────────────────────────────────────────────
def a09(ctx) -> tuple[bool, str]:
    out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
    folds = [item for item in out["data"]["items"] if item.get("fold")]
    if not folds:
        out = _stream(ctx, limit=100)
        folds = [item for item in out["data"]["items"] if item.get("fold")]
    ok = bool(folds) and all(
        item["fold"]["count"] >= 3 and len(item["fold"]["items"]) >= 3
        and all(sub.get("key") for sub in item["fold"]["items"]) for item in folds)
    detail = json.dumps({"groups": len(folds),
                         "counts": [item["fold"]["count"] for item in folds[:5]],
                         "detailLengths": [len(item["fold"]["items"]) for item in folds[:5]],
                         "truncated": [bool(item["fold"].get("truncated")) for item in folds[:5]]},
                        ensure_ascii=False)
    return ok, detail


def a09_mut(ctx) -> tuple[bool, str]:
    """变异：`FISH_WB_FOLD_MIN=9999`（折叠关掉）→ 必须一条折叠组都没有。"""
    out = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child", "folds",
         "--db", ctx["db"]],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
        env={**os.environ, "FISH_WB_FOLD_MIN": "9999"})
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"变异子进程没有输出：{out.stderr[-400:]}")
    result = json.loads(lines[-1])
    return bool(result["groups"]), json.dumps(result, ensure_ascii=False)


# ── A-10 ────────────────────────────────────────────────────────────────
def _four_distinct(codes: dict) -> list[str]:
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    return [f"{i}" for i, sentence in enumerate(sentences) if not sentence.strip()]


def a10(ctx) -> tuple[bool, str]:
    codes = ctx["codes"]
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    unique = len(set(sentences)) == 4
    bundle = ""
    for name in os.listdir(os.path.join(DIST_DIR, "assets")) if os.path.isdir(
            os.path.join(DIST_DIR, "assets")) else []:
        if name.endswith(".js"):
            bundle += open(os.path.join(DIST_DIR, "assets", name), encoding="utf-8").read()
    in_bundle = [key for key in ("emptyDb", "filteredEmpty", "windowEmpty")
                 if codes[key] in bundle]
    in_page = False
    if ctx["static"]:
        try:
            probe = dom_probe(f"{ctx['static']}/?api=http://127.0.0.1:9", wait_ms=5000)
            in_page = codes["unreachable"][:6] in probe["text"]
        except RuntimeError:
            in_page = False
    ok = unique and not _four_distinct(codes) and len(in_bundle) == 3 and in_page
    return ok, json.dumps({"fourDistinct": unique, "inBundle": in_bundle,
                           "unreachableInPage": in_page}, ensure_ascii=False)


def a10_mut(ctx) -> tuple[bool, str]:
    """变异：把两句写成同一句 → "四句不同"必须红。"""
    codes = dict(ctx["codes"])
    codes["windowEmpty"] = codes["filteredEmpty"]
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    return len(set(sentences)) == 4, json.dumps(
        {"uniqueUnderMutation": len(set(sentences))}, ensure_ascii=False)


# ── A-11 ────────────────────────────────────────────────────────────────
def a11(ctx) -> tuple[bool, str]:
    times = []
    for _ in range(5):
        started = time.perf_counter()
        _stream(ctx, limit=40)
        times.append((time.perf_counter() - started) * 1000)
    median = statistics.median(times)
    page = http_json(f"{ctx['api']}/api/stream?win=24h&limit=40")
    total = page["data"]["counts"]["total"] or 0
    rows = None
    if ctx["static"]:
        try:
            rows = dom_probe(f"{ctx['static']}/?api={ctx['api']}", wait_ms=7000)["rows"]
        except RuntimeError:
            rows = None
    rows_ok = rows is not None and rows < 200
    ok = median < 1000.0 and total >= 200 and rows_ok
    return ok, json.dumps({"medianMs": round(median, 1),
                           "min": round(min(times), 1), "max": round(max(times), 1),
                           "total": total, "domRows": rows,
                           "target": "首屏中位 <1000ms；DOM 行节点 <200 而流里有上千条"},
                          ensure_ascii=False)


def a11_mut(ctx) -> tuple[bool, str]:
    """变异：假设没虚拟滚动（DOM 行节点 = 流条数）→ 判据必须红。"""
    page = http_json(f"{ctx['api']}/api/stream?win=24h&limit=1")
    total = page["data"]["counts"]["total"] or 0
    fake_rows = total
    ok = fake_rows < 200
    return ok, json.dumps({"domRowsUnderMutation": fake_rows, "total": total},
                          ensure_ascii=False)


# ── A-12 ────────────────────────────────────────────────────────────────
def a12(ctx) -> tuple[bool, str]:
    if not ctx["static"]:
        raise RuntimeError("缺 --static（A-12 要数页签一里的 ⓘ 入口）")
    probe = dom_probe(f"{ctx['static']}/?api={ctx['api']}", wait_ms=7000)
    origins = probe["origins"] or 0
    ok = origins >= 5
    return ok, json.dumps({"originEntrances": origins, "need": ">=5（页签一五块每块一个）",
                           "textLen": len(probe["text"])}, ensure_ascii=False)


def a12_mut(ctx) -> tuple[bool, str]:
    """变异：设 DOM 里一个 ⓘ 都没有（等于"删任一处"）→ 必须红。"""
    origins = 0
    return origins >= 5, json.dumps({"originEntrances": origins}, ensure_ascii=False)


# ── A-13 ────────────────────────────────────────────────────────────────
def a13(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    norm = {"ZCode:abc": "zcode", "zcode:x": "zcode", "codebuddy:zz": "codebuddy",
            "dsh-desktop:q": "dsh-desktop"}
    norm_ok = all(A.host_from_session(key) == value for key, value in norm.items())
    row = ctx["conn"].execute(
        "SELECT id, session_key, host FROM tool_activity "
        "WHERE session_key <> '' AND host IS NOT NULL AND host <> '' "
        "AND lower(host) <> lower(substr(session_key, 1, instr(session_key, ':') - 1)) "
        "ORDER BY seen_at DESC LIMIT 1").fetchone()
    entry_ok = False
    detail = {}
    if row:
        entry = A.collect_entry(ctx["conn"], f"tool:{int(row['id'])}")
        if entry:
            api_host = entry["data"]["actor"]["host"]
            prefix = A.host_from_session(row["session_key"])
            entry_ok = api_host == prefix and api_host != (row["host"] or "").lower()
            detail = {"rowId": int(row["id"]), "dbHost": row["host"], "prefix": prefix,
                      "apiHost": api_host}
    ok = norm_ok and entry_ok
    detail["normalization"] = norm
    return ok, json.dumps(detail, ensure_ascii=False)


def a13_mut(ctx) -> tuple[bool, str]:
    """变异：宿主改回库内 host 列 → 必须红。"""
    A = ctx["A"]
    original = A.host_from_session
    A.host_from_session = lambda key: "workbuddy"  # 冒充"用库内 host 列"
    try:
        ok, _ = a13(ctx)
    finally:
        A.host_from_session = original
    return ok, json.dumps({"note": "宿主被写死成库内 host（workbuddy）"}, ensure_ascii=False)


# ── F-7 / F-8：状态构造与「partial 已删」的证据（--f78）────────────────────
def _copy_schema_only(source_db: str, target_db: str, drop: tuple[str, ...] = ()) -> None:
    """把真库的**表结构**复制成一个空库（不带一行数据），可再删掉几张表来造「缺」。

    跳过 FTS5 虚拟表：`CREATE VIRTUAL TABLE` 依赖 fts5 模块，复制过去既没必要也可能失败
    （读写口只用它在别的视图里，工作台四个端点都不碰）。
    """
    src = sqlite3.connect("file:" + source_db.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        rows = list(src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"))
    finally:
        src.close()
    dst = sqlite3.connect(target_db)
    try:
        for name, sql in rows:
            if name in drop or "VIRTUAL" in sql.upper():
                continue
            try:
                dst.execute(sql)
            except sqlite3.Error:
                pass
        dst.commit()
    finally:
        dst.close()


def _start_api(db_path: str, port: int) -> subprocess.Popen:
    """起一个**临时**读口（指向副本库）；起来后返回进程，由调用方负责杀。"""
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", os.path.join(_P0, "fish_workbench_api.py"),
         "--db", db_path, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            http_json(f"http://127.0.0.1:{port}/api/health", timeout=2)
            return proc
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    _kill_tree(proc)
    raise RuntimeError(f"临时读口没起来（db={db_path} port={port}）")


def _start_fake(mode: str) -> tuple[ThreadingHTTPServer, int]:
    """假读口（FakeProvider）：`error` = 所有请求回 400 错误信封；`slow` = 永远不返回。

    这两条**不是**产品能力，只为把 `Block` 的 `error` 与 `loading` 两个分支构造出来
    （QA F-7 要求"每个 state 至少被构造一次"）。它们跑在本进程的临时端口上，跑完关掉。
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if mode == "slow":
                time.sleep(45)   # 前端没有超时，请求挂着 → 整屏停在「正在读…」
                return
            body = json.dumps({
                "ok": False, "code": "fake_provider",
                "hint": "假读口：这条路径永远失败（只用于状态构造测试）",
                "action": "换成真读口：python p0/fish_workbench_api.py",
                "schema": "fish-workbench/1", "asOf": iso_now(),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.end_headers()

        def log_message(self, *args):  # 静音
            return

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-provider").start()
    return server, port


def _strip_ts_comments(source: str) -> str:
    """去掉 `//` 行注释与 `/* … */` 块注释——判据要判的是**代码**，不是注释里引用的示例。

    这条不是"放宽标准"：注释里出现 `availability === 'partial'` 这句话是在解释"它已被删"，
    真拿它当证据会得出反的结论（第一次跑就是这样误红了自己的说明文字）。
    """
    without_block = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


def _partial_leftovers(ctx) -> list[str]:
    """F-8：`partial` 必须是**整条删干净**，不是只在 UI 里藏起来。"""
    hits = []
    A = ctx["A"]
    if hasattr(A.Sources, "state"):
        hits.append("api:Sources.state() 仍在（会产出 partial）")
    types_src = _strip_ts_comments(
        open(os.path.join(UI_DIR, "src", "api", "types.ts"), encoding="utf-8").read())
    union = re.search(r"export type AvailState\s*=([\s\S]*?)\n\n", types_src)
    if union and "'partial'" in union.group(1):
        hits.append("ui:AvailState 仍声明 partial")
    boundary = _strip_ts_comments(
        open(os.path.join(UI_DIR, "src", "components", "AsyncBoundary.tsx"),
             encoding="utf-8").read())
    for line_no, line in enumerate(boundary.splitlines(), 1):
        if re.search(r"===\s*'partial'", line):
            hits.append(f"AsyncBoundary.tsx:{line_no} 仍在比较 partial")
    return hits


def run_states(ctx, args) -> int:
    """F-7 的"每个 state 至少被构造一次" + F-8 的"partial 已删"证据。"""
    static = ctx["static"]
    if not static:
        print("SKIP  F-7 状态构造  缺 --static（需要一个静态托管地址）")
        return 1
    if not os.path.isdir(DIST_DIR):
        print("SKIP  F-7 状态构造  缺 ui/dist（先 `cd ui; npm run build`）")
        return 1

    tmpdir = tempfile.mkdtemp(prefix="fish-wb-states-")
    procs: list[subprocess.Popen] = []
    servers: list[ThreadingHTTPServer] = []
    results: list[tuple[str, bool, str]] = []
    try:
        empty_db = os.path.join(tmpdir, "empty.sqlite")
        _copy_schema_only(ctx["db"], empty_db)
        missing_db = os.path.join(tmpdir, "missing.sqlite")
        _copy_schema_only(ctx["db"], missing_db, drop=("push_log",))
        empty_port = _free_port()
        procs.append(_start_api(empty_db, empty_port))
        missing_port = _free_port()
        procs.append(_start_api(missing_db, missing_port))
        error_server, error_port = _start_fake("error")
        servers.append(error_server)
        slow_server, slow_port = _start_fake("slow")
        servers.append(slow_server)

        cases = [
            ("ok", f"{static}/?api={ctx['api']}", 7000, "今日板"),
            ("empty", f"{static}/?api=http://127.0.0.1:{empty_port}", 7000, CODES["emptyDb"][:12]),
            ("missing", f"{static}/?api=http://127.0.0.1:{missing_port}", 7000, "该区数据未接"),
            ("error", f"{static}/?api=http://127.0.0.1:{error_port}", 7000, "这一块没读出来"),
            ("loading", f"{static}/?api=http://127.0.0.1:{slow_port}", 4000, CODES["loading"]),
        ]
        for name, url, wait_ms, marker in cases:
            try:
                probe = dom_probe(url, wait_ms=wait_ms)
                ok = marker in probe["text"]
                detail = json.dumps({"marker": marker, "chars": len(probe["text"]),
                                     "domRows": probe["rows"]}, ensure_ascii=False)
            except RuntimeError as exc:
                ok, detail = False, f"页面起不来：{exc}"
            results.append((name, ok, detail))
            print(f"{'PASS' if ok else 'FAIL'}  F-7 状态 {name:<8} {detail}")

        leftovers = _partial_leftovers(ctx)
        ok = not leftovers
        results.append(("partial", ok, json.dumps(leftovers, ensure_ascii=False)))
        print(f"{'PASS' if ok else 'FAIL'}  F-8 partial 已删   "
              f"{json.dumps({'leftovers': leftovers}, ensure_ascii=False)}")
    finally:
        for proc in procs:
            _kill_tree(proc)
        for server in servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n# F-7/F-8 小结 {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


# ── 判据表 ───────────────────────────────────────────────────────────────
CHECKS = [
    ("A-1", a01, a01_mut),
    ("A-2", a02, a02_mut),
    ("A-3", a03, a03_mut),
    ("A-4", a04, a04_mut),
    ("A-5", a05, a05_mut),
    ("A-6", a06, a06_mut),
    ("A-7", a07, a07_mut),
    ("A-8", a08, a08_mut),
    ("A-9", a09, a09_mut),
    ("A-10", a10, a10_mut),
    ("A-11", a11, a11_mut),
    ("A-12", a12, a12_mut),
    ("A-13", a13, a13_mut),
]


def child(mode: str, db: str) -> int:
    """变异子进程：在**另一个进程**里跑同一段读数（环境变量在 import 时生效）。"""
    import fish_workbench_api as A

    A._CURRENT_DB = db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    conn = A.open_conn(db)
    try:
        if mode == "walk":
            ctx = {"A": A, "conn": conn, "since": since}
            print(json.dumps(_walk_from(ctx, _stream(ctx, limit=40), 40, 200),
                             ensure_ascii=False))
            return 0
        if mode == "folds":
            out = A.collect_stream(conn, scope_kind="project", project=PROJECT,
                                   board_id=None, win="24h", since=since, limit=100,
                                   num_cursor=None, direction="older")
            folds = [item for item in out["data"]["items"] if item.get("fold")]
            print(json.dumps({"groups": len(folds)}, ensure_ascii=False))
            return 0
    finally:
        conn.close()
    raise SystemExit(f"unknown child mode: {mode}")


def _norm_id(raw: str) -> str:
    """`A-2` / `a02` / `A2` 视为同一条（命令行里两种写法都顺手）。"""
    digits = re.sub(r"\D", "", raw or "")
    return f"A{int(digits)}" if digits else (raw or "").strip().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="工作台 A-1~A-13 可执行验收（只读）")
    parser.add_argument("--api", default="http://127.0.0.1:8790")
    parser.add_argument("--static", default="http://127.0.0.1:8799")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--mutate", default="", help="故意让某条判据红（如 a08），用于自证")
    parser.add_argument("--only", default="", help="只跑某一条（如 a02）")
    parser.add_argument("--f78", action="store_true",
                        help="额外跑 F-7 状态构造（ok/empty/missing/error/loading）与 F-8 证据")
    parser.add_argument("--child", default="", help="内部用：变异子进程")
    args = parser.parse_args()

    if args.child:
        return child(args.child, args.db)

    print(f"# 判定时刻 {iso_now()} · 库 {args.db} · API {args.api} · 静态 {args.static}")
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(args.db), timezone.utc)
        print(f"# 库 mtime {mtime.strftime('%Y-%m-%dT%H:%M:%SZ')} · "
              f"size {os.path.getsize(args.db)}")
    except OSError:
        pass
    dist_index = os.path.join(DIST_DIR, "index.html")
    if os.path.exists(dist_index):
        dist_mtime = datetime.fromtimestamp(os.path.getmtime(dist_index), timezone.utc)
        print(f"# dist/index.html mtime {dist_mtime.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    ctx = build_ctx(args)
    if args.f78:
        try:
            return run_states(ctx, args)
        finally:
            ctx["conn"].close()

    mutation_fail = 0
    passed = skipped = failed = 0
    try:
        for cid, check, mutate in CHECKS:
            if args.only and _norm_id(args.only) != _norm_id(cid):
                continue
            if args.mutate and _norm_id(args.mutate) != _norm_id(cid):
                continue
            if args.mutate:
                # --mutate：直接把这条判据的**变异运行**结果当正式读数（应为红）
                try:
                    ok, detail = mutate(ctx)
                except Exception as exc:  # noqa: BLE001
                    record(cid, "SKIP", f"变异跑不起来：{type(exc).__name__}: {exc}")
                    skipped += 1
                    continue
                record(cid, "FAIL" if not ok else "PASS",
                       f"【故意破坏】{detail}")
                if not ok:
                    failed += 1
                else:
                    passed += 1
                continue
            try:
                ok, detail = check(ctx)
            except RuntimeError as exc:
                record(cid, "SKIP", f"{exc}")
                skipped += 1
                continue
            except Exception as exc:  # noqa: BLE001
                record(cid, "FAIL", f"判据自身抛错：{type(exc).__name__}: {exc}")
                failed += 1
                continue
            record(cid, "PASS" if ok else "FAIL", detail)
            passed += 1 if ok else 0
            failed += 0 if ok else 1
            # 变异自证：断言喂给坏输入必须红（红不了 = 这条判据没有判别力）
            if mutate is not None:
                try:
                    mutated_ok, mutated_detail = mutate(ctx)
                except Exception as exc:  # noqa: BLE001
                    record(cid, "MUTATION-SKIP", f"变异跑不起来：{type(exc).__name__}: {exc}")
                    mutation_fail += 1
                    continue
                if mutated_ok:
                    record(cid, "MUTATION-FAIL",
                           f"变异后仍然绿 → 判据没有判别力：{mutated_detail}")
                    mutation_fail += 1
                else:
                    print(f"      ↳ 变异自证 OK（坏输入下必红）：{mutated_detail}")
    finally:
        ctx["conn"].close()

    total = passed + failed + skipped
    print(f"\n# 汇总 {passed}/{total} PASS · {failed} FAIL · {skipped} SKIP · "
          f"变异自证失败 {mutation_fail} 条")
    return 0 if (failed == 0 and skipped == 0 and mutation_fail == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
