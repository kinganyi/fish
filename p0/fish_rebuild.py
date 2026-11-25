#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从事件重放重建 `code_index` 投影（2026-11-11 从 `fish.py` 抽出，错题本 E-11）。

## 为什么单独成模块

它是 `fish.py` 里**最长也最危险**的函数：406 行，且外部审查 🔴9 就出在它身上
（只重放 `memory.owner_fixed`、不重放 `citizen.merged` / `citizen.retired`，
于是一次 rebuild 会静默撤销人工合并）。留在一个 14.5k 行的文件里，改它的时候
没人愿意读完上下文——而它恰恰是"读不完全就会写错"的那种函数。

## 2026-11-25（外部审查 P1-1）：它必须先是**可证明无害的**恢复工具

审查实测的三个后果，同一个根因——"重建把"重放出来的值"当成唯一真相，不看眼前的事实"：

  1. **owner 降级**：`actor.get("citizen_id") or old.get("owner")` 里 `"unattributed"`
     是**非空字符串**，`or` 认它 → 生产 8 条真归属被覆写成"未归属"。修法见 `_pick_owner`。
  2. **跳过对账就提交**：旧版在重放结束处 `conn.commit()`，对账发生在提交**之后**——
     对账报出不一致时，那笔坏数据已经落盘（审查实测 480/517 就是这么进去的）。
     修法：整个重放 + 对账 + FTS 在**一个事务**里，对账不一致 → `rollback` + 退出码 2。
  3. **没有 dry-run、没有备份**：`fish rebuild` 唯一行为就是写。修法：缺省
     `dry-run`（跑完全流程后回滚，只报"将会变动的行"），写必须 `--apply`，
     且写前用 `conn.backup()` 留 `_backups/index-before-rebuild-*.sqlite`。

## 依赖方向（J2：`fish → fish_kb → fish_common`）

本模块**不 import `fish`**。它需要的东西全部来自下层：

  · `fish_common` —— `body_relpath`（正文路径的唯一规则）、`resolve_owner`（系统主人）、
    `split_code_prefix`（编码拆前缀，右锚定）、`reindex_fts`（重建后同步全文索引）；
  · `fish_trust` —— `load_ownership_merge_map` / `resolve_merged`（🔴9 的归属重放）。

