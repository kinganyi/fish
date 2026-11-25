#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W6 对账（**只读**）：归属率 / 语料水位 / 缺口 / 三份清单（113 §10.1 步 6）。

## 这个工具回答什么问题

W6 的回填把规则灌进历史数据，所以"回填前后到底长什么样"必须有一份**能被复跑**的读数，
而不是散在会话里的几段 SQL。四个问题，一份输出：

1. **WB 归属率**（两级归因，113 §10.1 步 5）：`tool_activity` 里 WB 的写入有多少行有公民。
   拆开打三档，因为它们的**语义完全不同**：
     · L1 该 `session_key` 在 `session_tokens` 一行都没有 → **永不回填**（`missing`）；
     · L2 有 token、但这一行早于首个 `issued_at` → `pre_identity`，**不许伪造归属**；
     · 镜像断言：**行时刻 ≥ 首 token 却仍未归属 = 0** —— 这才是"两级归因成立"的判据。
   把 16.4% 这种数当 KPI 是误读：分母里 7,978 行本来就没有凭证可归。
2. **`corpus_session` 的 `origin_basis<>'default'` 比例**（W3a 的水位）：`default` = 还没被
   新的血缘判定覆盖过的历史行。0 行不是失败——它说明"W3a 之后还没有新的 WB ingest"。
3. **DSH 语料 / 日志缺口**：日志侧会话头 vs 语料侧 `corpus_session` 的差集，**逐条列名**
   （有父 / 根分开数）。缺口本身是**活的**：会话树是一个正在被写的树，几分钟前刚开的会话
   就会出现在 log-only 里——所以本工具**只报数、不锚绝对数**，读数要带时刻。
4. **`session_lineage` 三份清单**（cycle / orphan / conflict）：0 行也打印。

## 为什么是独立脚本而不是 `fish` 子命令

它对**生产库**只读、对依赖只要求 stdlib + p0 的叶子模块，不需要装配 `fish`（`fish` 的
import 面比它需要的大一个数量级），也不需要 `migrate`——对账绝不该顺手改库结构。

用法（只读）：
    python p0/tools/reconcile_lineage.py                    # 缺省 data 根 = p0/data
    python p0/tools/reconcile_lineage.py --data <data>
    python p0/tools/reconcile_lineage.py --json             # 机器可读
    python p0/tools/reconcile_lineage.py --skip-log         # 不扫宿主日志（快；缺口标判不到）

退出码：
    0 = 全部判据通过
    1 = 有判据不过（逐条列出是哪一条、读数多少）
    2 = 判不到（库不存在 / 读不出来）——**判不到不是通过**
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
P0 = HERE.parent
DEFAULT_DATA = P0 / "data"
sys.path.insert(0, str(P0))

#: 「WB 归属率」的门槛（113 §10 验收：≥95%）。**两个口径各判一次**——只判一个时，
#: 另一个可以悄悄坏掉（活动行级绿了、会话级 0.9%，报告看起来还是"通过"）。
WB_ATTRIBUTION_GATE = 0.95


