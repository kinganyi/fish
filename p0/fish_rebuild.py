#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从事件重放重建 `code_index` 投影（2026-11-11 从 `fish.py` 抽出，错题本 E-11）。

## 为什么单独成模块

它是 `fish.py` 里**最长也最危险**的函数：406 行，且外部审查 🔴9 就出在它身上
（只重放 `memory.owner_fixed`、不重放 `citizen.merged` / `citizen.retired`，
于是一次 rebuild 会静默撤销人工合并）。留在一个 14.5k 行的文件里，改它的时候
没人愿意读完上下文——而它恰恰是"读不完全就会写错"的那种函数。

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
from pathlib import Path

from fish_common import (body_relpath, reindex_fts, resolve_owner,
                         split_code_prefix)
from fish_trust import load_ownership_merge_map, resolve_merged


def rebuild_projection(conn, root: Path) -> dict:
    """从事件重放重建 `code_index` 投影，返回对账报告。

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
        "occurred_corrected": 0, "occurred_corrected_list": [], "corrected_codes": set(),
        "access_from_events": 0, "dormant_from_events": 0,
        "atts_from_projection": 0, "atts_degraded": 0,
        "recorded_at_normalized": 0, "recovered": [], "mismatch": [], "snapshot_size": 0,
    }

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

    conn.execute("DELETE FROM code_index")
    conn.commit()

    # owner 归一所需的知识：哪些才是真公民（见 07 D-13）。
    # 库里没有公民知识时（空库 / 测试）不做归一——不凭猜测改写归属。
    valid_citizen_ids = {r[0] for r in conn.execute("SELECT citizen_id FROM citizens")}
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

        owner = actor.get("citizen_id") or old.get("owner") or "unknown"
        # 外部审查 🔴9（2026-11-11）：**先解析合并链，再判"是否有效公民"**。
        # 不解析 = rebuild 把人工合并静默撤销 —— owner 回到原始 actor 的公民，
        # 而那个公民此刻 status 已是 `merged_into:<dst>`，留下幽灵归属。
        if owner not in ("", "unknown", None):
            _resolved = resolve_merged(merge_map, owner)
            if _resolved != owner:
                rep["owner_merged"] += 1
                rep["owner_merged_list"].append(f"{code}: {owner!r} → {_resolved!r}")
                owner = _resolved
        # owner 必须是有效公民（07 D-13）。有公民知识时把无效值归一到系统主人并逐条记录，
        # 而不是把它写回投影（否则会留下指向幽灵公民的归属）。
        if valid_citizen_ids and owner not in valid_citizen_ids and owner != "unknown":
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
    conn.commit()

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
        if valid_citizen_ids and r_owner not in valid_citizen_ids and r_owner != "unknown":
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
    conn.commit()
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
    conn.commit()
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

    # ---- 6. 全文索引一并重建（ADR-005）：它同样是"可从原料重建"的派生物 ----
    rep["fts_indexed"] = reindex_fts(conn, root)
    return rep
