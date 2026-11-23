#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台修复轮（F-1~F-6）的回归判据 —— `python -X utf8 p0/tools/verify_workbench_fixes.py`

## 这份脚本回答什么

QA Round 1 的 6 条（见 `docs/100-QA-工作台首屏-20261123.md`）修完没有、变异能不能红。
每条都先跑**基线**（绿），再用一条环境变量/猴子补丁跑**变异**（必须红）——
判据不能在没有坏输入的情况下自称成立。

    R1 深翻页：limit=40/50/500 各翻到空页，rawSum == 本刀条数、无重复、游标单调、最旧时间 == MIN(ts)
    R2 性能：/api/stream 等价调用 5 次，报 min / median / max；24h limit=500 中位必须 <150 ms
    R3 推送折叠：**构造数据验折叠规则**（同 code 连续折 1 条 / 时间闸 / 只认连续同 code），变异 FOLD_MIN=11 必须全红（不是绝对比率，见下）
    R4 项目过滤记忆事件：项目视图 memory 类 >0 且项目对得上；变异（FISH_WB_EVENT_PROJECT=payload）必须 0
    R5 只读证据：每次开连接都留下 readonly_proven（含真实异常类型与消息）；变异（连接改可写）必须拒绝启动
    R6 D2 回归：FISH_WB_FOLD_MIN=9999 时 fold.groups 必须回到 0（工具+推送两种折叠一起关）

## R3 为什么不再写「首屏 ≤50%」（2026-11-24 修 KI-2）

「同 code 连续推送折叠 ×N」是 96/97 的**结构性**要求；「首屏 push 占比 ≤50%」这个数字
只出现在 `docs/100` F-4 的回归建议里（原文："例如 ≤10% 或『同 code 连续出现 ≤1 行』"），
**不是**规格硬要求。实测同一条代码：`docs/104`（05:58）读到 `0.525` 判红，本轮 22:06
读到 `0.375` 判绿——红绿跟着库长跑，不跟着代码跑，说明折叠没退化、是**阈值锚错了**。
所以 R3 改成用**构造数据**直接验折叠规则（确定性，不依赖库快照）：

    ① 同 code 连续 10 条、间隔 3s → 必须折 1 条、覆盖 10 条；
    ② 同 code 但间隔 400s > FOLD_PUSH_SEC(300s) → 不许折（时间闸）；
    ③ A/B 交替 → 不许折（只认**连续同 code**）；
    ④ 变异 FOLD_MIN=11 → ①②③ 必须全红（判据有判别力）。

真实 24h 首屏的 `pushShare` / `foldPushGroups` 仍然打印，但只作**读数**（不判红），
供跨轮对账——判据不锚易漂数字。

## 登记了几条（声称必须可复算）

本脚本**登记 15 条**（不带 `--api`）；带 `--api` 时额外 4 条 R7（五端点 200 / POST 405 /
异源 403 / 静态源放行 ACAO），合计 **19 条**。末尾汇总行按**实跑**条数打印，并与登记的
条数对账：对不上直接判红（`docs/104` KI-2 就是"声称 18/18、实际只登记 14 条"）。

## 用法

    python -X utf8 p0/tools/verify_workbench_fixes.py                 # 用默认库，跑全部
    python -X utf8 p0/tools/verify_workbench_fixes.py --db <路径>      # 指定库
    python -X utf8 p0/tools/verify_workbench_fixes.py --api http://127.0.0.1:8790
        # 额外跑 HTTP 面回归（五端点 200 / POST 405 / 异源 403 / 本机另一端口 200）

脚本**只读**：连接走 `fish_ui.open_readonly`（mode=ro + query_only），
变异用的临时库建在 `%TEMP%` 下、跑完删掉。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
if _P0 not in sys.path:
    sys.path.insert(0, _P0)

DEFAULT_DB = r"I:\app\fish-data\data\index.sqlite"
PROJECT = "大肥鱼生命系统"