def _connect_ro(path) -> sqlite3.Connection:
    """只读打开（`mode=ro`）：对账工具**一个字节都不许写**，写一句就当场报错。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在（判不到，不是通过）")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rate(part: int, total: int) -> float:
    return (float(part) / total) if total else 0.0


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


# ---------------------------------------------------------------- ① WB 归属率
def wb_attribution(index_conn, corpus_conn) -> dict:
    """两级归因的三档计数 + 两个口径的归属率（113 §10.1 步 5 的 SQL 原样）。

    `pre_identity` 与 `missing` **必须分开**：前者是"有凭证但行更早"（历史，不可回填），
    后者是"这个会话根本没有凭证"（数据缺口，要靠 `session write` 那类动作补）。
    合成一个数时，"回填规则改成整会话一律回填"这个变异就查不出来（设计 J1②）。
    """
    total, attributed = index_conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN IFNULL(citizen_id,'') <> '' THEN 1 ELSE 0 END) "
        "  FROM tool_activity WHERE host = 'workbuddy'").fetchone()
    total, attributed = int(total or 0), int(attributed or 0)
    first_token = ("SELECT session_key, MIN(issued_at) AS first_at FROM session_tokens "
                   "GROUP BY session_key")
    missing = index_conn.execute(
        "SELECT COUNT(*) FROM tool_activity t WHERE t.host = 'workbuddy' "
        "  AND IFNULL(t.citizen_id,'') = '' AND NOT EXISTS ("
        "    SELECT 1 FROM session_tokens s WHERE s.session_key = t.session_key)").fetchone()[0]
    pre_identity = index_conn.execute(
        "SELECT COUNT(*) FROM tool_activity t JOIN (" + first_token + ") f "
        "  ON f.session_key = t.session_key "
        " WHERE t.host = 'workbuddy' AND IFNULL(t.citizen_id,'') = '' "
        "   AND (t.seen_at IS NULL OR t.seen_at < f.first_at)").fetchone()[0]
    mirror = index_conn.execute(
        "SELECT COUNT(*) FROM tool_activity t JOIN (" + first_token + ") f "
        "  ON f.session_key = t.session_key "
        " WHERE t.host = 'workbuddy' AND IFNULL(t.citizen_id,'') = '' "
        "   AND t.seen_at >= f.first_at").fetchone()[0]

    sessions_total, sessions_attributed = corpus_conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN IFNULL(citizen_id,'') <> '' THEN 1 ELSE 0 END) "
        "  FROM corpus_session WHERE host = 'workbuddy'").fetchone()
    sessions_total, sessions_attributed = int(sessions_total or 0), int(sessions_attributed or 0)
    return {
        "activity_total": total, "activity_attributed": attributed,
        "activity_rate": _rate(attributed, total),
        "missing_no_token": int(missing or 0),
        "pre_identity": int(pre_identity or 0),
        "mirror_unattributed_after_token": int(mirror or 0),
        "session_total": sessions_total, "session_attributed": sessions_attributed,
        "session_rate": _rate(sessions_attributed, sessions_total),
    }


# ---------------------------------------------------------------- ② 语料水位
def corpus_origin_basis(corpus_conn) -> dict:
    """`origin_basis` 分布（WB 是 W3a 的验收面；DSH 只做对照）。

    `default` 不是"坏值"，它是"这一行还没被血缘判定覆盖过"的诚实标记：
    新 ingest 的行会是 `header`，历史行等 W6 回填。所以这个比例只报数、不设门槛。
    """
    out = {}
    for host in ("workbuddy", "dsh"):
        rows = corpus_conn.execute(
            "SELECT origin_basis, COUNT(*) FROM corpus_session WHERE host = ? "
            "GROUP BY origin_basis ORDER BY COUNT(*) DESC", (host,)).fetchall()
        dist = {str(k): int(v) for k, v in rows}
        total = sum(dist.values())
        non_default = total - dist.get("default", 0)
        out[host] = {"total": total, "dist": dist, "non_default": non_default,
                     "non_default_rate": _rate(non_default, total)}
    return out


# ---------------------------------------------------------------- ③ DSH 缺口
def dsh_gap(corpus_conn, roots=None) -> dict:
    """日志侧会话头 vs 语料侧 `corpus_session` 的差集（**逐条列名**）。

    走 `fish_lineage_dsh` 的 `scan_with_report`（W2 的实现），**不写第三套目录扫描**；
    根目录用它缺省的那一套（`$FISH_DSH_SESSIONS` / `$DSH_HOME/sessions`）。

    为什么缺口分"有父 / 根"两档：有父的行落在 log-only 里通常只是"语料还没 ingest 到它"
    （新会话），而根的行是"整个会话都没进语料"——两者的处理动作完全不同。
    """
    import fish_lineage_dsh as dsh                      # 局部 import：只这一节需要

    adapter = dsh.DshLineageAdapter(roots=roots)
    nodes, scan_report = adapter.scan_with_report()
    by_id = {node["session_id"]: node for node in nodes}
    corpus_ids = {r[0] for r in corpus_conn.execute(
        "SELECT session_id FROM corpus_session WHERE host = 'dsh'")}
    log_only, corpus_only = [], []
    for sid in sorted(set(by_id) - corpus_ids):
        node = by_id[sid]
        log_only.append({"session_id": sid,
                         "parent_session": node.get("parent_session"),
                         "kind": "sub" if node.get("parent_session") else "root",
                         "origin": node.get("origin"), "basis": node.get("basis")})
    corpus_only = sorted(corpus_ids - set(by_id))
    return {
        "files": scan_report["files"], "parsed": scan_report["parsed"],
        "unique": scan_report["unique"], "failed": scan_report["failed"],
        "corpus_rows": len(corpus_ids),
        "log_only": log_only,
        "log_only_subs": sum(1 for item in log_only if item["kind"] == "sub"),
        "log_only_roots": sum(1 for item in log_only if item["kind"] == "root"),
        "corpus_only": corpus_only,
    }


# ---------------------------------------------------------------- ④ 三份清单
def lineage_lists(index_conn, hosts=("dsh", "workbuddy")) -> dict:
    """`session_lineage` 的三份清单（**0 行也打印**，113 §10.2 J-W6-C）。

    走 `fish_lineage_backfill.report`——与 CLI 的 `fish lineage report` **同一份实现**：
    两份拷贝迟早各自漂开，那时"报告说 0"与"库里有环"会同时成立而没人发现。
    """
    import fish_lineage_backfill                          # 局部 import：只这一节需要

    out = {}
    for host in hosts:
        rep = fish_lineage_backfill.report(index_conn, host)
        out[host] = {"rows": rep["rows"], "missing_table": rep.get("missing_table", False),
                     "error": rep.get("error"), "max_depth": rep.get("max_depth", 0),
                     "depth_dist": rep.get("depth_dist") or {},
                     "by_basis": rep.get("by_basis") or {},
                     "cycles": rep.get("cycles") or [],
                     "orphans": rep.get("orphans") or [],
                     "conflicts": rep.get("conflicts") or []}
    return out


# ---------------------------------------------------------------- 汇总
def collect(data_root, *, dsh_roots=None, scan_log=True) -> dict:
    data = Path(data_root)
    index_conn = _connect_ro(data / "index.sqlite")
    corpus_conn = None
    try:
        try:
            corpus_conn = _connect_ro(data / "corpus.sqlite")
        except (FileNotFoundError, sqlite3.Error) as exc:
            corpus_conn = None
            corpus_error = str(exc)
        else:
            corpus_error = None

        wb = wb_attribution(index_conn, corpus_conn) if corpus_conn is not None else None
        basis = corpus_origin_basis(corpus_conn) if corpus_conn is not None else None
        lists = lineage_lists(index_conn)
    finally:
        index_conn.close()
        if corpus_conn is not None:
            corpus_conn.close()

    gap = None
    gap_error = None
    if scan_log and corpus_conn is not None:
        # 缺口要在**同一个语料快照**上算，所以这里重新开一次只读连接（上面那个已关）
        corpus_conn = _connect_ro(data / "corpus.sqlite")
        try:
            gap = dsh_gap(corpus_conn, roots=dsh_roots)
        except Exception as exc:                        # noqa: BLE001 —— 判不到要如实标
            gap_error = f"{type(exc).__name__}: {exc}"
        finally:
            corpus_conn.close()

    gates = []
    if wb is None:
        # 语料库读不出来 → 归属率与水位都判不到。**判不到不是通过**（退出码 1）：
        # 少了这一条，"corpus.sqlite 不见了"会以"门槛全绿 + 退出码 0"的形式收场。
        gates.append({"name": "WB 归属率（判不到）", "ok": False,
                      "reading": f"corpus.sqlite 读不出来：{corpus_error}"})
    else:
        gates.append({
            "name": "镜像断言：行时刻 ≥ 首 token 却未归属",
            "ok": wb["mirror_unattributed_after_token"] == 0,
            "reading": f"{wb['mirror_unattributed_after_token']} 行（要求 0）",
        })
        for label, key in (("WB 归属率（活动行级）", "activity_rate"),
                           ("WB 归属率（会话级）", "session_rate")):
            gates.append({
                "name": f"{label} ≥ {_pct(WB_ATTRIBUTION_GATE)}",
                "ok": wb[key] >= WB_ATTRIBUTION_GATE,
                "reading": _pct(wb[key]),
            })
    if scan_log and gap is None:
        gates.append({"name": "DSH 语料/日志缺口（判不到）", "ok": False,
                      "reading": str(gap_error)})
    for host, rep in lists.items():
        for label, key in (("cycle", "cycles"), ("orphan", "orphans"),
                           ("conflict", "conflicts")):
            gates.append({
                "name": f"{host} {label} = 0",
                "ok": len(rep[key]) == 0,
                "reading": f"{len(rep[key])} 行",
            })
    return {"data_root": str(data), "corpus_error": corpus_error,
            "wb_attribution": wb, "corpus_origin_basis": basis,
            "dsh_gap": gap, "dsh_gap_error": gap_error,
            "lineage": lists, "gates": gates}


def _gate_lines(rep: dict) -> list:
    lines = ["", "[门槛]（逐条判，读数在后面）"]
    for gate in rep["gates"]:
        lines.append(f"  {'PASS' if gate['ok'] else 'FAIL'}  {gate['name']}：{gate['reading']}")
    return lines


def render(rep: dict) -> str:
    """人读报告：**每一节、每一份清单都会打印**——包括"0 行"与"判不到"。"""
    lines = [f"会话树 W6 对账（只读）  data={rep['data_root']}"]
    wb = rep["wb_attribution"]
    lines.append("")
    lines.append("[1] WB 归属率（两级归因，113 §10.1 步 5）")
    if wb is None:
        lines.append(f"  判不到：{rep['corpus_error']}（不是通过）")
    else:
        lines.append(f"  tool_activity(workbuddy)：{wb['activity_total']} 行，"
                     f"已归属 {wb['activity_attributed']} → {_pct(wb['activity_rate'])}")
        lines.append(f"    - L1 无凭证（永不回填，basis=missing）：{wb['missing_no_token']} 行")
        lines.append(f"    - L2 pre_identity（行早于首个 issued_at，不伪造归属）："
                     f"{wb['pre_identity']} 行")
        lines.append(f"    - 镜像断言（行时刻 ≥ 首 token 却未归属）："
                     f"{wb['mirror_unattributed_after_token']} 行（要求 0）")
        lines.append(f"  corpus_session(workbuddy)：{wb['session_total']} 行，"
                     f"已归属 {wb['session_attributed']} → {_pct(wb['session_rate'])}")
        lines.append(f"  门槛 {_pct(WB_ATTRIBUTION_GATE)}：活动行级 "
                     f"{'达' if wb['activity_rate'] >= WB_ATTRIBUTION_GATE else '不达'}"
                     f" / 会话级 "
                     f"{'达' if wb['session_rate'] >= WB_ATTRIBUTION_GATE else '不达'}"
                     "（分母里含 L1+L2 两档本来就不该被回填的行，见上）")

    lines.append("")
    lines.append("[2] corpus_session 的 origin_basis（W3a 水位；default = 未被血缘判定覆盖）")
    basis = rep["corpus_origin_basis"]
    if basis is None:
        lines.append(f"  判不到：{rep['corpus_error']}（不是通过）")
    else:
        for host in ("workbuddy", "dsh"):
            item = basis[host]
            dist = " / ".join(f"{k or '空'}={v}" for k, v in item["dist"].items()) or "（0 行）"
            lines.append(f"  {host}：{item['total']} 行，非 default {item['non_default']} 行"
                         f"（{_pct(item['non_default_rate'])}）；分布：{dist}")
        lines.append("  （WB 非 default = 0 不是失败：它表示 W3a 之后还没有新的 WB ingest；"
                     "历史行由 W6 回填决定）")

    lines.append("")
    lines.append("[3] DSH 语料 / 日志缺口（日志侧会话头 vs corpus_session，逐条列名）")
    gap = rep["dsh_gap"]
    if gap is None:
        lines.append(f"  判不到：{rep['dsh_gap_error'] or '未扫描'}（不是通过）")
    else:
        lines.append(f"  日志：{gap['files']} 文件 / 解析 {gap['parsed']} / 唯一会话 {gap['unique']}"
                     f"（失败 {len(gap['failed'])}）；语料 {gap['corpus_rows']} 行")
        if gap["failed"]:
            lines.append(f"  扫描失败（逐条）：{len(gap['failed'])} 份")
            lines.extend(f"    - {item.get('path')}（{item.get('reason')}）"
                         for item in gap["failed"])
        lines.append(f"  log-only：{len(gap['log_only'])} 条"
                     f"（有父 {gap['log_only_subs']} / 根 {gap['log_only_roots']}）")
        if not gap["log_only"]:
            lines.append("    （本次检测到 0 条）")
        else:
            lines.extend(f"    - {item['session_id']}  [{item['kind']} / "
                         f"parent={item['parent_session'] or '-'} / basis={item['basis']}]"
                         for item in gap["log_only"])
        lines.append(f"  corpus-only：{len(gap['corpus_only'])} 条")
        if not gap["corpus_only"]:
            lines.append("    （本次检测到 0 条）")
        else:
            lines.extend(f"    - {sid}" for sid in gap["corpus_only"])

    lines.append("")
    lines.append("[4] session_lineage 三份清单（0 行也打印）")
    for host, item in rep["lineage"].items():
        lines.append(f"  == {host} ==")
        if item["missing_table"]:
            lines.append(f"    判不到：投影表不存在或读不出来（{item['error']}）")
        lines.append(f"    投影行数：{item['rows']} / 最大深度 {item['max_depth']}")
        depth = " / ".join(f"{k or '空'}={v}" for k, v in
                           sorted(item["depth_dist"].items(), key=lambda kv: str(kv[0]))) or "（空）"
        lines.append(f"    深度分布：{depth}")
        basis_text = " / ".join(f"{k or '空'}={v}" for k, v in item["by_basis"].items()) or "（空）"
        lines.append(f"    basis 分布：{basis_text}")
        for label, key, hint in (("cycle", "cycles", "环上的行不参与继承/推送/UI 展开"),
                                 ("orphan", "orphans", "父不存在 → unknown/missing，不是 main"),
                                 ("conflict", "conflicts", "depth 重算 ≠ depth_host")):
            rows = item[key]
            if not rows:
                lines.append(f"    {label}：本次检测到 0 行（{hint}）")
            else:
                lines.append(f"    {label}：{len(rows)} 行（{hint}）")
                lines.extend(f"      - {item_id}" for item_id in rows)
    lines.extend(_gate_lines(rep))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="会话树 W6 对账（只读）")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="data 根（含 index.sqlite / corpus.sqlite）")
    ap.add_argument("--dsh-sessions", default=None,
                    help="DSH 会话目录（缺省走 $FISH_DSH_SESSIONS / $DSH_HOME/sessions）")
    ap.add_argument("--skip-log", action="store_true",
                    help="不扫宿主日志（快；缺口一节标『判不到』）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    try:
        rep = collect(args.data, dsh_roots=args.dsh_sessions, scan_log=not args.skip_log)
    except Exception as exc:                             # noqa: BLE001 —— 判不到要说清
        print(f"判不到：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep), end="")
    failed = [gate for gate in rep["gates"] if not gate["ok"]]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
