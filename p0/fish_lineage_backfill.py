#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话树 W6 · 回填与对账（批量层）：`scan` / `report` / 报告格式化（113 §10.1）。

## 为什么它不在 `fish_lineage.py` 里

J4 窗口：`fish_lineage.py` 的上界是 700 行（`test_split_contract.FISH_LINEAGE_LINES_MAX`），
而 W6 的批量/对账面（影子库 dry-run、父闭合、`_backups` 快照、报告渲染）本身就有三百多行。
仓库的规矩是「**先抽层再抬窗口**」（2026-11-11 先例 + W1b 把 DDL 抽成
`fish_lineage_schema`），所以这一层独立成模块，`fish_lineage.py` 保持"投影层本体"，
窗口一个字节都不动。

## 依赖方向（单向）

    fish.py / CLI ──→ fish_lineage_backfill ──→ fish_lineage ──→ fish_lineage_schema
                                                  └──→ fish_common

`fish_lineage` **不 import 本模块**（它的分层护栏 `test_lineage_schema.LineageLayeringTest`
白名单里只有 `fish_common` + `fish_lineage_schema`）——所以 `detect_cycles` 留在那边，
"批量回填 + 报告"在这里。方向只有一个：本模块读投影层，投影层看不见本模块。

## 两个入口，一份格式化

  · `scan(host, ...)`    —— 批量回填（缺省 dry-run：全流程跑在内存影子库上，
                            不改任何行数据；`ensure_tables` 的建表 DDL 幂等除外）；
  · `report(conn, host)` —— 只读对账（行数 / 分布 / 三份清单）。

为什么 dry-run 要"跑全流程"而不是只数一数：只数文件数的话，"回填之后会不会出现
环/孤儿/冲突"这个问题在 dry-run 阶段根本答不了，而它正是 `--apply` 之前唯一该问的。