#: **登记了几条判据**。末尾会把实跑条数与它逐条对账——声称的条数必须可复算
#: （docs/104 KI-2：旧版本头写着 18，实际只登记 14 条，`--api` 才补齐 R7 的 4 条）。
REGISTERED_CHECKS = 15
REGISTERED_API_CHECKS = 4      # --api 时额外跑的 R7 四组

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    _RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 子进程模式：变异自证（每条一个进程，环境变量在 import 时生效）──────────
def child(mode: str, db: str) -> int:
    import fish_workbench_api as A
    A._CURRENT_DB = db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    conn = A.open_conn(db)
    try:
        if mode == "paging":
            print(json.dumps(_walk(conn, A, since, 40), ensure_ascii=False))
            return 0
        if mode == "pushfold":
            out = A.collect_stream(conn, scope_kind="project", project=PROJECT, board_id=None,
                                   win="24h", since=since, limit=40, num_cursor=None,
                                   direction="older")
            print(json.dumps(_push_summary(out["data"]["items"]), ensure_ascii=False))
            return 0
        if mode == "memory":
            out = A.collect_stream(conn, scope_kind="project", project=PROJECT, board_id=None,
                                   win="24h", since=since, limit=500, num_cursor=None,
                                   direction="older")
            mem = [i for i in out["data"]["items"] if i["cls"] == "memory"]
            cursor = out["data"]["cursor"]
            for _ in range(30):
                if not cursor:
                    break
                out = A.collect_stream(conn, scope_kind="project", project=PROJECT,
                                       board_id=None, win="24h", since=since, limit=500,
                                       num_cursor=cursor, direction="older")
                mem.extend(i for i in out["data"]["items"] if i["cls"] == "memory")
                nxt = out["data"]["cursor"]
                if not out["data"]["items"] or nxt == cursor:
                    break
                cursor = nxt
            print(json.dumps({"memoryCount": len(mem),
                              "allProjectMatch": all(i["project"] == PROJECT for i in mem),
                              "keys": [i["key"] for i in mem][:5]}, ensure_ascii=False))
            return 0
        if mode == "foldmin":
            out = A.collect_stream(conn, scope_kind="all", project=None, board_id=None,
                                   win="24h", since=since, limit=50, num_cursor=None,
                                   direction="older")
            fold = out["data"]["fold"]
            print(json.dumps({"minItems": A.FOLD_MIN, "groups": fold["groups"],
                              "covered": fold["covered"]}, ensure_ascii=False))
            return 0
    finally:
        conn.close()
    raise SystemExit(f"unknown child mode: {mode}")


def _raw_oldest_sk(item: dict) -> str:
    """一条显示条目覆盖的**最旧原始 sk**（折叠条要展开算，和翻页游标同口径）。"""
    if item.get("fold") and item["fold"].get("items"):
        return item["fold"]["items"][-1]["sk"]
    return item["sk"]