拆包的第一步（同日）已经把前三个纯函数下沉到 `fish_common`——正是为了让这里
不需要反向依赖。`fish.py` 通过 `from fish_rebuild import rebuild_projection`
保持同名，所有既有调用点（CLI `fish rebuild`、各测试）一个字都不用改。
"""
import datetime
import json
import sqlite3
import time
from pathlib import Path

from fish_common import (UNATTRIBUTED, body_relpath, citizen_ids_known, reindex_fts, resolve_owner, split_code_prefix)
from fish_trust import load_ownership_merge_map, resolve_merged

class RebuildError(Exception):
    """重建中止（**投影未被修改**）。

    ## 为什么要有专门的异常类型（外部审查 F-P0-2）

    重建是"恢复手段"。它失败时必须能被上层**明确区分**于"重建成功但有偏差"——
    前者应该让 CLI 以非零退出并保持数据库原样，后者只写进报告。

    旧实现的失败模式正好相反：删完投影再逐条重放，畸形事件一来就抛裸异常，
    而投影已经被清空，调用方甚至不知道"库现在是空的"。

    `detail` 带诊断信息（坏事件 seq 列表、hint），供 CLI/MCP 原样透出。
    """

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.detail = detail or {}


def _db_file(conn):
    """这个连接指向的数据库文件；内存库 / 拿不到 → `None`。"""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and path:
                return path
    except sqlite3.Error:
        # 内存库 / 连接已关都拿不到路径，"没有可备份的库文件"是正常态；
        # 调用方（`render_report`）会把"写前快照未生成"显式报出来，不静默。
        pass  # A档：见上——报告里有显式提示，这里不是"吞掉错误"
    return None


def backup_before_rebuild(conn) -> tuple:
    """写前快照：`<data>/_backups/index-before-rebuild-<stamp>.sqlite` → `(路径, 行数)`。

    **用 SQLite 官方 backup API，不做文件拷贝**（P1-1 明确要求）：WAL 模式下直接
    copy 会拿到**撕裂快照**（写了一半的 WAL 还没合并），还原回去是一份不一致的库。
    命名与 `fish_db` 的 `index-before-*.sqlite` 同风格；**永不自动删**——回滚点是人的
    凭据，不是缓存。备份失败 → 异常向上抛（**宁可不写**）：本次修复的回退完全依赖它。
    内存库（部分测试）没有可备份的文件 → `(None, 0)`，调用方要在报告里说明。
    """
    path = _db_file(conn)
    if not path:
        return None, 0
    if conn.in_transaction:
        # `conn.backup()` 要求源连接没有开着的写事务，而快照的语义本来就是"落到磁盘的
        # 那一刻"——所以先把此前**已完成的写**收尾，而不是回滚它们（回滚会连调用方
        # 刚做过的写一起丢掉）。这一步让"备份 = 重建前库里真实内容"成立。
        conn.commit()
    out_dir = Path(path).parent / "_backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"index-before-rebuild-{time.strftime('%y%m%d-%H%M%S')}.sqlite"
    dst = sqlite3.connect(str(dest))
    try:
        conn.backup(dst)                                    # WAL 也能拿到一致快照
        rows = dst.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    finally:
        dst.close()
    return str(dest), rows


def _pick_owner(actor, old_owner, valid_citizen_ids):
    """重建时的 owner 优选（P1-1）：**只有有效公民才允许覆盖**。

    旧写法 `actor.get("citizen_id") or old.get("owner") or "unknown"` 把 actor 当成
    无条件权威，而 `"unattributed"` 是个非空字符串，`or` 认它——于是"这一条事件没带
    凭证"就把快照里那个真实的归属覆写成"未归属"（生产实测 8 条，一次维护动作）。
    **恢复工具制造数据损伤**是最坏的一类缺陷：它披着"修复"的外衣。

    四级口径（与 `fish_common.citizen_ids_known` 的"有效性唯一入口"一致）：
      1. actor 是**有效公民**（非空、非 unattributed/unknown、且在 `citizens_all` 里）
         → 用它（写入口可信时，事件是更新的证据）；
      2. 否则保留重建前的投影值（维护动作**不改**它不认识的东西）；
      3. 两者都没有 → `unknown`（诚实：不知道）；
      4. `unattributed` 只有在快照本来就是它时才保持（"允许无主，但不许假归属"，
         见 `test_attribution_policy`）。

    `valid_citizen_ids` 为空 = 库里没有任何公民知识（空库/首建/部分测试）：此时不凭
    猜测否决 actor，与既有归一逻辑（`if valid_citizen_ids and ...`）同口径。
    """
    cid = (actor or {}).get("citizen_id")
    if cid and cid not in ("unknown", UNATTRIBUTED):
        if not valid_citizen_ids or cid in valid_citizen_ids:
            return cid
    return old_owner or "unknown"


def _settle(conn, rep, *, apply):
    """对账之后决定这一炉是**提交还是回滚**（P1-1 的核心，顺序不能换）。

      1. 对账不一致 → `rollback` + `aborted`（CLI 退出码 2）。**这条必须排在 apply
         之前**：先 commit 再对账 = 把"不一致"永久写进库，正是审查指出的损伤形状
         （生产实测 480/517 已提交、exit 1）。
      2. `apply` 且一致 → `commit`。
      3. 缺省（dry-run）→ 一律 `rollback`：跑完全流程只报结果，**一个字节都不落**。

    为什么把"决定权"抽成函数：判据要能**变异**它（"先 commit 后对账"必须红）。
    """
    rep["rows_after"] = rep["total"]
    if rep["mismatch"]:
        conn.rollback()
        rep["applied"] = False
        rep["aborted"] = True
        rep["would_change"] = list(rep["mismatch"])
        return rep
    rep["aborted"] = False
    rep["applied"] = bool(apply)
    rep["would_change"] = []
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return rep


# F-P1-X（评审 🟠-1）：预检覆盖的事件类型。
# 判据 `test_precheck_types_cover_every_replayed_type` 会扫源码里的重放类型，
# 断言它们全在这里——谁新增一类重放却忘了加进来，那条就红。
# 第二轮评审 🟠-1 剩余：**形状对不等于类型对**。`{"codes": 123}` 虽然能解析成
# dict，重放时会抛 TypeError——而且是在 DELETE 之后。这和 🟠-1 当初是同一个
# 失败形状（只修了表面）。所以按事件类型声明字段契约：重放怎么用它，预检就怎么验它。
_PRECHECK_FIELD_TYPES = {
    "memory.recorded": {"code": (str,)},
    "memory.superseded": {"old": (str,), "new": (str,)},
    "memory.retired": {"code": (str,), "status": (str,)},
    "memory.accessed": {"codes": (list, tuple)},
    "memory.dormant": {"codes": (list, tuple)},
}

_PRECHECK_TYPES = ("memory.recorded", "memory.superseded", "memory.retired",
                   "memory.accessed", "memory.dormant")



def rebuild_projection(conn, root: Path, *, apply: bool = True) -> dict:
    """从事件重放重建 `code_index` 投影，返回对账报告。

    `apply`（P1-1，2026-11-25 外部审查）：
      · `True`（库 API 的缺省，既有调用点不变）→ 对账一致才 `commit`；
        写前先 `backup_before_rebuild()` 留快照；
      · `False`（`fish rebuild` 的缺省 = dry-run）→ 全流程照跑，结尾
        `rollback`，报告里的数字是"**将会**如此"，库一个字节都不变。
    报告字段 `applied` / `aborted` / `dry_run` / `would_change` / `backup_path`
    是这次新增的机器可读结论（CLI 退出码就按 `aborted` 给）。

    为什么不是"教科书式纯重放"（三条都是取证出来的，不是偷懒）：

    1. **历史事件缺字段**：2026-10-22 之前写入的 `memory.recorded` 不含 `occurred_at`
       （见 07 D-11）。事件不可改（ADR-001），所以时间只能用重建前的投影值兜底，
       再退到正文文件 mtime，最后退到事件时间；三种兜底各记一个计数，不静默。
    2. **访问记录的来源分两段**：2026-10-22 之前，访问只 UPDATE 投影、没有事件；之后每次访问
       都写 `memory.accessed`（见 07 D-12）。所以重建策略是「**快照优先**（它是累积态，已含
       历史与事件两者的效果），快照没覆盖到的条目再从事件补」。既没有快照、又早于该日期的
       访问，仍然无从恢复——不假装能恢复。
    3. **只重建 `code_index`**：`code_registry.last_seq` 是编码分配状态（重放会把 last_seq
       归零 → `next_code` 撞号），`citizens.is_owner` 有唯一性约束且不在事件里。
       所以这两张表不参与重建。

    重建后逐条对账 owner / occurred_at / status / superseded_by / session_actor，
    不一致写进报告（对账判据见 03 §G「重建前后一致」）。
    """
    root = Path(root)
    rep = {
        "projected": 0, "superseded_events": 0, "retired_events": 0,
        "from_payload": 0, "from_projection": 0, "from_file": 0, "from_event": 0,
        "access_preserved": 0, "owner_missing": 0,
        "owner_normalized": 0, "owner_normalized_list": [], "normalized_codes": set(),
        "merged_codes": set(),
        "occurred_corrected": 0, "occurred_corrected_list": [], "corrected_codes": set(),
        "access_from_events": 0, "dormant_from_events": 0,
        "atts_from_projection": 0, "atts_degraded": 0,
        "recorded_at_normalized": 0, "recovered": [], "mismatch": [], "snapshot_size": 0,
    }

    # ---- 0a. 写前快照（P1-1）：**必须在任何写动作之前** ----
    # `conn.backup()` 只能在"没有开着写事务"时做，所以放在最前面（此时一行都没动）。
    # dry-run 不备份：dry-run 的承诺是"零写"，连备份文件也不该产生。
    rep["dry_run"] = not apply
    rep["rows_before"] = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    rep["backup_path"], rep["backup_rows"] = (
        backup_before_rebuild(conn) if apply else (None, 0))

    # ---- 0. 编码改名映射（2026-10-25 数据治理）----
    # events 是 append-only（ADR-001），历史 `memory.recorded` 里存的是**旧编码**
    # （ANYI-*）；而 2026-10-25 的治理把 153 条编码改成了 anyi-studio-* / 大肥鱼生命系统-*。
    # **不应用映射 = 重建把治理结果一键撤销**，所以必须在重放前把映射建好。
    # 映射本体在 `code.renamed.batch` 事件里（`project.renamed` 只存 sha256 指纹，不够用）。
    rename_map = {}
    renamed_events = 0
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='code.renamed.batch' ORDER BY seq"):
        try:
            m = json.loads(payload_s).get("mapping") or {}
        except (ValueError, TypeError):
            m = {}
        for _old, _new in m.items():
            if _old and _new:
                rename_map[_old] = _new
        renamed_events += 1
    rep["renamed_map_size"] = len(rename_map)
    rep["renamed_events"] = renamed_events
    rep["renamed_applied"] = 0

    # ---- 0b. 公民合并/迁移映射（外部审查 🔴9 · 2026-11-11 修）----
    # 问题：`citizen.merged` / `citizen.retired{--migrate-to}` 已经把归属搬走了，
    # 但 rebuild 只重放 `memory.owner_fixed`，**不重放这两类事件**。
    # 后果是**一次 rebuild 静默撤销人工合并**：owner 回到原始 actor 的公民，
    # 而那个公民此时 status 已是 `merged_into:<dst>` / `retired` —— 留下指向
    # 已退役公民的幽灵归属（报告 07 D-10 记载 2026-10-21 真跑时伤过 3 条记忆）。
    #
    # 修法与上面的 `code.renamed.batch` 同款：**重放前先把映射建好**，重放时套用它。
    # 事件 payload 结构（见写入点）：
    #   citizen.merged  → {"src": <被并入>, "dst": <并入到>, "moved": {...}}
    #   citizen.retired → {"migrated_to": <接收方或 None>, ...}
    # 外部审查 🔴9（2026-11-11）：重放前先建"公民 → 最终公民"映射，
    # 否则一次 rebuild 就把人工合并静默撤销（owner 回到已退役的 src 公民）。
    # 实现抽在 `fish_trust`（J4/E-11：fish.py 不再膨胀）。
    merge_map, merge_events = load_ownership_merge_map(conn)
    rep["merged_map_size"] = len(merge_map)
    rep["merged_events"] = merge_events
    rep["owner_merged"] = 0
    rep["owner_merged_list"] = []

    # ---- 1. 重建前快照：历史事实兜底 + 访问记录的唯一来源 ----
    # 2026-10-25：快照要存**足够补回一整行**的字段。原先只存了 owner/时间/状态这些
    # "对账用"字段，于是当某条在重放时丢了（见下面 §4b 的原因），连补都补不回来。
    snap = {}
    for r in conn.execute(
            "SELECT code,owner,occurred_at,recorded_at,status,superseded_by,session_actor,"
            "attachments,last_accessed_at,access_count,dormant,"
            "title,project,mem_type,visibility,tags,file_path FROM code_index"):
        snap[r[0]] = {"owner": r[1], "occurred_at": r[2], "recorded_at": r[3],
                      "status": r[4], "superseded_by": r[5], "session_actor": r[6],
                      "attachments": r[7], "last_accessed_at": r[8],
                      "access_count": r[9] or 0, "dormant": r[10] or 0,
                      "title": r[11], "project": r[12], "mem_type": r[13],
                      "visibility": r[14], "tags": r[15], "file_path": r[16]}
    rep["snapshot_size"] = len(snap)

    # ---- 1b. **先校验全部事件，再动投影**（外部审查 F-P0-2）----
    #
    # 旧实现：`DELETE FROM code_index` + `commit()`，然后才逐条重放 events。
    # 重放中任一畸形事件抛异常 → **投影已被清空**，而快照只在内存里，
    # 进程一退就永久丢失。审查方实测：一条畸形事件能让投影从 1 变 0，而 FTS 仍指旧。
    #
    # 恢复手段本身不能变成破坏手段。所以先做一遍**只读预检**：所有
    # `memory.recorded` 事件必须能解析成 dict 且带 `code`。任一失败就在这里停，
    # **一个字节都还没删**。这比"删了再回滚"更稳——回滚依赖连接状态，
    # 预检不依赖任何东西。
    # **F-P1-X（2026-11-18 独立评审 🟠-1）：预检必须覆盖所有会被重放解析的事件类型。**
    #
    # 原实现只扫 `memory.recorded`，而重放阶段还会裸 `json.loads` 另外三类
    # （`memory.superseded` / `memory.retired` / `memory.accessed`+`memory.dormant`）。
    # 后果实测：注入一条畸形 `memory.retired` → JSONDecodeError **发生在 DELETE 之后**，
    # 同一连接内投影 2→1 行，随后 commit 固化。"投影未被修改"那句话在那条路径上是假的。
    #
    # 判据要和实现一样长：预检的类型清单必须与重放用到的类型清单逐项相等，
    # 否则"我预检过了"永远只是"我预检了我想到的那部分"。
    # 提成模块级：判据要能读到它，否则"预检覆盖哪些类型"只能靠人肉核对。
    PRECHECK_TYPES = _PRECHECK_TYPES
    # 只有 `memory.recorded` 必须带 code（其余按 code 集合或 id 定位，不强求）
    NEEDS_CODE = ("memory.recorded",)

    marks = ",".join("?" for _ in PRECHECK_TYPES)
    bad_events = []
    for seq, ev_type, payload_s in conn.execute(
            "SELECT seq,type,payload FROM events WHERE type IN (%s)" % marks,
            PRECHECK_TYPES).fetchall():
        try:
            p = json.loads(payload_s or "{}")
        except Exception as exc:
            bad_events.append((seq, ev_type, f"JSON 解析失败: {type(exc).__name__}"))
            continue
        if not isinstance(p, dict):
            bad_events.append((seq, ev_type, "payload 不是对象"))
            continue
        if ev_type in NEEDS_CODE and not p.get("code"):
            bad_events.append((seq, ev_type, "payload 缺 code"))
            continue
        # 字段类型：重放会怎么用它，这里就怎么验它（见 _PRECHECK_FIELD_TYPES 注释）
        _bad_field = None
        for _field, _types in _PRECHECK_FIELD_TYPES.get(ev_type, {}).items():
            if _field not in p:
                continue
            if not isinstance(p[_field], _types):
                _bad_field = (_field, type(p[_field]).__name__,
                              "/".join(t.__name__ for t in _types))
                break
        if _bad_field:
            bad_events.append(
                (seq, ev_type,
                 f"字段 {_bad_field[0]} 类型是 {_bad_field[1]}，应为 {_bad_field[2]}"))
    if bad_events:
        # 抛出去而不是"跳过坏事件继续"：跳过会让投影少一条，而少的那条
        # 看起来和"本来就没有"完全一样——静默丢数据比响亮失败糟得多。
        by_type = {}
        for _seq, ev_type, _why in bad_events:
            by_type[ev_type] = by_type.get(ev_type, 0) + 1
        raise RebuildError(
            f"重建中止：{len(bad_events)} 条事件无法解析"
            f"（{', '.join(f'{k}×{v}' for k, v in sorted(by_type.items()))}），"
            f"投影**未被修改**",
            detail={"bad_events": bad_events[:10],
                    "checked_types": list(PRECHECK_TYPES),
                    "hint": "这几条事件不修就不能重建——跳过它们会让投影少行，"
                            "而少的那行与「本来就没有」看起来完全一样。\n"
                            "先按 bad_events 里的 seq 定位并修好 payload。"})
    rep["precheck_ok"] = True

    # **F-P1-X（第三轮评审）：删投影 + 重放必须可回滚。**
    #
    # 预检只能挡住它**声明过**的字段类型。评审官实测 `body_ref: 123`：
    # 形状合法 → 预检放过 → 重放时 TypeError → **而 DELETE 已经执行了**。
    # 实测：坏事件排第一条时投影 2→0 且调用方 commit 后永久丢失。
    #
    # 根子不在"字段表漏了一行"——枚举永远会漏下一行。
    # 所以改成结构性保证：**删除与重放包进一个事务，异常就回滚**。
    try:
        conn.execute("DELETE FROM code_index")

        # owner 归一所需的知识：哪些才是真公民（见 07 D-13）。
        # 库里没有公民知识时（空库 / 测试）不做归一——不凭猜测改写归属。
        # 2026-11-11（序3 归档验证暴露）：必须含 `citizens_archive` 里的冷公民。
        # 只用热表时，已归档公民名下的记忆会被判成"无效 owner"→ 归一成系统主人
        # （实测：归档 12 个公民后 rebuild 改写了 6 条记忆的归属，D-10 同类事故）。
        valid_citizen_ids = citizen_ids_known(conn)
        sys_owner = resolve_owner(conn)

        # ---- 2. 重放 memory.recorded ----
        rows = conn.execute(
            "SELECT ts,actor,payload FROM events WHERE type='memory.recorded' ORDER BY seq"
        ).fetchall()
        for ts, actor_s, payload_s in rows:
            p = json.loads(payload_s)
            try:
                actor = json.loads(actor_s or "{}")
            except Exception:
                actor = {}
            code = p["code"]
            # 应用改名映射（2026-10-25 治理）。**必须在 `snap.get` 之前**：
            # 快照的键是新编码，用旧编码去查会查不到，于是 owner/occurred_at 的兜底链全断。
            if code in rename_map:
                code = rename_map[code]
                rep["renamed_applied"] += 1
            old = snap.get(code, {})

            # 时间事实的兜底链（2026-10-22 调整顺序）：
            #   payload → 重建前投影（**仅当它不晚于记录时间**）→ 事件时间 → 正文文件 mtime
            # 为什么要「不晚于」：occurred_at 晚于 recorded_at 在逻辑上不可能；出现就说明投影值
            # 被历史那版坏重建压平过（实证 3 条被写成重建时刻）。这种值不可信，以事件时间为准。
            occurred = p.get("occurred_at")
            if occurred:
                rep["from_payload"] += 1
            elif old.get("occurred_at") and old["occurred_at"] <= ts:
                occurred = old["occurred_at"]
                rep["from_projection"] += 1
            elif old.get("occurred_at"):
                occurred = ts
                rep["from_event"] += 1
                rep["occurred_corrected"] += 1
                rep["corrected_codes"].add(code)
                rep["occurred_corrected_list"].append(f"{code}: {old['occurred_at']} → {ts}")
            else:
                ref = p.get("body_ref") or ""
                f = (root / ref) if ref else None
                if f is not None and f.exists():
                    occurred = datetime.datetime.fromtimestamp(
                        f.stat().st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    rep["from_file"] += 1
                else:
                    occurred = ts
                    rep["from_event"] += 1

            # **P1-1（2026-11-25 外部审查）**：owner 优选收在 `_pick_owner` 里。
            # 旧的一行 `actor.get("citizen_id") or old.get("owner") or "unknown"`
            # 把 `"unattributed"`（非空字符串）当成了有效覆盖值 → 生产 8 条真归属被
            # 降级成"未归属"。现在 actor 只有是**有效公民**才允许覆盖快照。
            owner = _pick_owner(actor, old.get("owner"), valid_citizen_ids)
            # 外部审查 🔴9（2026-11-11）：**先解析合并链，再判"是否有效公民"**。
            # 不解析 = rebuild 把人工合并静默撤销 —— owner 回到原始 actor 的公民，
            # 而那个公民此刻 status 已是 `merged_into:<dst>`，留下幽灵归属。
            if owner not in ("", "unknown", None):
                _resolved = resolve_merged(merge_map, owner)
                if _resolved != owner:
                    rep["owner_merged"] += 1
                    rep["owner_merged_list"].append(f"{code}: {owner!r} → {_resolved!r}")
                    # P1-1：合并/迁移是**有据可查**的归属变化，对账时必须豁免，
                    # 否则闸门会把"正确地重放了合并"判成不一致并整炉回滚（R9-3 实测）。
                    rep["merged_codes"].add(code)
                    owner = _resolved
            # owner 必须是有效公民（07 D-13）。有公民知识时把无效值归一到系统主人并逐条记录，
            # 而不是把它写回投影（否则会留下指向幽灵公民的归属）。
            if (valid_citizen_ids and owner not in valid_citizen_ids
                and owner not in ("unknown", UNATTRIBUTED)):   # 2026-11-11：豁免「未归属」
                rep["owner_normalized"] += 1
                rep["owner_normalized_list"].append(f"{code}: {owner!r} → {sys_owner!r}")
                rep["normalized_codes"].add(code)
                owner = sys_owner or "unknown"
            if owner in ("", "unknown", None):
                rep["owner_missing"] += 1

            # 项目：**编码前缀优先**；`body_ref` 只在 code 没被改名映射过时才拿来兜底。
            # 2026-10-25 修：原文是「body_ref 优先（重命名场景比按前缀切更忠实）」，但改名后
            # body_ref 是**旧路径**（`projects/ANYI/...`），而 code 已映射成 `anyi-studio-*`——
            # 于是 body_ref 把正确的 project 覆盖回 `ANYI`。副本实测：rebuild 后
            # anyi-studio 108→8、大肥鱼 55→0（几乎全部被打回旧项目），212 条挂在 ANYI 下。
            project = split_code_prefix(code)
            if code == p.get("code"):
                # code 没被映射：沿用老逻辑（历史重命名的场景 body_ref 更忠实）
                ref = p.get("body_ref") or ""
                if ref.startswith("projects/") and ref.count("/") >= 2:
                    project = ref.split("/")[1]
            if not project:
                project = code.split("-")[0]

            # file_path 的取法（2026-10-25 三次迭代后的最终版）：
            #   ① **优先用快照**——它是"文件此刻真的在哪"这个唯一事实。
            #      `body_relpath` 用 title 算，而事件里的 title 是**记录时**的；
            #      重命名脚本用的是 `code_index.title`（可能被 memory.revised 改过）。
            #      两者不一致时重算会算出不同路径 → 重建即断链（副本实测 73 条）。
            #   ② 快照没有（新库/首建）时，code 没被映射就信 body_ref；
            #   ③ 都不行才用 `body_relpath` 重算。
            snap_fp = (snap.get(code) or {}).get("file_path") or ""
            if snap_fp and (root / snap_fp).exists():
                file_path = snap_fp
            elif code == p.get("code") and p.get("body_ref") and (root / p["body_ref"]).exists():
                file_path = p["body_ref"]
            else:
                file_path = body_relpath(project, code, p.get("title"))

            session_actor = p.get("session_actor", old.get("session_actor"))
            # 附件：新事件 payload 存完整元数据（含 blob 路径），旧事件只有 name+sha256。
            # 旧事件不完整时用重建前投影兜底；连投影都没有，就降级保留 name+sha256 并计数。
            atts = p.get("attachments")
            full = bool(atts) and all(isinstance(a, dict) and a.get("blob") for a in atts)
            if full:
                atts_s = json.dumps(atts, ensure_ascii=False)
            elif old.get("attachments"):
                atts_s = old["attachments"]
                rep["atts_from_projection"] += 1
            else:
                atts_s = json.dumps(atts, ensure_ascii=False) if atts else None
                if atts:
                    rep["atts_degraded"] += 1

            conn.execute(
                "INSERT OR IGNORE INTO code_index (code,title,project,owner,mem_type,visibility,"
                "status,tags,file_path,occurred_at,recorded_at,session_actor,attachments) "
                "VALUES (?,?,?,?,?,?,'active',?,?,?,?,?,?)",
                (code, p.get("title"), project, owner,
                 p.get("mem_type", "fact"),
                 # G-06（安全审计 2026-10-29）：旧版 `p.get("visibility", "shared")`
                 # 在事件**缺键或为 null** 时兜底成 shared —— 生产 322 条事件里有 7 条
                 # 没有 visibility 键（2.2%），一次 rebuild 就会把 private **静默公开**。
                 # 回落到重建前快照里的历史值（owner/attachments 都是这么做的），
                 # 只有"历史也没有"时才用 shared。
                 (p.get("visibility")
                  or (snap.get(code) or {}).get("visibility")
                  or "shared"),
                 json.dumps(p.get("tags", []), ensure_ascii=False), file_path,
                 occurred, ts, session_actor, atts_s))
            rep["projected"] += 1

        # ---- 3. 重放 memory.superseded（替代链靠事件恢复，不靠猜）----
        # 2026-10-25：old/new **都要过改名映射**，否则事件里的旧编码会把映射后的条目
        # "悬空"——UPDATE 找不到行，静默失效（重建后 status 全变回 active）。
        for (payload_s,) in conn.execute(
                "SELECT payload FROM events WHERE type='memory.superseded' ORDER BY seq").fetchall():
            p = json.loads(payload_s)
            old_code, new_code = p.get("old"), p.get("new")
            if not old_code:
                continue
            old_code = rename_map.get(old_code, old_code)
            if new_code:
                new_code = rename_map.get(new_code, new_code)
            conn.execute(
                "UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
                (new_code, old_code))
            rep["superseded_events"] += 1

        # ---- 3b. 重放 memory.retired（作废 / withdrawn，ADR-003 状态机）----
        for (payload_s,) in conn.execute(
                "SELECT payload FROM events WHERE type='memory.retired' ORDER BY seq").fetchall():
            p = json.loads(payload_s)
            code = p.get("code")
            if not code:
                continue
            code = rename_map.get(code, code)      # 2026-10-25：作废事件也要过改名映射
            if p.get("status", "withdrawn") == "withdrawn":
                conn.execute("UPDATE code_index SET status='withdrawn' WHERE code=?", (code,))
                rep["retired_events"] += 1

        # ---- 4a. 访问记录：快照优先（它是累积态，已含历史与事件两者的效果）----
        restored = set()
        for code, s in snap.items():
            if not (s["last_accessed_at"] or s["access_count"] or s["dormant"]):
                continue
            cur = conn.execute(
                "UPDATE code_index SET last_accessed_at=?, access_count=?, dormant=? WHERE code=?",
                (s["last_accessed_at"], s["access_count"], s["dormant"], code))
            if cur.rowcount:
                rep["access_preserved"] += 1
                restored.add(code)

        # ---- 4b. 快照里没有访问信息的条目：从事件补齐 ----
        # 2026-10-22 起访问与休眠都有事件来源（memory.accessed / memory.dormant），
        # 所以「投影整个被毁、没有快照」时这两类状态不再必然丢失（修 07 D-12）。
        # 只对未被快照覆盖的条目重放，避免与累积值重复计数。
        for ts, payload_s in conn.execute(
                "SELECT ts,payload FROM events WHERE type IN ('memory.accessed','memory.dormant') "
                "ORDER BY seq"):
            p = json.loads(payload_s)
            if p.get("codes"):                       # memory.accessed
                counted = bool(p.get("counted", True))
                for c in p["codes"]:
                    if c in restored:
                        continue
                    if counted:
                        conn.execute(
                            "UPDATE code_index SET last_accessed_at=?, dormant=0, "
                            "access_count=COALESCE(access_count,0)+1 WHERE code=?", (ts, c))
                    else:
                        conn.execute(
                            "UPDATE code_index SET last_accessed_at=?, dormant=0 WHERE code=?", (ts, c))
                    rep["access_from_events"] += 1
            elif p.get("code"):                      # memory.dormant
                if p["code"] in restored:
                    continue
                conn.execute("UPDATE code_index SET dormant=1 WHERE code=?", (p["code"],))
                rep["dormant_from_events"] += 1
    except BaseException:
        # 回滚到删除之前。这里**不吞异常**：原始错误必须传上去。
        conn.rollback()
        raise

    # ---- 4b. 快照补回：事件里找不到来源的条目（2026-10-25）----
    # 为什么会有这种条目：**编码被复用过**。典型案例——
    #   seq=391  `ANYI-261025-01` 是第一棒的原始记忆
    #   seq=458  `ANYI-261025-01` 又被 ZCode 重写（同编码、不同内容）
    # 改名映射只能把 `ANYI-261025-01` 映射到**一个**新编码，于是原始那条在重放时
    # 撞上已被占用的编码、`INSERT OR IGNORE` 丢弃 → 凭空少一条。
    #
    # 处理原则：**快照是累积态**（rebuild 的 docstring 早就这么写了，owner/occurred_at
    # 的兜底也走它）。事件里还原不出来的，就用快照补回整行并计数——**不静默丢**。
    restored_from_snap = 0
    for code, s in snap.items():
        if conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone():
            continue
        # 补回也要做 owner 归一——否则"从快照补回的条目"会绕过 07 D-13 的规则，
        # 变成 healthcheck 里永远消不掉的最后一条（2026-10-25 实测：归一 10→1，
        # 剩下的那一条正是从快照补回的）。
        r_owner = s["owner"]
        if (valid_citizen_ids and r_owner not in valid_citizen_ids
            and r_owner not in ("unknown", UNATTRIBUTED)):  # 同上
            rep["owner_normalized"] += 1
            rep["owner_normalized_list"].append(f"{code}: {r_owner!r} → {sys_owner!r}")
            rep["normalized_codes"].add(code)
            r_owner = sys_owner or "unknown"
        conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,session_actor,attachments,"
            "last_accessed_at,access_count,dormant,superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, s["title"], s["project"], r_owner, s["mem_type"], s["visibility"],
             s["status"], s["tags"], s["file_path"], s["occurred_at"], s["recorded_at"],
             s["session_actor"], s["attachments"], s["last_accessed_at"],
             s["access_count"], s["dormant"], s["superseded_by"]))
        restored_from_snap += 1
        rep["recovered"].append(f"{code}: 事件里无来源，从快照补回")
    # P1-1：**这里不再 commit**。提交点只有一个（`_settle`），否则"先提交后对账"的
    # 损伤形状会从这道口子回来：对账在提交之后 → 报出不一致时已经落盘。
    rep["restored_from_snapshot"] = restored_from_snap
    if restored_from_snap:
        print(f"  从快照补回 {restored_from_snap} 条（事件里找不到来源：编码被复用过的）")

    # ---- 4c. 重放 memory.owner_fixed（EVAL-261025-21 修复 · 2026-10-25 QA 查出）----
    # 为什么必须在 §4b（快照补回）**之后**：修正事件是对"最终归属"的权威声明，
    # 快照补回的行带的是旧 owner——先补回再覆盖，才不会让修正被快照盖掉。
    # 为什么必须在 §5 对账**之前**：对账要看到修正后的值，否则每次 rebuild
    # 都会把这条修正报成 mismatch（而且下一次还会回退）。
    # 为什么这不算"改历史"：events 一个字节不动，修正本身就是追加进来的事件；
    # 重放只是让投影忠实反映"事件流的最终状态"。
    owner_fixed_codes = set()
    owner_fixed_skipped = []
    for (payload_s,) in conn.execute(
            "SELECT payload FROM events WHERE type='memory.owner_fixed' ORDER BY seq"):
        try:
            p = json.loads(payload_s)
        except (ValueError, TypeError):
            continue
        code = p.get("code")
        new_owner = p.get("new_owner")
        if not code or not new_owner:
            continue
        code = rename_map.get(code, code)
        if valid_citizen_ids and new_owner not in valid_citizen_ids:
            # 目标公民已被归档/删除：不把归属写回投影（会成为幽灵引用），
            # 跳过并计数——不静默。
            owner_fixed_skipped.append(f"{code}: 目标公民 {new_owner!r} 不在库中，跳过")
            continue
        cur = conn.execute("UPDATE code_index SET owner=? WHERE code=?",
                           (new_owner, code))
        if cur.rowcount:
            owner_fixed_codes.add(code)
            rep["owner_fixed_applied"] = rep.get("owner_fixed_applied", 0) + 1
        else:
            owner_fixed_skipped.append(f"{code}: 重建后不存在该编码，跳过")
    # P1-1：同上——提交点统一收在 `_settle`（一致才 commit，不一致 rollback + 退出码 2）。
    rep["owner_fixed_skipped"] = owner_fixed_skipped

    # ---- 5. 对账：重建前后必须一致的五件事 ----
    for code, s in snap.items():
        row = conn.execute(
            "SELECT owner,occurred_at,status,superseded_by,session_actor,recorded_at "
            "FROM code_index WHERE code=?", (code,)).fetchone()
        if not row:
            rep["mismatch"].append(f"{code}: 重建后消失了")
            continue
        owner, occurred, status, sup, sess, rec = row
        for field, before, after in (
                ("owner", s["owner"], owner),
                ("occurred_at", s["occurred_at"], occurred),
                ("status", s["status"], status),
                ("superseded_by", s["superseded_by"], sup),
                ("session_actor", s["session_actor"], sess)):
            if field == "owner" and code in rep["normalized_codes"]:
                continue      # 已在 owner_normalized 里单独报告，不算不一致
            if field == "owner" and code in rep["merged_codes"]:
                continue      # 归属被 citizen.merged / retired{--migrate-to} 显式搬过（🔴9）
            if field == "owner" and code in owner_fixed_codes:
                continue      # 归属被 memory.owner_fixed 显式修正过：新旧不同是预期
            if field == "occurred_at" and code in rep["corrected_codes"]:
                continue      # 已在 occurred_corrected 里单独报告，不算不一致
            # 空 → 有值 = 从事件里**恢复**了投影丢掉的字段（2026-10-22 实测发生过：
            # 旧重建把 3 条记忆的 owner 清成空串，事件里仍留着真实 actor）。这不算不一致。
            if before in (None, ""):
                if after not in (None, ""):
                    rep["recovered"].append(f"{code}: {field} 空 → {after!r}")
                continue
            if before != after:
                rep["mismatch"].append(f"{code}: {field} {before!r} → {after!r}")
        if s["recorded_at"] and s["recorded_at"] != rec:
            rep["recorded_at_normalized"] += 1

    rep["total"] = conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    rep["superseded_total"] = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE status='superseded'").fetchone()[0]

    # ---- 6. 提交 / 回滚（P1-1）：**对账是闸门，不是日志** ----
    # 旧版顺序是"提交 → 对账 → 报 mismatch 然后 exit 1"，于是"报出来"的时候库里
    # 已经是坏数据（审查实测 480/517）。现在对账在同一事务内完成，不一致直接撤。
    # 这一步必须排在全文索引**之前**：`reindex_fts` 内部逐行 commit
    # （`fish_common._index_one_fts(commit=True)`），先跑它 = 先把事务落盘，
    # 之后无论对账说什么都回不去了——那正是本次要修的形状。
    _settle(conn, rep, apply=apply)

    # ---- 7. 全文索引一并重建（ADR-005）：它同样是"可从原料重建"的派生物 ----
    # 只在**真的提交了**之后做（同上：它自己会 commit）。dry-run / 回滚时跳过：
    # FTS 与库内 code_index 仍然一一对应，没有半状态。
    rep["fts_indexed"] = reindex_fts(conn, root) if rep["applied"] else 0
    rep["fts_skipped"] = not rep["applied"]
    return rep


def render_report(rep) -> None:
    """打印重建报告（CLI 的唯一出处：`fish_cli._cmd_rebuild` 调它）。

    为什么把打印从 `fish.py:cmd_rebuild` 搬到这里：报告字段与"提交/回滚"是同一件事
    的两面（`applied` / `aborted` / `backup_path` 只有 `_settle` 知道）。分居两个文件
    必然漂——本项目已经在"两份实现"上吃过多次亏（D-10 的旧重建）。
    """
    if rep.get("dry_run"):
        head = "重建演练"
        mode = "dry-run：**未写入任何字节**（加 --apply 才落库）"
    elif rep.get("aborted"):
        head = "重建中止"
        mode = "对账不一致 → 已回滚（库保持重建前状态）"
    else:
        head = "重建完成"
        mode = "已提交"
    print(f"{head}：{rep.get('rows_before', '?')} → {rep['total']} 条"
          f"（重放 {rep['projected']} 条记录事件、{rep['superseded_events']} 条替代事件、"
          f"{rep['retired_events']} 条作废事件）｜{mode}")
    if rep.get("backup_path"):
        print(f"  写前快照：{rep['backup_path']}（{rep.get('backup_rows')} 行；回退 = "
              f"替换 data/index.sqlite）")
    elif not rep.get("dry_run"):
        print("  ⚠ 写前快照：未生成（内存库 / 拿不到库文件路径）——回退只能靠外部备份")
    print(f"  occurred_at 来源：payload {rep['from_payload']} / 投影兜底 {rep['from_projection']}"
          f" / 文件 mtime {rep['from_file']} / 事件时间 {rep['from_event']}")
    print(f"  访问记录：快照保留 {rep['access_preserved']} 条 ｜ 事件补齐 "
          f"{rep['access_from_events']} 次 / 休眠 {rep['dormant_from_events']} 条")
    if rep["occurred_corrected"]:
        print(f"  occurred_at 纠正 {rep['occurred_corrected']} 条"
              f"（投影值晚于记录时间，逻辑上不可能，改用事件时间）：")
        for m in rep["occurred_corrected_list"][:10]:
            print(f"    - {m}")
    if rep["atts_from_projection"]:
        print(f"  附件元数据从投影兜底 {rep['atts_from_projection']} 条（旧事件只存 name+sha256）")
    if rep["atts_degraded"]:
        print(f"  ⚠ 附件元数据降级 {rep['atts_degraded']} 条（无投影可兜底）")
    if rep["recorded_at_normalized"]:
        print(f"  recorded_at 归一到事件时间：{rep['recorded_at_normalized']} 条（预期内）")
    if rep["recovered"]:
        print(f"  ✓ 从事件恢复投影丢失字段 {len(rep['recovered'])} 处：")
        for m in rep["recovered"][:10]:
            print(f"    - {m}")
    if rep["owner_normalized"]:
        print(f"  owner 归一为系统主人 {rep['owner_normalized']} 条"
              f"（事件里的 actor 不是有效公民，见 07 D-13）：")
        for m in rep["owner_normalized_list"][:10]:
            print(f"    - {m}")
    if rep.get("owner_fixed_applied"):
        print(f"  ✓ 归属修正重放 {rep['owner_fixed_applied']} 条（memory.owner_fixed，EVAL-261025-21）")
    if rep.get("owner_fixed_skipped"):
        print(f"  ⚠ 归属修正跳过 {len(rep['owner_fixed_skipped'])} 条（目标公民已归档/不存在）：")
        for m in rep["owner_fixed_skipped"][:10]:
            print(f"    - {m}")
    if rep["owner_missing"]:
        print(f"  ⚠ owner 缺失 {rep['owner_missing']} 条")
    if rep.get("fts_skipped"):
        print("  全文索引：本次未重建（未提交；--apply 且对账一致时随投影一起重建）")
    else:
        print(f"  全文索引已重建 {rep['fts_indexed']} 条（memory_fts，ADR-005）")
    if rep["mismatch"]:
        print(f"  ✗ 对账不一致 {len(rep['mismatch'])} 处（本次**未提交**，库保持重建前状态）：")
        for m in rep["mismatch"][:20]:
            print(f"    - {m}")
        print("  处置：先看上面哪几条「重放结果 ≠ 眼前投影」。库没坏——不修事件或投影就"
              "别加 --apply；确需覆盖时先手工备份 data/index.sqlite 再评估。")
    else:
        print("  ✓ 对账一致（owner / occurred_at / status / superseded_by / session_actor）")


def cmd_rebuild(args, *, fish) -> int:
    """`fish rebuild [--apply]` 的命令实现（P1-1）。

    `fish` 命名空间由 `fish_cli` 注入：本模块**不 import fish**（J2 分层，
    `test_split_contract.test_j2_no_back_import_static` 钉着），而 connect / SCHEMA /
    migrate 都是 fish.py 的装配层职责——注入比反向依赖干净。

    退出码：`0` = 一致（dry-run 或已提交）；`2` = 对账不一致（**已回滚**）。
    """
    root = Path(args.root)
    apply = bool(getattr(args, "apply", False))
    conn = fish.connect(root)
    try:
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        rep = rebuild_projection(conn, root, apply=apply)
        render_report(rep)
        if apply and rep.get("applied"):
            # 画像槽位与 code_index 是两张投影：各自重放、各自报账（混进同一个报告会
            # 让两边的 mismatch 互相污染）；失败不拖垮整次 rebuild（既有口径）。
            try:
                prep = fish.fish_profile.rebuild_slots(conn)
                print(f"画像槽位重建：事件 {prep['from_events']} 个 / 一致 {prep['projected']}"
                      f" / 校正 {prep['fixed']}"
                      + (f" / ⚠ 对不上 {len(prep['mismatch'])}" if prep['mismatch'] else ""))
            except Exception as exc:                      # noqa: BLE001 —— 见上
                print(f"  ⚠ 画像槽位重建失败（不影响投影）：{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    return 2 if rep.get("aborted") else 0