对外契约只有 `scan` / `report` / `format_report` / `list_lines` / `run_cli` / `HOSTS`；
其余 `_` 前缀的辅助函数都服务这两个入口之一。
"""
import sqlite3
import time
from pathlib import Path

import fish_common
import fish_lineage

# ================================================================ W6 · 回填与对账
#
# 两个入口，**同一份格式化**（"0 行也要打印"只许有一处实现）：
#   · `scan(host, ...)`   —— 批量回填（缺省 dry-run：全流程跑在内存影子库上，
#                            不改任何行数据；`ensure_tables` 的建表 DDL 幂等除外）；
#   · `report(conn, host)`—— 只读对账（行数 / 分布 / 三份清单）。
#
# 为什么 dry-run 要"跑全流程"而不是只数一数：只数文件数的话，"回填之后会不会出现
# 环/孤儿/冲突"这个问题在 dry-run 阶段根本答不了，而它正是 `--apply` 之前唯一该问的。

#: 回填的宿主顺序（报告里 DSH 在前：它是本波的主对象，WB 是 W3 的历史语料）
HOSTS = ("dsh", "workbuddy")

#: 三份清单的**语义提示**：打印时逐份带上，读报告的人不必去翻 113 §10。
_LIST_HINTS = {
    "cycle": "环上的行不参与继承/推送/UI 展开",
    "orphan": "父不存在 → unknown/missing，不是 main",
    "conflict": "depth 重算 ≠ depth_host",
}


def _default_data_dir() -> Path:
    """缺省 data 根（`p0/data`）：与 `fish.DEFAULT_ROOT` 同值，但**不 import fish**。"""
    return Path(__file__).resolve().parent / "data"


def _db_file(conn: sqlite3.Connection) -> str | None:
    """连接指向的库文件；内存库 → `None`（**不回落生产目录**，测试污染最难查）。"""
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if row[1] == "main" and row[2]:
                return str(row[2])
    except sqlite3.Error:
        return None
    return None


def _count_rows(conn: sqlite3.Connection, host: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM session_lineage WHERE host = ?",
                        (host,)).fetchone()[0]


def _distributions(conn: sqlite3.Connection, host: str) -> dict:
    """四张分布表（origin / relation / basis / depth）：报告里"行数"之外唯一的形状证据。"""
    out: dict[str, dict] = {"origin": {}, "relation": {}, "basis": {}, "depth": {}}
    for origin, relation, basis, depth in conn.execute(
            "SELECT origin, relation, basis, depth FROM session_lineage WHERE host = ?",
            (host,)):
        for key, value in (("origin", origin), ("relation", relation),
                           ("basis", basis), ("depth", depth)):
            label = "" if value is None else str(value)
            out[key][label] = out[key].get(label, 0) + 1
    return out


def _backup_index(conn: sqlite3.Connection) -> str | None:
    """写前快照：`<data>/_backups/index-before-lineage-<stamp>.sqlite`。

    用 SQLite 官方 backup API 而不是文件拷贝：WAL 模式下直接 copy 会拿到**撕裂快照**
    （拷到一半的 WAL 还没合并），还原回去是一份不一致的库。命名与 `fish_db._presence_backup`
    同风格；**永不自动删**（回滚点是人的凭据，不是缓存）。

    备份失败 → 异常向上抛（**宁可不写**）：113 §10.3 的回退完全依赖这份快照。
    内存库（测试）没有可备份的文件 → 返回 `None` 并在报告里注明。
    """
    path = _db_file(conn)
    if not path:
        return None
    out_dir = Path(path).parent / "_backups"
    stamp = time.strftime("%y%m%d-%H%M%S")
    dest = out_dir / f"index-before-lineage-{stamp}.sqlite"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = sqlite3.connect(str(dest))
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return str(dest)


def _shadow_of(conn: sqlite3.Connection, host: str) -> sqlite3.Connection:
    """目标库的宿主行复制进一个**内存库**：dry-run 的全流程都跑在它上面。

    为什么不只数文件：dry-run 要回答的是"**回填之后**会不会出现环/孤儿/冲突"。
    只数文件数的 dry-run 会把这个问题留到 `--apply` 之后，而那时生产库已经被写了。
    影子库是"在同一份现状上先跑一遍"的最低成本实现——生产连接全程只读。
    """
    shadow = sqlite3.connect(":memory:")
    fish_lineage.ensure_tables(shadow)
    columns = ", ".join(fish_lineage.NODE_FIELDS)
    marks = ", ".join(f":{f}" for f in fish_lineage.NODE_FIELDS)
    try:
        existing = [dict(zip(fish_lineage.NODE_FIELDS, r)) for r in conn.execute(
            f"SELECT {columns} FROM session_lineage WHERE host = ?", (host,))]
    except sqlite3.Error:
        existing = []                       # 老库还没有投影表：影子库从空开始
    if existing:
        shadow.executemany(
            f"INSERT OR REPLACE INTO session_lineage ({columns}) VALUES ({marks})", existing)
    return shadow


def _adapter_report(adapter) -> dict | None:
    """适配器的对账面（DSH 有 `last_report`；WB 没有 → `None`）。"""
    try:
        report = adapter.last_report
    except AttributeError:                  # 适配器没有对账面不是错误
        return None
    return report or None


def _unique_nodes(nodes) -> tuple[list, int]:
    """按 `(host, session_id)` 去重（**后到者赢**，与 `upsert_node` 的整行覆盖同口径）。"""
    unique: dict[tuple, dict] = {}
    duplicates = 0
    for node in nodes:
        key = (node.get("host"), node.get("session_id"))
        if key in unique:
            duplicates += 1
        unique[key] = node
    return list(unique.values()), duplicates


#: 父闭合的跳数上限（与 `ancestor_sessions(max_hops=64)` 同口径：防病态数据，不是业务限制）
_PARENT_CLOSURE_MAX_ROUNDS = 64


def _resolve_parents(adapter, nodes) -> tuple[list, list]:
    """把"父行不在本次扫描结果里"的父补进投影：`(补到的节点, 补不到的父 id)`。

    为什么 W6 必须做这一步（不是可选项）：`session_lineage` 是一棵树，而**两个宿主的
    扫描面本来就不一样**——DSH 的 `discover` 枚举全部会话文件（父自然在），WB 的适配器
    按 W3 的契约只枚举 `agent-*.jsonl`（**父是顶层会话文件，一个都不在里面**）。
    少了这一步，WB 的 2,100 个成员会**全部**落成孤儿：orphan 清单从"异常告警"变成
    "每一行都在里面"，J-W6-C 的逐条列名立刻不可读，回填也就没有把树建起来。

    判不到的父**不进投影**（`missing` 原样返回）：父指针指向一个查不到的会话，是真孤儿，
    该由 `detect_cycles` 的 orphan 清单逐条列名——不在这里编一行假父。

    每个补到的节点再往上看它的父（最多 `_PARENT_CLOSURE_MAX_ROUNDS` 跳）：WB 的
    `resolve` 对顶层会话走 `sessions` 表（实测 31 个父全部命中本机），一次就收工。
    """
    known = {node.get("session_id") for node in nodes}
    pending = {node.get("parent_session") for node in nodes
               if node.get("parent_session")} - known
    extra: list = []
    missing: list = []
    rounds = 0
    while pending and rounds < _PARENT_CLOSURE_MAX_ROUNDS:
        nxt: set = set()
        for sid in sorted(pending):
            node = adapter.resolve(sid)          # 只读；判不到 → None（不猜）
            if not node:
                missing.append(sid)
                continue
            extra.append(node)
            known.add(node.get("session_id") or sid)
            known.add(sid)
            if node.get("parent_session"):
                nxt.add(node["parent_session"])
        pending = {sid for sid in nxt if sid not in known}
        rounds += 1
    return extra, missing


def scan(host: str, *, since: str | None = None, limit: int | None = None,
         dry_run: bool = True, conn: sqlite3.Connection | None = None,
         root=None) -> dict:
    """**W6 回填入口**（113 §10.1 步 1–4）：批量 `upsert_node` → 重算深度 → 检测三清单。

    `dry_run=True`（**缺省**）时**不改任何行数据**（`ensure_tables` 的建表 DDL 幂等
    除外）：节点写入、深度重算、环标记全部发生在内存影子库上（`_shadow_of`），
    报告里给出"真跑会是什么样"的读数。
    `dry_run=False` 时先备份（`_backup_index`）再写，并返回前后计数。

    **为什么留这条例外**（123 评审 🟡-4）：dry-run 的第一步是对**生产连接**跑
    `fish_lineage.ensure_tables`（表不存在就建），在全新/半旧的库上那就是一次写。
    今天生产两张表都在、`CREATE TABLE IF NOT EXISTS` 是空操作（实测前后内容指纹
    相同），但**表述不能比实现更宽**——"一个字节都不写"是错的，"不改任何行数据"
    才是这条路径的真实承诺。要连这一次也不写，就得把 `ensure_tables` 也搬到 shadow
    上，而"投影表存在且可读"恰恰是影子库从生产复制的**前提**，所以例外留在明面上。

    扫描委托宿主适配器（`fish_lineage.get_adapter(host).scan`）——**不新增第三套目录扫描**：
    DSH 的发现走 `fish_corpus_dsh.discover`、WB 走 `fish_corpus_wb.discover`，
    两条路都已由 W2/W3 的适配器实现；这里只是批量调用。

    `conn` 缺省时自己按 `root`（缺省 `p0/data`）打开库并建表；显式传连接时不负责关它
    （CLI 与测试共用一条连接）。事务提交点在**本函数**：一批投影一次提交，
    半批落盘 = 树有洞（`upsert_node` 不 commit 正是为了这个）。

    返回的 report 里，`new`/`existing` 是**按宿主会话 id** 与既有投影比出来的，
    `rows_before`/`rows_after` 是投影行数的前后计数——两者都不是恒 0 的装饰。
    """
    adapter = fish_lineage.get_adapter(host)                       # 未知宿主 → KeyError（不猜）
    started = time.time()
    nodes = list(adapter.scan(since=since, limit=limit))
    observed_at = fish_common.now_utc()
    nodes, duplicates = _unique_nodes(nodes)

    owned = conn is None
    db_dir = None
    if owned:
        db_dir = Path(root) if root is not None else _default_data_dir()
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_dir / "index.sqlite"), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
    try:
        fish_lineage.ensure_tables(conn)                           # 老库升级路径（幂等）
        existing = set(fish_lineage.read_rows(conn, host))        # 影子库也要基于**现状**
        found_ids = {node["session_id"] for node in nodes}
        rows_before = len(existing)
        adapter_report = _adapter_report(adapter)
        backup = None
        target = conn
        if dry_run:
            target = _shadow_of(conn, host)
        else:
            backup = _backup_index(conn)              # 失败 → 抛（宁可不写）
        try:
            # 重扫时**保留计算列**（`depth` / `lineage_path`）：适配器的节点里没有这两列
            # （深度由 CTE 算，113 §3.3），`upsert_node` 的整行覆盖会把它们拍回 DEFAULT 0，
            # 于是每次重扫都要重算一遍——状态最终一样，但"幂等"在读数上不成立
            # （`updated` 永远 >0，谁也分不出"改了"和"重置了再算回来"）。
            # 只在节点自己没给值时保留（`setdefault`）：适配器哪天开始报深度，仍以它为准。
            previous = fish_lineage.read_rows(conn, host)

            def _project(node) -> None:
                old = previous.get(node.get("session_id"))
                if old is not None:
                    node.setdefault("depth", old[3])
                    node.setdefault("lineage_path", old[4])
                fish_lineage.upsert_node(target, node)

            written = 0
            for node in nodes:
                _project(node)
                written += 1
            # 父闭合：WB 的扫描面只有 `agent-*`，父（顶层会话）得由适配器按 id 补进来，
            # 否则整棵树全是孤儿（见 `_resolve_parents`）。判不到的父**不编**。
            extra_nodes, missing_parents = _resolve_parents(adapter, nodes)
            for node in extra_nodes:
                _project(node)
                written += 1
            target.commit()
            depth = fish_lineage.recompute_depth(target, host)
            detect = fish_lineage.detect_cycles(target, host, apply=True)
            target.commit()
            rows_after = _count_rows(target, host)
            dist = _distributions(target, host)
        finally:
            if dry_run:
                target.close()                        # 影子库一次性用完就丢
        return {
            "host": host, "dry_run": bool(dry_run), "since": since, "limit": limit,
            "observed_at": observed_at, "elapsed": round(time.time() - started, 2),
            "found": written, "unique": len(nodes), "duplicates": duplicates,
            "new": len(found_ids - existing), "existing": len(found_ids & existing),
            "written": written, "rows_before": rows_before, "rows_after": rows_after,
            "delta": rows_after - rows_before,
            "parents_resolved": len(extra_nodes), "parents_missing": missing_parents,
            "by_origin": dist["origin"], "by_relation": dist["relation"],
            "by_basis": dist["basis"], "depth_dist": dist["depth"],
            "depth": depth, "max_depth": depth["max_depth"],
            "cycles": detect["cycles"], "orphans": detect["orphans"],
            "conflicts": detect["conflicts"], "marked": detect["marked"],
            "restored": detect["restored"],
            "backup": backup, "db": _db_file(conn),
            "adapter": adapter_report,
            "failed": (adapter_report or {}).get("failed") or [],
        }
    finally:
        if owned:
            conn.close()


def report(conn: sqlite3.Connection, host: str) -> dict:
    """**W6 只读对账**（`fish lineage report` / `tools/reconcile_lineage.py` 共用）。

    只读的含义写进实现：`fish_lineage.detect_cycles(..., apply=False)` —— 连"标环"都不做。
    对账工具用 `mode=ro` 打开生产库时也必须能跑完（写一句就会 `attempt to write`）。

    投影表不存在（老库 / 只读介质）→ `missing_table=True` 的**判不到**，不是"0 行通过"。
    """
    observed_at = fish_common.now_utc()
    try:
        rows = fish_lineage.read_rows(conn, host)
    except sqlite3.Error as exc:
        return {"host": host, "rows": 0, "missing_table": True, "error": str(exc)[:300],
                "depth_dist": {}, "by_origin": {}, "by_relation": {}, "by_basis": {},
                "max_depth": 0, "cycles": [], "orphans": [], "conflicts": [],
                "marked": [], "restored": [], "observed_at": observed_at}
    detect = fish_lineage.detect_cycles(conn, host, apply=False)
    dist = _distributions(conn, host)
    return {"host": host, "rows": len(rows), "missing_table": False,
            "depth_dist": dist["depth"], "by_origin": dist["origin"],
            "by_relation": dist["relation"], "by_basis": dist["basis"],
            "max_depth": detect["max_depth"], "cycles": detect["cycles"],
            "orphans": detect["orphans"], "conflicts": detect["conflicts"],
            "marked": detect["marked"], "restored": detect["restored"],
            "observed_at": observed_at}


def _dist_text(dist: dict) -> str:
    """分布 dict → 一行（按值倒序；空 → `（空）`）。"""
    if not dist:
        return "（空）"
    items = sorted(dist.items(), key=lambda item: (-item[1], item[0]))
    return " / ".join(f"{label or '空'}={count}" for label, count in items)


def list_lines(label: str, ids: list, *, hint: str) -> list:
    """**三份清单的唯一打印格式**：0 行也打印（J-W6-C 的可观测性要求）。

    这条函数存在全部理由就是 J-W6-C："没打印"与"没检测"必须能被读报告的人区分开。
    所以空清单不是 `[]`、不是空行，而是一句明确的话——把打印挪进 `if ids:` 就再也
    分不出这两件事（那正是变异脚本打的那一点）。
    """
    if not ids:
        return [f"  {label}：本次检测到 0 行（{hint}）"]
    lines = [f"  {label}：{len(ids)} 行（{hint}）"]
    lines.extend(f"    - {item}" for item in ids)
    return lines


def format_report(reports, *, title: str = "会话树 · 回填与对账") -> str:
    """把 `scan` / `report` 的返回值渲染成**人读报告**（CLI 与对账工具共用这一份）。

    两种 report 用 `dry_run` 键区分（scan 有、report 没有）；三份清单**任何形状下都打**。
    """
    lines = [f"{title}（{reports[0]['observed_at'] if reports else fish_common.now_utc()}）"]
    for rep in reports:
        lines.append("")
        lines.append(f"== {rep['host']} ==")
        if rep.get("missing_table"):
            lines.append(f"  判不到：投影表不存在或读不出来（{rep.get('error', '')}）")
        if "dry_run" in rep:
            mode = ("dry-run（不改任何行数据；`ensure_tables` 的建表 DDL 幂等除外；"
                    "以下读数来自内存影子库）") if rep["dry_run"] \
                else "apply（已写库）"
            lines.append(f"  模式：{mode}")
            window = []
            if rep.get("since"):
                window.append(f"since={rep['since']}")
            if rep.get("limit") is not None:
                window.append(f"limit={rep['limit']}")
            if window:
                lines.append(f"  窗口：{' / '.join(window)}")
            adapter = rep.get("adapter") or {}
            scan_line = (f"  扫描：宿主机找到 {rep.get('unique', rep['found'])} 个会话"
                         f"（重复 {rep.get('duplicates', 0)} 份已去重）")
            if adapter:
                scan_line += (f"；文件 {adapter.get('files', '?')} / 解析 {adapter.get('parsed', '?')}"
                              f" / 失败 {len(adapter.get('failed') or [])}")
            lines.append(scan_line)
            if rep.get("failed"):
                lines.append(f"  扫描失败（逐条）：{len(rep['failed'])} 份")
                lines.extend(f"    - {item.get('path')}（{item.get('reason')}）"
                             for item in rep["failed"])
            if rep.get("duplicates"):
                lines.append(f"  同一会话多份文件：{rep['duplicates']} 份（只投影最新那份）")
            lines.append(f"  写入：新增 {rep.get('new', 0)} / 覆盖 {rep.get('existing', 0)}"
                         f"（upsert {rep.get('written', 0)} 行）")
            resolved = rep.get("parents_resolved") or 0
            missing_parents = rep.get("parents_missing") or []
            if resolved or missing_parents:
                lines.append(f"  父闭合：补进 {resolved} 个父行 / 判不到 {len(missing_parents)} 个"
                             "（判不到的父不编，落进下面的 orphan 清单）")
            lines.append(f"  投影行数：{rep['rows_before']} → {rep['rows_after']}"
                         f"（变化 {rep['delta']:+d}）")
            lines.append(f"  备份：{rep['backup'] or '（未写库，无需备份）'}")
            if rep.get("depth"):
                lines.append(f"  深度重算：更新 {rep['depth'].get('updated', 0)} 行 / "
                             f"最大深度 {rep.get('max_depth', 0)} / "
                             f"冲突 {rep['depth'].get('conflicts', 0)}")
            lines.append(f"  耗时：{rep.get('elapsed')}s")
        else:
            lines.append(f"  投影行数：{rep['rows']}（本次检测到 {rep['rows']} 行）")
            lines.append(f"  最大深度：{rep.get('max_depth', 0)}")
        lines.append(f"  深度分布：{_dist_text(rep.get('depth_dist') or {})}")
        lines.append(f"  origin 分布：{_dist_text(rep.get('by_origin') or {})}")
        lines.append(f"  relation 分布：{_dist_text(rep.get('by_relation') or {})}")
        lines.append(f"  basis 分布：{_dist_text(rep.get('by_basis') or {})}")
        lines.extend(list_lines("cycle", rep.get("cycles") or [], hint=_LIST_HINTS["cycle"]))
        lines.extend(list_lines("orphan", rep.get("orphans") or [],
                                hint=_LIST_HINTS["orphan"]))
        lines.extend(list_lines("conflict", rep.get("conflicts") or [],
                                hint=_LIST_HINTS["conflict"]))
        if rep.get("marked"):
            lines.append(f"  本轮标环：{len(rep['marked'])} 行（relation='cycle' / basis='conflict'）")
        if rep.get("restored"):
            lines.append(f"  本轮还原：{len(rep['restored'])} 行（已不成环，标记撤回）")
    return "\n".join(lines) + "\n"


def run_cli(conn: sqlite3.Connection, action: str = "report", *, host: str | None = None,
            apply_scan: bool = False) -> str:
    """`fish lineage scan|report` 的**唯一装配点**：选宿主 → 调入口 → 渲染成文本。

    为什么装配放在这里而不是 `fish.cmd_lineage`：`fish.py` 有 J4 行数窗口（11250），
    而"哪些宿主、默认 dry-run、用哪一份格式化"是这一层的领域知识，不是 CLI 的。
    `fish.cmd_lineage` 只负责开连接、兜异常、定退出码——三件 CLI 该做的事。
    """
    hosts = [host] if host else list(HOSTS)
    if action == "scan":
        reports = [scan(item, dry_run=not apply_scan, conn=conn) for item in hosts]
    else:
        reports = [report(conn, item) for item in hosts]
    return format_report(reports)