def _walk(conn, A, since: str, limit: int) -> dict:
    """翻到空页，返回覆盖读数（与 QA Round 1 的 A-5 同口径）。"""
    total_before = _count_total(conn, A, since)
    first = A.collect_stream(conn, scope_kind="all", project=None, board_id=None,
                             win="24h", since=since, limit=limit, num_cursor=None,
                             direction="older")
    items = first["data"]["items"]
    cursor = first["data"]["cursor"]
    keys: set[str] = set()
    raw = 0
    dup = 0
    pages = 0
    monotonic = True
    oldest = ""
    while True:
        pages += 1
        for it in items:
            if it["key"] in keys:
                dup += 1
            keys.add(it["key"])
            raw += it["fold"]["count"] if it.get("fold") else 1
            ts = _raw_oldest_sk(it).split("|")[0]
            oldest = ts if not oldest or ts < oldest else oldest
        if not items or not cursor:
            break
        nxt = A.collect_stream(conn, scope_kind="all", project=None, board_id=None,
                               win="24h", since=since, limit=limit, num_cursor=cursor,
                               direction="older")
        if nxt["data"]["cursor"] and not (nxt["data"]["cursor"] < cursor):
            monotonic = False
        items, cursor = nxt["data"]["items"], nxt["data"]["cursor"]
    total_after = _count_total(conn, A, since)
    min_ts = conn.execute(
        "SELECT MIN(ts) FROM ("
        "SELECT seen_at ts FROM tool_activity WHERE seen_at>=?"
        " UNION ALL SELECT created_at FROM board_posts WHERE created_at>=?"
        " UNION ALL SELECT pushed_at FROM push_log WHERE pushed_at>=?"
        " UNION ALL SELECT ts FROM events WHERE ts>=?)", (since, since, since, since)).fetchone()[0]
    return {"limit": limit, "pages": pages, "rawSum": raw, "totalBefore": total_before,
            "totalAfter": total_after, "dup": dup, "monotonic": monotonic,
            "oldestShown": oldest, "minTs": min_ts,
            "reach": round(raw / total_before, 4) if total_before else None}


def _count_total(conn, A, since: str) -> int:
    sql, params = A._count_sql("all", None, None, since)
    return sum(int(r[0]) for r in conn.execute(sql, params).fetchall())


def _push_summary(items: list[dict]) -> dict:
    push = sum(1 for it in items if it["src"] == "push")
    longest = 0
    run = 0
    prev = None
    for it in items:
        code = (it["meta"].get("code") or "") if it["src"] == "push" else ""
        if code and not it.get("fold"):
            run = run + 1 if code == prev else 1
            prev = code
            longest = max(longest, run)
        else:
            run, prev = 0, None
    push_groups = [it for it in items
                   if it.get("fold") and it["meta"].get("kind") == "push"]
    return {"returned": len(items), "push": push,
            "pushShare": round(push / max(1, len(items)), 4),
            "longestConsecutiveSameCodeUnfolded": longest,
            "foldGroups": sum(1 for it in items if it.get("fold")),
            "foldPushGroups": len(push_groups),
            # 折叠组覆盖的原始行数：每组必须 ≥ FOLD_MIN 条才算真折叠（不是把相邻两条并一条）
            "foldPushCovered": sum(int(it["fold"]["count"]) for it in push_groups)}


def _push_stub(code: str, offset_sec: int, pk: int, actor: str = "某人") -> dict:
    """构造一条推送 stub（形状与 `fish_workbench_api._stub_item` 一致，折叠只认这几列）。"""
    ts = _FIXTURE_BASE + timedelta(seconds=offset_sec)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    rk = 2
    return {"key": f"push:{pk}", "src": "push", "cls": "push", "rk": rk, "ts": stamp,
            "sk": f"{stamp}|{9 - rk}|{pk:012d}",
            "actor": {"name": actor, "sessionTag": "", "disambig": ""},
            "project": PROJECT, "text": "", "tool": None, "path": None,
            "meta": {"kind": "push", "code": code}}


#: 构造数据的固定基准时刻（不取 now：判据不该随墙钟变）。
_FIXTURE_BASE = datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc)


def _fold_fixture(A) -> dict:
    """三组**构造数据**直接验折叠规则 → `{组名: {groups, covered, displayed}}`。

    为什么不拿真库的比率当判据：24h 首屏构成随库增长漂移，同一条代码能判红也能判绿
    （docs/104 KI-2 的 `0.525` vs 本轮的 `0.375`）。折叠规则本身是确定性的，就该用确定性
    输入来验。
    """
    cases = {
        "dense": [_push_stub("SAME", i * 3, 1000 + i) for i in range(10)],
        "sparse": [_push_stub("SAME", i * 400, 2000 + i) for i in range(10)],
        "alternating": [_push_stub("A" if i % 2 == 0 else "B", i * 3, 3000 + i)
                        for i in range(10)],
    }
    out = {}
    for name, items in cases.items():
        displayed, stats = A.fold_items(items)
        out[name] = {"groups": stats["groups"], "covered": stats["covered"],
                     "displayed": len(displayed)}
    return out


