#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4 收口的新增判据（R-08 / R-09 / L1 / R-01 / R-16 + 四栏窄条）—— 五条，每条都能红

## 为什么单开一个脚本

`accept_workbench.py`（A-1~A-13，13 条）与 `verify_workbench_fixes.py`（F-1~F-6，18 条）
是**既有判据的锚**：往上加条数会让"13/13""18/18"这两个读数失去可比性。所以 v4 这一轮的
新判据单独成册，互不覆盖：

    V1  /api/meta 200 且 capabilities 非空（R-09 ①）
    V2  「缺」在中栏/右栏显式表态，且**不出现"重试"**（R-08）
    V3  `_count_sql` 与 `_branch_selects` 共用同一份作用域谓词（R-16，"改一处即全变"）
    V4  按下态：源码 `active:` 命中 > 0 且**真的进了构建产物**（L1）
    V5  窄条页签数量 == 1（现在只有「现场」），且清单驱动（四栏）

## 变异自证（判据必须能红）

每条都配一个变异：把**同一段断言**喂给一个已知为坏的输入，必须红；红不了记
`MUTATION-FAIL`（不算 PASS）。变异只发生在进程内存与临时副本库里，**不改源码、不写真库**。

## 用法

    python -X utf8 p0\\tools\\verify_v4.py --api http://127.0.0.1:8790 --static http://127.0.0.1:8799