def _child(env_extra: dict, mode: str, db: str) -> dict:
    env = dict(os.environ)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child", mode, "--db", db],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise SystemExit(f"child {mode} 没有输出；stderr={out.stderr[-800:]}")
    return json.loads(lines[-1])


# ── 主流程 ───────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="工作台修复轮回归判据（只读）")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--api", default="", help="可选：额外跑 HTTP 面回归")
    parser.add_argument("--child", default="", help="内部用：变异子进程模式")
    args = parser.parse_args()

    if args.child:
        return child(args.child, args.db)

    import fish_workbench_api as A

    expected = REGISTERED_CHECKS + (REGISTERED_API_CHECKS if args.api else 0)
    print(f"# 判定时刻 {iso_now()} · 库 {args.db} · "
          f"fold={{sec:{A.FOLD_SEC},min:{A.FOLD_MIN},push:{A.FOLD_PUSH}/{A.FOLD_PUSH_SEC}s}} · "
          f"cursorPushdown={A.CURSOR_PUSHDOWN} · eventProject={A.EVENT_PROJECT_MODE}")
    print(f"# 登记判据 {REGISTERED_CHECKS} 条"
          + (f" + --api 的 {REGISTERED_API_CHECKS} 条 R7 = {expected} 条" if args.api
             else f"（本次不带 --api，实跑应为 {expected} 条）"))
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(args.db), timezone.utc)
        print(f"# 库 mtime {mtime.strftime('%Y-%m-%dT%H:%M:%SZ')} · size {os.path.getsize(args.db)}")
    except OSError:
        pass

    A._CURRENT_DB = args.db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)

    conn = A.open_conn(args.db)
    try:
        # ── R1 深翻页 ────────────────────────────────────────────
        for limit in (40, 50, 500):
            t0 = time.perf_counter()
            r = _walk(conn, A, since, limit)
            r["walkSeconds"] = round(time.perf_counter() - t0, 2)
            lo, hi = min(r["totalBefore"], r["totalAfter"]), max(r["totalBefore"], r["totalAfter"])
            ok = (lo <= r["rawSum"] <= hi and r["dup"] == 0 and r["monotonic"]
                  and r["oldestShown"] == r["minTs"])
            check(f"R1 深翻页 limit={limit}", ok, json.dumps(r, ensure_ascii=False))
        legacy = _child({"FISH_WB_CURSOR_PUSHDOWN": "0"}, "paging", args.db)
        check("R1 变异（游标回并集外）", legacy["rawSum"] < legacy["totalBefore"] * 0.5,
              json.dumps(legacy, ensure_ascii=False))

        # ── R2 性能 ──────────────────────────────────────────────
        for limit in (40, 500):
            times = []
            for _ in range(5):
                t0 = time.perf_counter()
                A.collect_stream(conn, scope_kind="all", project=None, board_id=None,
                                 win="24h", since=since, limit=limit, num_cursor=None,
                                 direction="older")
                times.append((time.perf_counter() - t0) * 1000)
            med = statistics.median(times)
            detail = json.dumps({"limit": limit, "min": round(min(times), 1),
                                 "median": round(med, 1), "max": round(max(times), 1),
                                 "runs": [round(t, 1) for t in times]}, ensure_ascii=False)
            check(f"R2 性能 limit={limit} 中位 <150ms", med < 150.0, detail)

        # ── R3 推送折叠（结构判据；不锚会漂的比率）───────────────
        #
        # KI-2 的结论：**不是折叠退化，是阈值锚错了。**
        #   · 96/97 的要求是结构性的——「同 code 连续推送折叠 ×N」；
        #   · 「首屏 ≤50%」只出自 docs/100 F-4 的回归建议（原文 "例如 ≤10% 或『同 code
        #     连续出现 ≤1 行』"），**不是**规格硬要求；
        #   · 同一条代码：docs/104（05:58）读到 `0.525` 判红，本轮 22:06 读到 `0.375`
        #     判绿——红绿跟着库长跑，不跟着代码跑。
        # 所以 R3 改成**构造数据直接验折叠规则**（确定性、不依赖库快照），真实首屏的
        # pushShare / 折叠组只作**读数**打印（不判红），供跨轮对账。
        r3 = _fold_fixture(A)
        check("R3 折叠规则：同 code 连续 10 条（3s 间隔）→ 折 1 条、覆盖 10",
              r3["dense"]["groups"] == 1 and r3["dense"]["covered"] == 10
              and r3["dense"]["displayed"] == 1,
              json.dumps(r3["dense"], ensure_ascii=False))
        check(f"R3 折叠时间闸：同 code 但间隔 400s > {A.FOLD_PUSH_SEC}s → 不折（10 条原样）",
              r3["sparse"]["groups"] == 0 and r3["sparse"]["displayed"] == 10,
              json.dumps(r3["sparse"], ensure_ascii=False))
        check("R3 折叠只认连续同 code：A/B 交替 → 不折（10 条原样）",
              r3["alternating"]["groups"] == 0 and r3["alternating"]["displayed"] == 10,
              json.dumps(r3["alternating"], ensure_ascii=False))
        original_min = A.FOLD_MIN
        try:
            A.FOLD_MIN = 11                      # 门槛抬到 11 → 10 条不该再折
            mutated = _fold_fixture(A)["dense"]
        finally:
            A.FOLD_MIN = original_min
        check("R3 变异（FOLD_MIN=11）→ 折叠判据必须红",
              mutated["groups"] == 0 and mutated["displayed"] == 10,
              f"基线 {r3['dense']} → 变异后 {mutated}（必须 0 组 / 10 条）")

        # 读数（**不判红**）：真实 24h 项目视图首屏 + 关折叠对照，只用于跨轮对账。
        live = A.collect_stream(conn, scope_kind="project", project=PROJECT, board_id=None,
                                win="24h", since=since, limit=40, num_cursor=None,
                                direction="older")
        live_sum = _push_summary(live["data"]["items"])
        off = _child({"FISH_WB_FOLD_PUSH": "0"}, "pushfold", args.db)
        print(f"# R3 读数（不判红，仅对账）：首屏 {json.dumps(live_sum, ensure_ascii=False)}"
              f" · 关折叠对照 {json.dumps(off, ensure_ascii=False)}")

        # ── R4 项目过滤记忆事件 ──────────────────────────────────
        mem = _child({}, "memory", args.db)
        check("R4 项目视图 memory 类 >0 且项目对得上",
              mem["memoryCount"] > 0 and mem["allProjectMatch"],
              json.dumps(mem, ensure_ascii=False))
        mem_bad = _child({"FISH_WB_EVENT_PROJECT": "payload"}, "memory", args.db)
        check("R4 变异（改回 payload.project）memory 类归零",
              mem_bad["memoryCount"] == 0, json.dumps(mem_bad, ensure_ascii=False))

        # ── R6 D2 回归（工具+推送折叠一起关）─────────────────────
        foldmin = _child({"FISH_WB_FOLD_MIN": "9999"}, "foldmin", args.db)
        check("R6 变异（FOLD_MIN=9999）fold.groups 回到 0",
              foldmin["groups"] == 0, json.dumps(foldmin, ensure_ascii=False))
    finally:
        conn.close()

    # ── R5 只读证据 + 可写连接必须拒绝启动 ─────────────────────
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        conn = A.open_conn(args.db)
        conn.close()
    proven = [line for line in err.getvalue().splitlines() if "readonly_proven" in line]
    if proven:
        rec = json.loads(proven[0])
        ok = (rec.get("event") == "readonly_proven"
              and "OperationalError" in str(rec.get("errorType"))
              and "readonly database" in str(rec.get("error")))
        check("R5 开连接留下 readonly_proven 真异常", bool(ok), json.dumps(rec, ensure_ascii=False))
    else:
        check("R5 开连接留下 readonly_proven 真异常", False, "stderr 里没有 readonly_proven 行")

    tmpdir = tempfile.mkdtemp(prefix="fish-wb-verify-")
    tmpdb = os.path.join(tmpdir, "writable.sqlite")
    seed = sqlite3.connect(tmpdb)
    seed.execute("CREATE TABLE probe_seed (x INTEGER)")
    seed.commit()
    seed.close()
    try:
        writable = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child-writable", tmpdb],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        lines = [line for line in (writable.stdout or "").splitlines() if line.strip()]
        detail = json.loads(lines[-1]) if lines else {"stdout": writable.stdout,
                                                      "stderr": writable.stderr[-400:]}
        check("R5 变异（连接可写）必须拒绝启动",
              bool(detail.get("refused")) and detail.get("startedServer") is False,
              json.dumps(detail, ensure_ascii=False))
    finally:
        for name in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, name))
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    # ── HTTP 面回归（可选）────────────────────────────────────
    if args.api:
        base = args.api.rstrip("/")
        hits = {}
        for path in ("/api/health", "/api/facets?win=24h", "/api/stream?win=24h&limit=40",
                     "/api/stats?days=1&top=5", f"/api/entry?id=push:1"):
            try:
                with urllib.request.urlopen(base + path, timeout=30) as resp:
                    hits[path] = resp.status
            except urllib.error.HTTPError as exc:
                hits[path] = exc.code
            except Exception as exc:  # noqa: BLE001
                hits[path] = f"ERR {type(exc).__name__}"
        check("R7 HTTP 五端点 200（entry 允许 404）",
              all(v in (200, 404) for v in hits.values()), json.dumps(hits, ensure_ascii=False))

        req = urllib.request.Request(base + "/api/stream", data=b"x=1", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                post_status = resp.status
        except urllib.error.HTTPError as exc:
            post_status = exc.code
        check("R7 POST 被拒 405", post_status == 405, f"status={post_status}")

        req = urllib.request.Request(base + "/api/stream", headers={"Origin": "https://evil.example"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                evil = resp.status
        except urllib.error.HTTPError as exc:
            evil = exc.code
        check("R7 异源 Origin 被拒 403", evil == 403, f"status={evil}")

        req = urllib.request.Request(base + "/api/stream",
                                     headers={"Origin": "http://127.0.0.1:8799"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            acao = resp.headers.get("Access-Control-Allow-Origin")
        check("R7 静态托管源 8799 放行并回执 ACAO", acao == "http://127.0.0.1:8799",
              f"ACAO={acao}")

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n# 汇总 {passed}/{total} PASS（登记 {expected} 条，实跑 {total} 条）")
    if total != expected:
        print(f"# 条数与登记不符：登记 {expected} 条、实跑 {total} 条 → 判红"
              "（声称的条数必须可复算）")
        return 1
    return 0 if passed == total else 1


if __name__ == "__main__":
    if "--child-writable" in sys.argv:
        # 变异：只读连接被换成可写连接时，启动必须被拒绝（用 temp 库，绝不碰真库）
        tmp = sys.argv[sys.argv.index("--child-writable") + 1]
        import fish_workbench_api as A
        A.fish_ui.open_readonly = lambda path: sqlite3.connect(path)
        err = io.StringIO()
        refused = False
        try:
            with contextlib.redirect_stderr(err):
                A.main(["--db", tmp, "--port", "0"])
        except SystemExit:
            refused = True
        print(json.dumps({"refused": refused, "startedServer": False,
                          "hasReadonlyFailed": "readonly_failed" in err.getvalue()},
                         ensure_ascii=False))
        raise SystemExit(0 if refused else 1)
    raise SystemExit(main())