`--static` 缺失或浏览器起不来时，DOM 类判据（V2/V5）报 **SKIP** 并写明缺什么，
**不冒充通过**（SKIP ≠ PASS）。退出码：全 PASS 0，否则 1。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
for path in (_P0, _HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import accept_workbench as ACC  # noqa: E402  复用它的无头浏览器 / CDP / 临时读口 / 假读口

UI_DIR = os.path.join(_REPO, "ui")
SRC_DIR = os.path.join(UI_DIR, "src")
DIST_DIR = os.path.join(UI_DIR, "dist")
API_FILE = os.path.join(_P0, "fish_workbench_api.py")

RESULTS: list[tuple[str, bool]] = []


def record(cid: str, ok: bool, skipped: bool, detail: str) -> None:
    status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    RESULTS.append((cid, ok and not skipped))
    print(f"{status:<4}  {cid}  {detail}")


def http_status_json(url: str, timeout: float = 15.0) -> tuple[int, object]:
    """GET 一个 URL，返回 `(status, payload)`；4xx/5xx 也照常返回（不抛），便于判据看状态码。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001  错误体不是 JSON（例如 HTML 404 页）
            return exc.code, None


# ── 通用：无头浏览器 + 任意 JS 求值（带视口尺寸）───────────────────────────
def eval_js(url: str, expressions: dict[str, str], *, wait_ms: int = 7000,
            width: int = 1440, height: int = 900, attempts: int = 3) -> dict:
    """渲染一个页面并按 `expressions` 逐个求值；起不来抛 RuntimeError（调用方判 SKIP）。"""
    errors = []
    for _ in range(attempts):
        try:
            return _eval_js_once(url, expressions, wait_ms, width, height)
        except RuntimeError as exc:
            errors.append(str(exc))
            time.sleep(1.5)
    raise RuntimeError("；".join(errors))


def _eval_js_once(url: str, expressions: dict[str, str], wait_ms: int,
                  width: int, height: int) -> dict:
    browser = ACC.find_browser()
    if not browser:
        raise RuntimeError("找不到无头浏览器（Edge/Chrome）")
    profile = tempfile.mkdtemp(prefix="fish-v4-")
    port = ACC._free_port()
    proc = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
         "--no-default-browser-check", f"--user-data-dir={profile}",
         f"--remote-debugging-port={port}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    pages = [t for t in json.loads(resp.read().decode()) if t.get("type") == "page"]
                    if pages:
                        target = pages[0]
                        break
            except Exception:  # noqa: BLE001  调试端口还没起来
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("CDP 目标没起来（调试端口绑不上）")
        ws = ACC._Ws(target["webSocketDebuggerUrl"])
        try:
            ws.call(1, "Page.enable")
            ws.call(2, "Runtime.enable")
            ws.call(3, "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1,
                     "mobile": False})
            ws.call(4, "Page.navigate", {"url": url})
            time.sleep(wait_ms / 1000.0)
            out = {}
            for index, (name, expression) in enumerate(expressions.items(), start=10):
                result = ws.call(index, "Runtime.evaluate",
                                 {"expression": expression, "returnByValue": True})
                out[name] = result.get("result", {}).get("value")
            if not (out.get("text") or "").strip():
                raise RuntimeError("页面文本为空")
            return out
        finally:
            ws.close()
    finally:
        ACC._kill_tree(proc)
        shutil.rmtree(profile, ignore_errors=True)


TEXT_PROBE = {"text": "document.body ? document.body.innerText : ''"}


# ── V1：`/api/meta` 200 且 capabilities 非空（R-09 ①）─────────────────────
META_KEYS = ("schema", "schemas", "capabilities", "db", "readOnly", "serverPid")


def meta_ok(payload, status: int = 200) -> tuple[bool, str]:
    """判据本身：状态 200 + 六个字段齐 + `capabilities` 非空且都是字符串。"""
    if status != 200 or not isinstance(payload, dict):
        return False, f"status={status} payload={type(payload).__name__}"
    missing = [key for key in META_KEYS if key not in payload]
    caps = payload.get("capabilities")
    caps_ok = isinstance(caps, list) and len(caps) > 0 and all(isinstance(c, str) for c in caps)
    db_ok = isinstance(payload.get("db"), dict) and "index" in payload["db"] and "corpus" in payload["db"]
    ok = not missing and caps_ok and db_ok and payload.get("readOnly") is True
    return ok, json.dumps({"missingKeys": missing, "capabilities": caps,
                           "dbKeys": sorted(payload.get("db") or {}),
                           "readOnly": payload.get("readOnly"),
                           "serverPid": payload.get("serverPid")}, ensure_ascii=False)


def v1(api: str) -> tuple[bool, bool, str]:
    try:
        status, payload = http_status_json(f"{api}/api/meta")
    except Exception as exc:  # noqa: BLE001
        return False, False, f"请求 /api/meta 失败：{type(exc).__name__}: {exc}"
    ok, detail = meta_ok(payload, status)
    return ok, False, detail


def v1_mut(api: str) -> tuple[bool, str]:
    """变异：换一个**不存在的端点**取同样的六字段 → 判据必须红。

    另加两条构造：`capabilities` 空数组、`readOnly` 被改成 false——分别证明
    "非空"和"只读"这两条都在真的判定，不是摆设。
    """
    status, payload = http_status_json(f"{api}/api/nope")
    ok404, _ = meta_ok(payload, status)
    ok_empty, _ = meta_ok({"schema": "x", "schemas": ["x"], "capabilities": [],
                           "db": {"index": "i", "corpus": None}, "readOnly": True,
                           "serverPid": 1}, 200)
    ok_rw, _ = meta_ok({"schema": "x", "schemas": ["x"], "capabilities": ["a"],
                        "db": {"index": "i", "corpus": None}, "readOnly": False,
                        "serverPid": 1}, 200)
    red = (not ok404) and (not ok_empty) and (not ok_rw)
    return red, json.dumps({"404Exists": "" if not ok404 else "判据仍绿",
                            "capabilitiesEmptyPassed": ok_empty,
                            "readOnlyFalsePassed": ok_rw}, ensure_ascii=False)


# ── V2：「缺」显式表态且不出现「重试」（R-08）──────────────────────────────
def missing_page_ok(text: str) -> tuple[bool, str]:
    """判据本身：中栏/右栏各说一次「缺 <cap>（不是读口没起）」，且整屏没有「重试」。"""
    need_stream = "缺 /api/stream" in text
    need_stats = "缺 /api/stats" in text
    no_retry = "重试" not in text
    ok = need_stream and need_stats and no_retry
    return ok, json.dumps({"缺 /api/stream": need_stream, "缺 /api/stats": need_stats,
                           "无重试": no_retry, "chars": len(text)}, ensure_ascii=False)


def v2(api: str, static: str) -> tuple[bool, bool, str]:
    if not static:
        return False, True, "缺 --static（V2 要在浏览器里看缺表副本的渲染）"
    tmpdir = tempfile.mkdtemp(prefix="fish-v4-missing-")
    proc = None
    try:
        missing_db = os.path.join(tmpdir, "missing.sqlite")
        ACC._copy_schema_only(ACC.DEFAULT_DB, missing_db, drop=("push_log",))
        port = ACC._free_port()
        proc = ACC._start_api(missing_db, port)
        probe = eval_js(f"{static}/?api=http://127.0.0.1:{port}", TEXT_PROBE)
        ok, detail = missing_page_ok(probe["text"])
        return ok, False, detail
    except RuntimeError as exc:
        return False, True, f"{exc}"
    finally:
        if proc is not None:
            ACC._kill_tree(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)


def v2_mut(api: str, static: str) -> tuple[bool, str]:
    """变异：把同一段断言喂给**错误态**页面（假读口回错误信封）→ 必须红。"""
    if not static:
        raise RuntimeError("缺 --static")
    server, port = ACC._start_fake("error")
    try:
        probe = eval_js(f"{static}/?api=http://127.0.0.1:{port}", TEXT_PROBE)
        ok, detail = missing_page_ok(probe["text"])
        has_retry = "重试" in probe["text"]
    finally:
        server.shutdown()
        server.server_close()
    return (not ok) and has_retry, json.dumps(
        {"错误态页面判据成立": "" if not ok else "仍绿",
         "错误态页面含重试": has_retry, "detail": json.loads(detail)}, ensure_ascii=False)


# ── V3：作用域谓词单一来源 + 改一处即全变（R-16）───────────────────────────
#: 作用域谓词的三个"签名"字面量：只允许出现在 `scope_where` 里。
#: 它们**不带外层引号**：源码里既有 `"project = ?"` 也有 f-string，按内容判比按引号判稳。
SCOPE_LITERALS = {
    "project = ?": 2,                       # tool + push（post 走 post_project_sql()）
    "project IS NULL OR project = ''": 2,   # tool + push（post 走 post_project_sql() IS NULL）
    "session_key IN": 2,                    # tool + push（event 走 _EVENT_SESSION_SQL）
}


def _source() -> str:
    with open(API_FILE, encoding="utf-8") as handle:
        return handle.read()


def _block(source: str, name: str) -> str:
    """截出某个顶层函数的函数体（到下一个顶层 `def ` 或 `class ` 为止）。"""
    match = re.search(rf"\ndef {name}\(", source)
    if not match:
        return ""
    rest = source[match.end():]
    end = re.search(r"\n(?=def |class |# ── )", rest)
    return rest[: end.start()] if end else rest


def _strip_docs(text: str) -> str:
    """去掉三引号文档字符串——判据数的是**代码**，不是文档里引用的字样。"""
    return re.sub(r'"""[\s\S]*?"""', "", text)


def scope_single_source(source: str) -> tuple[bool, str]:
    """判据本身：**一处定义**（`scope_where`），两处调用（取行 / 计数），外面没有副本。

    三个作用域字面量在 `scope_where` 里各 2 处（tool + push；post/event 走共享表达式），
    在**函数体之外 0 处**——外面再出现一次就是又写了一份谓词（R-16 的原症状）。
    文档字符串里引用这些字样不算实现（先剥三引号再数）。
    """
    defined = len(re.findall(r"\ndef scope_where\(", source))
    where_block = _strip_docs(_block(source, "scope_where"))
    outside = _strip_docs(source.replace(_block(source, "scope_where"), ""))
    branch = _block(source, "_branch_selects")
    count = _block(source, "_count_sql")
    branch_calls = "scope_where(" in branch
    count_calls = "scope_where(" in count
    leaks = {}
    for literal, expected in SCOPE_LITERALS.items():
        inside = where_block.count(literal)
        leaked = outside.count(literal)
        if inside != expected or leaked:
            leaks[literal] = {"inside": inside, "expect": expected, "outside": leaked}
    ok = defined == 1 and branch_calls and count_calls and not leaks
    return ok, json.dumps({"defScopeWhere": defined, "branchCalls": branch_calls,
                           "countCalls": count_calls, "leaks": leaks}, ensure_ascii=False)


def v3(db: str) -> tuple[bool, bool, str]:
    source = _source()
    ok, detail = scope_single_source(source)
    return ok, False, detail


def v3_mut(db: str) -> tuple[bool, str]:
    """变异：**改一处**（把 `scope_where` 的项目谓词整段摘掉）→ 计数与行数必须**一起变**。

    这一条才是 R-16 的要害：如果 `_count_sql` 自己又写了一份谓词，摘掉 `scope_where` 的
    项目过滤后，行会变多而计数**不变**——两者不再相等，判据就红了。
    """
    import fish_workbench_api as A

    A._CURRENT_DB = db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    original = A.scope_where

    def stripped(scope_kind, project, board_id, since_):
        where = original(scope_kind, project, board_id, since_)
        if scope_kind == "project":
            # 只留第一条（`*_ts >= ?` 的时间谓词），把项目谓词摘掉。
            return {name: (preds[:1], args[:1]) for name, (preds, args) in where.items()}
        return where

    conn = A.open_conn(db)
    try:
        def measure() -> tuple[int, int]:
            # 计数缓存（`streamcount`，TTL 10s）会把基线那次的总数复用给变异那次——
            # 那样"行数没变"不是谓词没共享，而是读到了缓存。测完一次就清空。
            A._CACHE.clear()
            sql, params = A._count_sql("project", ACC.PROJECT, None, since)
            counted = sum(int(r[0]) for r in conn.execute(sql, params).fetchall())
            stream = A.collect_stream(conn, scope_kind="project", project=ACC.PROJECT,
                                      board_id=None, win="24h", since=since, limit=100,
                                      num_cursor=None, direction="older")
            return counted, stream["data"]["counts"]["total"]

        base_count, base_total = measure()
        A.scope_where = stripped
        try:
            mut_count, mut_total = measure()
        finally:
            A.scope_where = original
            A._CACHE.clear()
    finally:
        conn.close()
    red = (mut_count != base_count) and (mut_total != base_total) and (mut_count == mut_total)
    return red, json.dumps({"基线": {"count": base_count, "streamTotal": base_total},
                            "变异后": {"count": mut_count, "streamTotal": mut_total},
                            "一起变了": mut_count != base_count and mut_total != base_total,
                            "仍相等（同一份谓词）": mut_count == mut_total},
                           ensure_ascii=False)


# ── V4：按下态（L1）───────────────────────────────────────────────────────
ACTIVE_RE = re.compile(r"active:")
#: 点了应该有反馈的位置（缺一个都算漏；判据取"至少 6 处 + 这 5 个文件里有"）。
ACTIVE_FILES = ("components/NavRail.tsx", "components/Tabs.tsx", "views/workbench/Rail.tsx",
                "views/workbench/Stream.tsx", "components/StreamRow.tsx")


def active_hits(source: str) -> int:
    return len(ACTIVE_RE.findall(source))


def v4(ui_dir: str = UI_DIR) -> tuple[bool, bool, str]:
    src = os.path.join(ui_dir, "src")
    files = {}
    for root, _dirs, names in os.walk(src):
        for name in names:
            if not name.endswith((".tsx", ".ts")):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                hits = active_hits(handle.read())
            if hits:
                files[os.path.relpath(path, src).replace("\\", "/")] = hits
    total = sum(files.values())
    missing_files = [name for name in ACTIVE_FILES if name not in files]
    # 构建产物里必须有 `:active` 变体（否则只是写在源码里、Tailwind 没生成）
    css = ""
    assets = os.path.join(ui_dir, "dist", "assets")
    if os.path.isdir(assets):
        for name in os.listdir(assets):
            if name.endswith(".css"):
                with open(os.path.join(assets, name), encoding="utf-8") as handle:
                    css += handle.read()
    built = ":active" in css
    ok = total >= 6 and not missing_files and built
    return ok, False, json.dumps({"hits": total, "files": files,
                                  "missingFiles": missing_files,
                                  "distHasActiveVariant": built}, ensure_ascii=False)


def v4_mut(ui_dir: str = UI_DIR) -> tuple[bool, str]:
    """变异：把同一段计数喂给一个**没有** `active:` 的文件（`codes.json`）→ 必须 0。"""
    with open(os.path.join(ui_dir, "codes.json"), encoding="utf-8") as handle:
        hits = active_hits(handle.read())
    return hits < 6, json.dumps({"与判据同口径喂坏输入（codes.json）": hits}, ensure_ascii=False)


# ── V5：窄条页签数量 == 1（四栏）──────────────────────────────────────────
NARROW_PROBE = {
    **TEXT_PROBE,
    "navTabs": "document.querySelectorAll('nav[aria-label=\"顶级页签\"] [role=\"tab\"]').length",
    "pageNodes": "document.querySelectorAll('[data-page]').length",
    # 只取按钮里**最后一段文字**（第一个 span 是装饰字符，`aria-hidden`），
    # 否则 `innerText` 会把符号一起带出来（实测 "◉\n现场"）。
    "navLabels": "Array.from(document.querySelectorAll('nav[aria-label=\"顶级页签\"] [role=\"tab\"]')).map(function(n){var s=n.querySelectorAll('span');return (s.length?s[s.length-1].innerText:n.innerText).trim()}).join('|')",
    "allTabs": "document.querySelectorAll('[role=\"tab\"]').length",
    "selected": "document.querySelectorAll('nav[aria-label=\"顶级页签\"] [role=\"tab\"][aria-selected=\"true\"]').length",
    "columns": "getComputedStyle(document.querySelector('[data-page]').closest('.grid')).gridTemplateColumns",
}


def narrow_ok(probe: dict) -> tuple[bool, str]:
    """判据本身：窄条里**只有 1 个**页签、名字是「现场」、且有选中态。"""
    nav_tabs = probe.get("navTabs")
    page_nodes = probe.get("pageNodes")
    labels = (probe.get("navLabels") or "").strip("|")
    selected = probe.get("selected")
    ok = (nav_tabs == 1 and page_nodes == 1 and labels == "现场" and selected == 1
          and "工作台" not in labels)
    return ok, json.dumps({"navTabs": nav_tabs, "pageNodes": page_nodes, "labels": labels,
                           "selected": selected, "columns": probe.get("columns")},
                          ensure_ascii=False)


def v5(api: str, static: str) -> tuple[bool, bool, str]:
    if not static:
        return False, True, "缺 --static（V5 要在浏览器里数窄条页签）"
    try:
        probe = eval_js(f"{static}/?api={api}", NARROW_PROBE, wait_ms=7000)
    except RuntimeError as exc:
        return False, True, f"{exc}"
    ok, detail = narrow_ok(probe)
    return ok, False, detail


def v5_mut(api: str, static: str) -> tuple[bool, str]:
    """变异：把同一段断言喂给「右栏那两个也数进来」（`allTabs`）→ 必须红。

    这也顺手证明判据的作用域是**窄条**，不是"页面上所有页签"。
    """
    if not static:
        raise RuntimeError("缺 --static")
    probe = eval_js(f"{static}/?api={api}", NARROW_PROBE, wait_ms=7000)
    spoofed = dict(probe)
    spoofed["navTabs"] = probe.get("allTabs")
    ok_spoofed, detail = narrow_ok(spoofed)
    return not ok_spoofed, json.dumps({"allTabs": probe.get("allTabs"),
                                       "窄条数": probe.get("navTabs"),
                                       "换成全页页签后判据": "仍绿（无判别力）" if ok_spoofed else "红"},
                                      ensure_ascii=False)


# ── V6：窗口空 ≠ 库空（QA K1：左栏与中栏同一判据）──────────────────────────
def window_empty_db(source_db: str, target: str, hours: int = 24) -> dict:
    """整库副本 + 删掉近 N 小时的行 = 「窗口里 0 行、库里还有旧数据」（K1 的现场）。

    先卸掉触发器：真库的 `events` 有 `events are immutable` 约束（设计如此，不改真库），
    副本上要能删才能造出这个状态。
    """
    shutil.copyfile(source_db, target)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(target)
    try:
        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]
        for name in triggers:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        deleted = {}
        for table, column in (("tool_activity", "seen_at"), ("board_posts", "created_at"),
                              ("push_log", "pushed_at"), ("events", "ts")):
            deleted[table] = conn.execute(
                f"DELETE FROM {table} WHERE {column} >= ?", (cutoff,)).rowcount
        conn.commit()
        left = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("tool_activity", "board_posts", "push_log", "events")}
    finally:
        conn.close()
    return {"cutoff": cutoff, "deleted": deleted, "left": left}


def window_empty_ok(text: str) -> tuple[bool, str]:
    """判据本身：窗口空的页面上**不许**出现「库空」句，**必须**出现「窗口空」句。"""
    codes = ACC.CODES
    empty_db = codes["emptyDb"][:10]          # 「这个库还是空的」
    window_empty = codes["windowEmpty"][:12]  # 「这个时间窗里没有任何行为记录」
    filtered = codes["filteredEmpty"][:8]     # 「这个切片里还没有…」
    ok = (empty_db not in text) and (window_empty in text) and (filtered not in text)
    return ok, json.dumps({"库空句(须无)": empty_db not in text,
                           "窗口空句(须有)": window_empty in text,
                           "筛选空态(须无)": filtered not in text,
                           "chars": len(text)}, ensure_ascii=False)


def v6(api: str, static: str, db: str) -> tuple[bool, bool, str]:
    if not static:
        return False, True, "缺 --static（V6 要在浏览器里看窗口空的渲染）"
    tmpdir = tempfile.mkdtemp(prefix="fish-v4-window-")
    proc = None
    try:
        target = os.path.join(tmpdir, "window-empty.sqlite")
        made = window_empty_db(db, target)
        port = ACC._free_port()
        proc = ACC._start_api(target, port)
        probe = eval_js(f"{static}/?api=http://127.0.0.1:{port}", TEXT_PROBE, wait_ms=8000)
        ok, detail = window_empty_ok(probe["text"])
        payload = json.loads(detail)
        payload["构造"] = {"删掉": made["deleted"], "窗口内剩余": made["left"]}
        return ok, False, json.dumps(payload, ensure_ascii=False)
    except RuntimeError as exc:
        return False, True, f"{exc}"
    finally:
        if proc is not None:
            ACC._kill_tree(proc)
        shutil.rmtree(tmpdir, ignore_errors=True)


def v6_mut(api: str, static: str, db: str) -> tuple[bool, str]:
    """变异：把「库空」那一句**拼回**窗口空页面的文本里 → 判据必须红。"""
    if not static:
        raise RuntimeError("缺 --static")
    spoofed = "这个时间窗里没有任何行为记录（构造的页面文本）" + ACC.CODES["emptyDb"]
    ok, _ = window_empty_ok(spoofed)
    return not ok, json.dumps({"拼回库空句后判据": "仍绿（无判别力）" if ok else "红"},
                              ensure_ascii=False)


# ── main ────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="v4 收口新增判据（只读）")
    parser.add_argument("--api", default="http://127.0.0.1:8790")
    parser.add_argument("--static", default="http://127.0.0.1:8799")
    parser.add_argument("--db", default=ACC.DEFAULT_DB)
    args = parser.parse_args()
    api = args.api.rstrip("/")
    static = (args.static or "").rstrip("/")

    print(f"# 判定时刻 {ACC.iso_now()} · API {api} · 静态 {static or '(无)'} · 库 {args.db}")
    dist_index = os.path.join(DIST_DIR, "index.html")
    if os.path.exists(dist_index):
        print(f"# dist/index.html mtime "
              f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(os.path.getmtime(dist_index)))}")

    checks = [
        ("V1 /api/meta", lambda: v1(api), lambda: v1_mut(api)),
        ("V2 缺≠错", lambda: v2(api, static), lambda: v2_mut(api, static)),
        ("V3 谓词单源", lambda: v3(args.db), lambda: v3_mut(args.db)),
        ("V4 按下态", lambda: v4(), lambda: v4_mut()),
        ("V5 窄条页签=1", lambda: v5(api, static), lambda: v5_mut(api, static)),
        ("V6 窗口空≠库空", lambda: v6(api, static, args.db),
         lambda: v6_mut(api, static, args.db)),
    ]
    mutation_fail = 0
    for name, check, mutate in checks:
        try:
            ok, skipped, detail = check()
        except Exception as exc:  # noqa: BLE001  判据自身抛错要如实报，不静默
            record(name, False, False, f"判据自身抛错：{type(exc).__name__}: {exc}")
            continue
        record(name, ok, skipped, detail)
        if skipped:
            continue
        try:
            red, mut_detail = mutate()
        except Exception as exc:  # noqa: BLE001
            print(f"MUTATION-SKIP  {name}  变异跑不起来：{type(exc).__name__}: {exc}")
            mutation_fail += 1
            continue
        if red:
            print(f"      ↳ 变异自证 OK（坏输入下必红）：{mut_detail}")
        else:
            print(f"MUTATION-FAIL  {name}  变异后仍然绿 → 判据没有判别力：{mut_detail}")
            mutation_fail += 1

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n# 汇总 {passed}/{total} PASS · 变异自证失败 {mutation_fail} 条")
    return 0 if passed == total and mutation_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
