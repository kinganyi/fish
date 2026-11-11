#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主人画像（PRD `09` §4.2 · R1-1 / R1-2 / R1-4 / R1-5 / R1-6）。

## 它为什么必须存在（以及为什么不能是"再来一类记忆"）

用户原话（`09` §4.2 引文）：

> 「永不衰减的这类信息，**本质上有变质和越来越大的风险**。」

系统对记忆的做法是**追加**：每条一个新编码，靠衰减（episode 21 天 / fact 60 天…）
兜住增长。但画像**不能衰减**——衰减就违背"永不"。于是它没有自然清理机制，
**唯一的出路是换结构**：从"一条条记忆"变成"一格一格的槽位"，一格只能有一个值。

这不是偏好，是算术：追加式 + 不衰减 = 无限的线性增长 = 迟早淹掉注入预算。

## 三层结构（与 fish 其余部分同构，不是独立模块）

    data_root/profile/画像.md   人的编辑面（**唯一人类可写介质**）
            ↓  fish profile sync（解析 → 差异 → 事件）
    events: profile.edited      真相源（ADR-001：事件是唯一真相）
            ↓  投影
    profile_slots               一格一值（可由事件完整重放）
            ↓  fish profile render
    data_root/profile/画像.md   再次渲染成人可读/可改的形态

外加一条**不动 slots 的旁路**（AI 只能走这条）：

    events: profile.proposed  →  profile_proposals（待采纳）
            ↓  人点 accept
    events: profile.edited    →  slots 改变

## 两个关键取舍（都有代价，写在这里免得后人重犯）

**取舍一：只让 `profile.edited` 改槽位。**
`profile.proposed` / `profile.rejected` / `profile.deferred` 都是**元数据**，不碰 slots。
于是重建器只需要重放**一类**事件就能还原全部槽位——D-10 的教训是"以为重放朴素、
其实漏了分支"，分支越少越不会漏。采纳一条建议时，代码**同时**写 `profile.edited`
（带 `cause=accepted:<id>`）和 `profile.accepted`（仅审计）。

**取舍二：不做双向文件同步。**
文件是**输入介质**（人表达意图），不是真相。同步方向永远是 文件 → 事件 → 表 → 文件。
反向（表 → 文件）只在 `render` 时发生，且**受 hash 保护**：若文件被人在上次渲染后
改过，`render` 会拒绝覆盖并提示先 `sync`。这跟 `fish-home` 那条"门面全是投影、
手改会被覆盖"是**两套规则**，因为这两个目录职责不同：
`fish-home` 是只读投影，`data_root/profile/` 是人的写入口。

## 写权分离（R1-2）怎么落地

用户要求「人类可写；**AI 只能建议，不得直接写画像**；违反时拒绝并回"请提建议"」。

落地在**接口层**，因为那才是真实的权限边界：

| 通道 | 谁在用 | 能做什么 |
|---|---|---|
| MCP（`mcp_server.py` 的 `tool_*`） | AI | 只能 `profile_propose`（写建议队列） |
| CLI（`fish profile set/accept/...`） | 人 | 可写槽位 |

**这是"防误操作"，不是"防恶意"**：AI 确实可以 shell 出去跑 CLI（hooks 的
`tool_activity` 会留下痕迹）。诚实的说法是——写权分离在**接口默认值**和
**审计可追溯**两个层面成立，它拦不住一个执意绕路的 AI，但能让"AI 悄悄改了画画像"
这件事**不可能不留痕**。

## 依赖方向（J2）

    fish_common ← fish_schema ← fish_db ← fish_profile ← fish.py

本模块**不许** import fish。需要的事件写入原语 `append_event`、配置读写
`get_config`/`set_config` 都已在 `fish_db`/`fish_common` 里。
"""
import hashlib
import json
import re
from pathlib import Path

from fish_common import get_config, now_utc, ulid, warn_ignored
from fish_db import append_event, set_config

# ============================================================== 槽位目录（R1-1）

# 固定槽位表。**这就是"每类有槽位上限"的实现**：`slots` 元组写死了每类能有几格，
# 写入一个不在表里的槽名会被拒绝（而不是静默新增一行）——
# 因为"可扩展"一旦没有边界，就退化成追加式记忆，正是本模块要避免的东西。
#
# permanence 决定注入分级（R1-6）：
#   stable   永不变（称呼）——总是注入
#   lasting  长期有效（生活/工作/性格）——注入前若干条
#   volatile 会过期（近期动态）——**带时效标记**才注入，过期不注入
# 每个槽位带一个**注入序号**（第 4 位），越小越先注入。
#
# 为什么需要它（2026-11-11 实测暴露的缺陷）：R1-6 说"L0 只放称呼 + 3 条**最稳定**字段"，
# 而第一版把它实现成"按目录顺序取前 3 条 lasting"。目录顺序是 生活→工作→性格，
# 于是只要生活组填了几格，**性格组（价值观/沟通偏好/决策风格）永远进不了默认注入**——
# 而那三条恰恰最影响 AI 怎么做事。实测：9 格填满后，注入包里只有
# 所在地 / 身份 / 业务线，沟通偏好与决策风格一条都没出现。
#
# "最稳定"是个语义判断，不是字母序。所以改成**显式排序**：人可以通过改这张表控制，
# 判据钉住"性格必须排在生活之前"这条语义。
PROFILE_GROUPS = (
    # (组名, 稳定性, ((槽名, 注入序号), ...))
    ("称呼", "stable", (("名字", 0), ("怎么叫我", 1))),
    # 性格排在最前：它决定 AI **怎么跟这个人相处**，比"他在哪"重要。
    ("性格", "lasting", (("沟通偏好", 10), ("决策风格", 11), ("价值观", 12))),
    ("工作", "lasting", (("身份", 20), ("当前重点", 21), ("业务线", 22), ("公司", 23))),
    ("生活", "lasting", (("所在地", 30), ("家庭", 31), ("作息", 32))),
    # `近期动态` 的槽位名刻意避开了注入包 L1 里那些词（"当前项目"等）：
    # L1 的「当前项目」是**这次会话**的坐标，画像的「在忙什么」是**这个人**的现状，
    # 两者同名会让人以为是同一件事。撞名的代价是误读，不值得省几个字。
    # 这一组是 volatile，序号只用于 full 模式排序。
    ("近期动态", "volatile", (("在忙什么", 40), ("最近的变化", 41), ("待解决", 42))),
)

# 注入分级（R1-6）：L0 只放"称呼 + 3 条最稳定字段"。
PROFILE_STABLE_EXTRA = 3
# 提问三问（R1-4，用户原话逐字）
PROFILE_QUESTIONS = (
    "① 你的印象有变化吗？",
    "② 有必须被调整的信息吗？",
    "③ 你可以选择本轮不回答（`fish profile defer`）。",
)
PROFILE_EVENT_EDITED = "profile.edited"
PROFILE_EVENT_PROPOSED = "profile.proposed"
PROFILE_EVENT_ACCEPTED = "profile.accepted"
PROFILE_EVENT_REJECTED = "profile.rejected"
PROFILE_EVENT_DEFERRED = "profile.deferred"

# 人的编辑面相对 data_root 的位置（`projects/` 存记忆正文，画像单独一层）
PROFILE_REL_DIR = "profile"
PROFILE_FILE = "画像.md"


def profile_catalog():
    """`{组名: {"permanence":…, "slots": (...)}}`，给 CLI 的报错/帮助用。"""
    return {g: {"permanence": p, "slots": tuple(x[0] for x in s)}
            for g, p, s in PROFILE_GROUPS}


def profile_all_slot_keys():
    """全部合法 slot_key（`组.槽名`），有序。"""
    return [f"{g}.{name}" for g, _p, slots in PROFILE_GROUPS
            for name, _rank in slots]


def resolve_slot(group, name):
    """校验 `(组, 槽名)` 合法，返回 `(slot_key, permanence)`；非法则**响亮报错**并给出全部可选项。

    为什么报错而不是自动建槽：见 `PROFILE_GROUPS` 上方注释——
    "可扩展"没有边界就退化成追加式记忆。宁可让人显式改这张表。
    """
    g = (group or "").strip()
    n = (name or "").strip()
    for gname, perm, slots in PROFILE_GROUPS:
        if gname != g:
            continue
        names = [x[0] for x in slots]
        if n not in names:
            raise ValueError(
                f"「{gname}」没有槽位「{n}」。可用：{'/'.join(names)}"
                f"（要加新槽位请改 `fish_profile.PROFILE_GROUPS`——这是**故意的**固定表）")
        return f"{gname}.{n}", perm
    raise ValueError(
        f"没有画像分组「{g}」。可用分组：{'/'.join(x[0] for x in PROFILE_GROUPS)}")


def _split_key(slot_key):
    """`"称呼.名字"` → `("称呼", "名字")`；格式不对返回 `(None, None)`。"""
    if not slot_key or "." not in slot_key:
        return None, None
    g, _, n = slot_key.partition(".")
    return g, n


# ============================================================== 读

def get_slot(conn, slot_key):
    """取一格；没有返回 None。"""
    row = conn.execute(
        "SELECT slot_key, slot_group, slot_name, value, permanence, as_of,"
        " version, actor_citizen_id, updated_at FROM profile_slots WHERE slot_key=?",
        (slot_key,)).fetchone()
    if not row:
        return None
    keys = ("slot_key", "slot_group", "slot_name", "value", "permanence",
            "as_of", "version", "actor_citizen_id", "updated_at")
    return dict(zip(keys, row))


def list_slots(conn, group=None):
    """按目录顺序列出已填的槽位（**顺序稳定**：注入与渲染都依赖它）。"""
    rows = conn.execute(
        "SELECT slot_key, slot_group, slot_name, value, permanence, as_of,"
        " version, actor_citizen_id, updated_at FROM profile_slots").fetchall()
    keys = ("slot_key", "slot_group", "slot_name", "value", "permanence",
            "as_of", "version", "actor_citizen_id", "updated_at")
    got = {r[0]: dict(zip(keys, r)) for r in rows}
    out = []
    for g, _perm, slots in PROFILE_GROUPS:
        if group and g != group:
            continue
        for name, _rank in slots:
            k = f"{g}.{name}"
            if k in got:
                out.append(got[k])
    return out


def filled_count(conn):
    """已填槽位数（CLI `profile show` 与体检用）。"""
    return conn.execute("SELECT COUNT(*) FROM profile_slots").fetchone()[0]


# ============================================================== 写（**人通道**）

def _write_event(conn, slot_key, action, old, new, actor, reason, commit):
    """唯一的槽位变更事件。**只此一处**产生 `profile.edited`。"""
    return append_event(
        conn, PROFILE_EVENT_EDITED,
        {"citizen_id": actor or "human", "evidence_level": "signed"},
        {"kind": "profile_slot", "id": slot_key},
        {"slot_key": slot_key, "action": action, "old": old, "new": new,
         "reason": reason or ""},
        cause=reason or None, commit=commit)


def set_slot(conn, group, name, value, *, actor=None, reason="", commit=True,
             bump_counter=False):
    """**人**写一格（覆盖语义，R1-1）。

    `version` 每次写入 +1——它是"这一格改过几次"，不是记忆条数。
    覆盖而不是追加：同 key 只有一行，所以槽位总数恒定（这是防"变质变大"的机制本体）。

    `bump_counter`：写**画像**本身不算"写了文档"，默认 False；
    只有让 `record_memory` 去推进提问计数（见 `bump_question_counter`）。
    """
    slot_key, perm = resolve_slot(group, name)
    old = get_slot(conn, slot_key)
    old_value = old["value"] if old else None
    as_of = now_utc() if perm == "volatile" else (old["as_of"] if old else None)
    version = (old["version"] + 1) if old else 1
    conn.execute(
        "INSERT INTO profile_slots (slot_key, slot_group, slot_name, value,"
        " permanence, as_of, version, actor_citizen_id, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(slot_key) DO UPDATE SET value=excluded.value,"
        " as_of=excluded.as_of, version=excluded.version,"
        " actor_citizen_id=excluded.actor_citizen_id, updated_at=excluded.updated_at",
        (slot_key, group, name, str(value), perm, as_of, version, actor, now_utc()))
    _write_event(conn, slot_key, "set", old_value, str(value), actor, reason, commit)
    if bump_counter:
        bump_question_counter(conn)
    return {"slot_key": slot_key, "old": old_value, "new": str(value),
            "version": version, "permanence": perm}


def remove_slot(conn, slot_key, *, actor=None, reason="", commit=True):
    """**人**清空一格（不是删历史——历史在事件里）。"""
    old = get_slot(conn, slot_key)
    if not old:
        return None
    conn.execute("DELETE FROM profile_slots WHERE slot_key=?", (slot_key,))
    _write_event(conn, slot_key, "remove", old["value"], None, actor, reason, commit)
    return {"slot_key": slot_key, "old": old["value"], "new": None}


# ============================================================== 建议队列（**AI 通道**）

def propose(conn, group, name, value, *, by, rationale="", commit=True):
    """AI 提一条建议。**不碰 `profile_slots`**——这是 R1-2 的核心。

    返回 `proposal_id`。同槽已有 pending 建议时，旧建议置 `superseded`
    （否则队列会堆满互相矛盾的旧想法，人根本没法采纳）。
    """
    slot_key, _perm = resolve_slot(group, name)
    conn.execute(
        "UPDATE profile_proposals SET status='superseded'"
        " WHERE slot_key=? AND status='pending'", (slot_key,))
    pid = ulid()
    now = now_utc()
    conn.execute(
        "INSERT INTO profile_proposals (proposal_id, slot_key, action, proposed_value,"
        " rationale, proposed_by, proposed_at, status) VALUES (?,?,?,?,?,?,?,'pending')",
        (pid, slot_key, "set", str(value), rationale or "", by or "unknown", now))
    append_event(conn, PROFILE_EVENT_PROPOSED,
                 {"citizen_id": by or "unknown", "evidence_level": "self-reported"},
                 {"kind": "profile_proposal", "id": pid},
                 {"proposal_id": pid, "slot_key": slot_key, "action": "set",
                  "value": str(value), "rationale": rationale or ""},
                 commit=commit)
    return pid


def propose_removal(conn, group, name, *, by, rationale="", commit=True):
    """AI 建议**清空**一格。"""
    slot_key, _perm = resolve_slot(group, name)
    conn.execute(
        "UPDATE profile_proposals SET status='superseded'"
        " WHERE slot_key=? AND status='pending'", (slot_key,))
    pid = ulid()
    conn.execute(
        "INSERT INTO profile_proposals (proposal_id, slot_key, action, proposed_value,"
        " rationale, proposed_by, proposed_at, status) VALUES (?,?,?,?,?,?,?,'pending')",
        (pid, slot_key, "remove", None, rationale or "", by or "unknown", now_utc()))
    append_event(conn, PROFILE_EVENT_PROPOSED,
                 {"citizen_id": by or "unknown", "evidence_level": "self-reported"},
                 {"kind": "profile_proposal", "id": pid},
                 {"proposal_id": pid, "slot_key": slot_key, "action": "remove",
                  "value": None, "rationale": rationale or ""},
                 commit=commit)
    return pid


def list_proposals(conn, status="pending"):
    rows = conn.execute(
        "SELECT proposal_id, slot_key, action, proposed_value, rationale,"
        " proposed_by, proposed_at, status FROM profile_proposals"
        " WHERE status=? ORDER BY proposed_at", (status,)).fetchall()
    keys = ("proposal_id", "slot_key", "action", "proposed_value", "rationale",
            "proposed_by", "proposed_at", "status")
    return [dict(zip(keys, r)) for r in rows]


def _decide(conn, proposal_id, status, by, note, commit):
    row = conn.execute(
        "SELECT proposal_id, slot_key, action, proposed_value, status"
        " FROM profile_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError(f"没有建议 {proposal_id}")
    if row[4] != "pending":
        raise ValueError(f"建议 {proposal_id} 已是 {row[4]}，不能再改（只处理 pending）")
    conn.execute(
        "UPDATE profile_proposals SET status=?, decided_by=?, decided_at=?,"
        " decision_note=? WHERE proposal_id=?",
        (status, by or "human", now_utc(), note or "", proposal_id))
    return row


def accept_proposal(conn, proposal_id, *, by=None, note="", commit=True):
    """人采纳一条建议：**这里才真正改槽位**。

    同时写两个事件：
      · `profile.edited`（**改槽位的唯一事件**，带 cause 指向建议）
      · `profile.accepted`（纯审计：谁在什么时候采纳了谁的哪条建议）
    分开的理由：重建只需要重放前者；后者若也改槽位，重放会「同一改动施加两次」，
    版本号就飘了。
    """
    pid, slot_key, action, value, _st = _decide(conn, proposal_id, "accepted", by, note, commit)
    group, name = _split_key(slot_key)
    if action == "remove":
        res = remove_slot(conn, slot_key, actor=by, reason=f"accepted:{pid}", commit=commit)
    else:
        res = set_slot(conn, group, name, value, actor=by,
                       reason=f"accepted:{pid}", commit=commit)
    append_event(conn, PROFILE_EVENT_ACCEPTED,
                 {"citizen_id": by or "human", "evidence_level": "signed"},
                 {"kind": "profile_proposal", "id": pid},
                 {"proposal_id": pid, "slot_key": slot_key, "new": value},
                 commit=commit)
    return res


def reject_proposal(conn, proposal_id, *, by=None, note="", commit=True):
    pid, slot_key, _a, _v, _st = _decide(conn, proposal_id, "rejected", by, note, commit)
    append_event(conn, PROFILE_EVENT_REJECTED,
                 {"citizen_id": by or "human", "evidence_level": "signed"},
                 {"kind": "profile_proposal", "id": pid},
                 {"proposal_id": pid, "slot_key": slot_key, "note": note or ""},
                 commit=commit)
    return {"proposal_id": pid, "slot_key": slot_key, "status": "rejected"}


# ============================================================== 提问节律（R1-4）

def bump_question_counter(conn, n=1):
    """每次成功写一篇**记忆文档**就 +1（由 `record_memory` 调用）。

    为什么用 config 而不建表：它是一条**游标**（"距上次提问又写了几篇"），
    重建时不该被事件覆盖——和 `code_registry.last_seq` 同类。
    """
    cur = int(get_config(conn, "profile.docs_since_ask") or 0)
    return set_config(conn, "profile.docs_since_ask", cur + int(n))


def question_due(conn):
    """到该问画像的时候了吗（累计文档数 ≥ 阈值）。"""
    every = int(get_config(conn, "profile.ask_every") or 10)
    since = int(get_config(conn, "profile.docs_since_ask") or 0)
    return every > 0 and since >= every


def reset_question_counter(conn, *, status="answered", by=None, commit=True):
    """回答或"本轮不回答"之后清零。

    **"本轮不回答"也要留痕**（用户原话"你可以选择本轮不回答"）：
    记一条 `profile.deferred` 事件，把当时的累计数写进去——
    否则"为什么这次没问"在事后完全不可查。
    """
    since = int(get_config(conn, "profile.docs_since_ask") or 0)
    qid = ulid()
    conn.execute(
        "INSERT INTO profile_questions (question_id, raised_at, trigger_count,"
        " status, answered_at) VALUES (?,?,?,?,?)",
        (qid, now_utc(), since, status,
         now_utc() if status == "answered" else None))
    if status == "deferred":
        append_event(conn, PROFILE_EVENT_DEFERRED,
                     {"citizen_id": by or "human", "evidence_level": "signed"},
                     {"kind": "profile_question", "id": qid},
                     {"question_id": qid, "trigger_count": since},
                     commit=commit)
    set_config(conn, "profile.docs_since_ask", 0)
    return {"question_id": qid, "status": status, "trigger_count": since}


def render_questions_block(conn):
    """注入包里的「画像三问」段。**只在到点且未延平时出现**。"""
    if not question_due(conn):
        return ""
    since = int(get_config(conn, "profile.docs_since_ask") or 0)
    lines = [f"【画像提问】距上次已写 {since} 篇，按节律该问三问（R1-4）："]
    lines += ["  " + q for q in PROFILE_QUESTIONS]
    lines.append("  回答：`fish profile ask --answer \"...\"`；"
                 "不回答：`fish profile defer`（会留痕，且同样清零）")
    return "\n".join(lines)


# ============================================================== 注入分级（R1-6）

def _is_fresh(as_of, max_days):
    """volatile 槽位是否还在时效窗内。`as_of` 为空视为过期（宁可不说）。"""
    if not as_of:
        return False
    try:
        from datetime import datetime, timezone
        s = as_of.replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
        return age <= max_days
    except Exception as exc:            # noqa: BLE001 —— 时间解析失败不该拖垮注入
        warn_ignored("fish_profile._is_fresh", exc)
        return False


def render_profile_block(conn, *, full=False):
    """注入包里的「主人画像」段。

    分级（R1-6）：
      · `称呼`（stable）——**总是**注入，那是"怎么称呼这个人"
      · 另有 `PROFILE_STABLE_EXTRA` 条最稳定的 lasting 字段
      · `近期动态`（volatile）——只有**在时效窗内**才注入，且**必须带 as-of**
        （过期不注入：三个月前的"最近在忙"当"当下"递出去只会误导）
      · `full=True` 时放开条数上限（仍不给过期的 volatile）

    返回空串表示"没有画像可注入"——调用方据此**连标题都不输出**，
    免得注入包里出现一个空壳段（那是噪音，也浪费预算）。
    """
    slots = list_slots(conn)
    if not slots:
        return ""
    max_days = int(get_config(conn, "profile.volatile_max_days") or 14)
    by_group = {}
    for s in slots:
        by_group.setdefault(s["slot_group"], []).append(s)

    lines = []
    # 1) 称呼（stable）——全部
    for s in by_group.get("称呼", []):
        lines.append(f"  {s['slot_name']}：{s['value']}")
    # 2) lasting 组：**按槽位的注入序号**取前 N 条（不是按目录顺序）。
    #
    # 2026-11-11 修：第一版按目录顺序取，于是"性格"（在目录最后）永远进不来——
    # 而沟通偏好/决策风格恰恰最影响 AI 怎么做事。现在序号写死在 `PROFILE_GROUPS` 里，
    # 排序依据是**语义**（越影响相处的越前），不是字母序。
    # 分组轮转分配（round-robin），而不是纯按序号取前 N。
    #
    # 为什么（两次实测的教训）：
    #   · 按**目录顺序**取 → 性格组（排在最后）永远进不来，而它最影响 AI 怎么做事；
    #   · 改成**纯按序号**取 → 性格三条全占满，把「工作·身份」挤掉，AI 连这个人是
    #     干什么的都不知道了。
    # 两个极端都不对。真正要的是**每组都有代表**：先取各组排最前的那条，
    # 再取各组的第二条——这样 3 条恰好覆盖 3 个 lasting 组，各司其职。
    buckets = []
    for gname, perm, slots in PROFILE_GROUPS:
        if perm != "lasting":
            continue
        rank_of = {name: r for name, r in slots}
        items = sorted(by_group.get(gname, []),
                       key=lambda x: rank_of.get(x["slot_name"], 999))
        if items:
            buckets.append((min(rank_of.values()), gname, items))
    buckets.sort(key=lambda b: b[0])          # 组之间按该组最小序号排
    picked = []
    round_no = 0
    while True:
        added = False
        for _r, gname, items in buckets:
            if round_no < len(items):
                picked.append((gname, items[round_no]))
                added = True
        if not added:
            break
        round_no += 1
    limit = None if full else PROFILE_STABLE_EXTRA
    for gname, s in (picked if limit is None else picked[:limit]):
        lines.append(f"  {gname}·{s['slot_name']}：{s['value']}")
    # 3) 近期动态：过期不注入，注入必带 as-of
    for s in by_group.get("近期动态", []):
        if not _is_fresh(s.get("as_of"), max_days):
            continue
        stamp = (s.get("as_of") or "")[:10]
        lines.append(f"  {s['slot_name']}（as-of {stamp}）：{s['value']}")
    if not lines:
        return ""
    return "主人画像：\n" + "\n".join(lines)


# ============================================================== 文件即界面（R1-3 落地）

def profile_path(root):
    """人的编辑面：`<data_root>/profile/画像.md`。"""
    return Path(root) / PROFILE_REL_DIR / PROFILE_FILE


def render_markdown(conn):
    """渲染成人可编辑的 Markdown（`## 组` + `- 槽名: 值`，一行一槽）。"""
    by_group = {}
    for s in list_slots(conn):
        by_group.setdefault(s["slot_group"], {})[s["slot_name"]] = s
    out = [
        "# 主人画像",
        "",
        "> 这个文件是**人**的编辑面（AI 不得直接改；AI 只能 `fish profile propose`）。",
        "> 改完运行 `fish profile sync` 生效；`fish profile render` 会重写本文件。",
        "> 格式：`## 分组` 下每行 `- 槽名: 值`；留空的槽不会出现。",
        "",
    ]
    for gname, perm, slots in PROFILE_GROUPS:
        out.append(f"## {gname}")
        if perm == "volatile":
            out.append("<!-- 这一组带时效：sync 时会把 as-of 记成当前时间 -->")
        for sname, _rank in slots:
            s = by_group.get(gname, {}).get(sname)
            val = s["value"] if s else ""
            out.append(f"- {sname}: {val}")
        out.append("")
    return "\n".join(out) + "\n"


_LINE_RE = re.compile(r"^\s*-\s*([^:：]+)[:：]\s*(.*)$")


def parse_markdown(text):
    """把人的编辑解析成 `{slot_key: value}`（空值 = 清空该槽）。

    只认 `## 组` 与 `- 槽名: 值`；其余行忽略——这样人在文件里写注释、加标题、
    贴一段话都不会把解析弄崩（**人的编辑面必须宽容**，否则他改一次就被判错一次）。
    """
    got = {}
    group = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            group = line[3:].strip()
            continue
        if not group or not line.lstrip().startswith("-"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        val = m.group(2).strip()
        try:
            slot_key, _perm = resolve_slot(group, name)
        except ValueError:
            continue          # 不认识的组/槽：忽略（宽容），但 sync 的 report 会记 skipped
        got[slot_key] = val
    return got


def sync_from_text(conn, text, *, actor=None, commit=True):
    """人的编辑 → 事件 → 槽位。返回 `{changed:[...], cleared:[...], skipped:int}`。

    这是**单向**的：文件 → 事件。`render` 才是反向。
    """
    parsed = parse_markdown(text)
    changed, cleared, unchanged = [], [], 0
    for slot_key, value in parsed.items():
        g, n = _split_key(slot_key)
        old = get_slot(conn, slot_key)
        oldv = old["value"] if old else ""
        if value == "":
            if old:
                remove_slot(conn, slot_key, actor=actor,
                            reason="profile.sync（文件里清空）", commit=commit)
                cleared.append(slot_key)
            continue
        if oldv == value:
            unchanged += 1
            continue
        set_slot(conn, g, n, value, actor=actor, reason="profile.sync", commit=commit)
        changed.append(slot_key)
    return {"changed": changed, "cleared": cleared, "unchanged": unchanged,
            "parsed": len(parsed)}


def file_digest(path):
    """文件内容指纹（用于"人被改过没有"的判断）。不存在返回 None。"""
    try:
        b = Path(path).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(b).hexdigest()


# ============================================================== 重建（D-10 纪律）

def apply_event_to_slots(state, payload):
    """把一条 `profile.edited` 施加到重放状态 `{slot_key: value}`。

    **只有这一个事件改槽位**（见模块头"取舍一"）。`remove` 从 state 里删键。
    返回 True 表示这条事件被消费了（调用方据此计数）。
    """
    slot_key = (payload or {}).get("slot_key")
    if not slot_key:
        return False
    action = payload.get("action") or "set"
    if action == "remove":
        state.pop(slot_key, None)
    else:
        new = payload.get("new")
        if new is None:
            state.pop(slot_key, None)
        else:
            state[slot_key] = new
    return True


def replay_slots_from_events(conn):
    """从事件重放出 `{slot_key: value}`（**不改表**，纯函数式，方便对账）。"""
    state = {}
    for (raw,) in conn.execute(
            "SELECT payload FROM events WHERE type=? ORDER BY seq", (PROFILE_EVENT_EDITED,)):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            warn_ignored("fish_profile.replay_slots_from_events", exc)
            continue
        apply_event_to_slots(state, payload)
    return state


def rebuild_slots(conn, *, commit=True):
    """用事件重放**校正** `profile_slots`，返回对账报告。

    与 `code_index` 的重建同一条纪律：投影可以坏，事件不会坏；
    但**不静默修**——每一条差异都记进报告（`mismatch`），让 `fish rebuild` 能报出来。

    为什么不直接 `DELETE + 重插`：`profile_slots` 里还有 `version`/`updated_at` 等
    由写入路径维护的列，重放只还原 `value`。所以策略是"**按事件校正 value**"，
    而不是"把表整个重建"——后者会把版本号打成 1，等于丢信息。
    """
    state = replay_slots_from_events(conn)
    rep = {"from_events": len(state), "projected": 0, "fixed": 0,
           "orphan_dropped": 0, "mismatch": []}
    current = {s["slot_key"]: s["value"] for s in list_slots(conn)}
    for slot_key, value in state.items():
        cur = current.get(slot_key)
        if cur == value:
            rep["projected"] += 1
            continue
        g, n = _split_key(slot_key)
        if not g or n is None:
            rep["mismatch"].append(f"{slot_key}: 事件 key 不合法")
            continue
        try:
            resolve_slot(g, n)
        except ValueError as exc:
            rep["mismatch"].append(f"{slot_key}: {exc}")
            continue
        _apply_value_only(conn, slot_key, value)
        rep["fixed"] += 1
    # 表里有、事件里没有 → 幽灵行（正常不该出现；出现就报，不自动删）
    for slot_key in current:
        if slot_key not in state:
            rep["orphan_dropped"] += 1
            rep["mismatch"].append(f"{slot_key}: 投影有、事件无（幽灵槽位，未自动删）")
    if commit:
        conn.commit()
    return rep


def _apply_value_only(conn, slot_key, value):
    """重建专用：只改 value（保留 version/updated_at 等写入侧维护的列）。"""
    conn.execute("UPDATE profile_slots SET value=? WHERE slot_key=?", (str(value), slot_key))


# ============================================================== CLI 分发（presentation 与 domain 同处）

def run_cli(args, conn, *, actor_fn=None, config_fn=None):
    """执行 `fish profile <action>`。返回退出码。

    **为什么 CLI 代码在这里而不是 `fish.py`**：它是画像的**表现层**，改画像的人
    只需要打开一个文件就能同时看到"数据怎么存"和"命令怎么用"。`fish.py` 只留一个
    薄适配器负责建连接/迁移（那两件事依赖核心，搬不下来）。

    写权分离（R1-2）在这里也看得清：`set` / `remove` / `accept` / `reject` 是**人通道**；
    `propose` 存在于 CLI 只是为了让"AI 也能用命令行提建议"这条路径对称——
    **AI 的默认通道是 MCP，而 MCP 没有 set**。
    """
    import json as _json
    import sys as _sys
    act = getattr(args, "profile_action", None)
    who = (getattr(args, "as_actor", None)
           or (actor_fn(conn) if actor_fn else None) or "human")

    def err(msg):
        _sys.stderr.write(f"错误：{msg}\n")
        return 2

    if act == "show":
        slots = list_slots(conn, group=getattr(args, "group", None))
        if getattr(args, "json", False):
            print(_json.dumps(slots, ensure_ascii=False, indent=2))
            return 0
        if not slots:
            print("画像还是空的。填法：`fish profile set 称呼 名字 <你的名字>`")
            return 0
        cur = None
        for s in slots:
            if s["slot_group"] != cur:
                cur = s["slot_group"]
                print(f"## {cur}")
            stamp = (f"（as-of {(s['as_of'] or '')[:10]}）"
                     if s["permanence"] == "volatile" else "")
            print(f"  {s['slot_name']}{stamp}：{s['value']}  [v{s['version']}]")
        pend = list_proposals(conn)
        if pend:
            print(f"\n待采纳建议 {len(pend)} 条（`fish profile proposals` 查看）：")
            for p in pend[:5]:
                print(f"  {p['proposal_id'][:10]}… {p['slot_key']} → {p['proposed_value']!r}"
                      f"  by {p['proposed_by']}")
        return 0

    if act == "set":
        try:
            res = set_slot(conn, args.group, args.name, args.value, actor=who,
                           reason=getattr(args, "reason", "") or "")
        except ValueError as e:
            return err(e)
        print(f"已写入 {res['slot_key']}：{res['old']!r} → {res['new']!r}"
              f"（第 {res['version']} 版）")
        return 0

    if act == "remove":
        res = remove_slot(conn, args.slot_key, actor=who,
                          reason=getattr(args, "reason", "") or "")
        if not res:
            return err(f"没有槽位 {args.slot_key}")
        print(f"已清空 {res['slot_key']}（原值 {res['old']!r}，历史在事件里）")
        return 0

    if act == "propose":
        try:
            if getattr(args, "remove", False):
                pid = propose_removal(conn, args.group, args.name, by=who,
                                      rationale=args.rationale or "")
            else:
                if args.value is None:
                    return err("提建议要给值（或加 --remove 建议清空）")
                pid = propose(conn, args.group, args.name, args.value, by=who,
                              rationale=args.rationale or "")
        except ValueError as e:
            return err(e)
        print(f"已提交建议 {pid}（**画像没变**；等人 `fish profile accept {pid}` 才生效）")
        return 0

    if act == "proposals":
        rows = list_proposals(conn, status=getattr(args, "status", "pending"))
        if not rows:
            print(f"没有 {getattr(args, 'status', 'pending')} 状态的建议。")
            return 0
        for p in rows:
            print(f"{p['proposal_id']}  {p['slot_key']}  {p['action']} "
                  f"{p['proposed_value']!r}")
            print(f"    by {p['proposed_by']} @ {p['proposed_at']}")
            if p["rationale"]:
                print(f"    理由：{p['rationale']}")
        return 0

    if act == "accept":
        try:
            res = accept_proposal(conn, args.proposal_id, by=who)
        except ValueError as e:
            return err(e)
        print(f"已采纳：{res['slot_key']} → {res['new']!r}"
              + (f"（原 {res['old']!r}）" if res.get("old") is not None else ""))
        return 0

    if act == "reject":
        try:
            res = reject_proposal(conn, args.proposal_id, by=who,
                                  note=getattr(args, "note", "") or "")
        except ValueError as e:
            return err(e)
        print(f"已拒绝 {res['proposal_id']}（画像不变）")
        return 0

    if act == "render":
        return _cli_render(args, conn)
    if act == "sync":
        return _cli_sync(args, conn, who)
    if act == "ask":
        return _cli_ask(args, conn, who, config_fn)
    if act == "defer":
        res = reset_question_counter(conn, status="deferred", by=who)
        print(f"已标记本轮不回答（累计 {res['trigger_count']} 篇，已清零）")
        return 0
    return err(f"未知的 profile 动作：{act}")


def _cli_render(args, conn):
    """把槽位渲染成人类可编辑的 Markdown。**受 hash 保护**：文件被人在库外改过就拒绝覆盖。"""
    import sys as _sys
    path = profile_path(Path(args.root))
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(conn)
    if path.exists() and not getattr(args, "force", False):
        old = path.read_text(encoding="utf-8", errors="replace")
        if old != text and parse_markdown(old) != parse_markdown(text):
            _sys.stderr.write(
                f"错误：{path} 在库之外被改过（与当前槽位不一致）。\n"
                f"　先 `fish profile sync` 把文件里的改动导入，再 render；"
                f"或用 --force 丢弃它们。\n")
            return 2
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"已渲染 {path}（人可直接编辑，改完跑 `fish profile sync`）")
    return 0


def _cli_sync(args, conn, who):
    """人的编辑 → 事件 → 槽位（单向）。"""
    import sys as _sys
    path = profile_path(Path(args.root))
    if not path.exists():
        _sys.stderr.write(f"错误：{path} 不存在。先 `fish profile render` 生成模板。\n")
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    rep = sync_from_text(conn, text, actor=who)
    print(f"同步完成：改动 {len(rep['changed'])} 槽、清空 {len(rep['cleared'])} 槽、"
          f"未变 {rep['unchanged']} 槽")
    for k in rep["changed"]:
        print(f"  ~ {k}")
    for k in rep["cleared"]:
        print(f"  - {k}（已清空）")
    return 0


def _cli_ask(args, conn, who, config_fn):
    """R1-4：看三问 / 回答。

    **回答本身不改画像**——那是 R1-2 的边界（回答≠授权改画像）。
    这里只负责显示、清零、留痕。
    """
    answer = getattr(args, "answer", None)
    if answer is None:
        due = question_due(conn)
        since = int((config_fn(conn, "profile.docs_since_ask") if config_fn else 0) or 0)
        every = int((config_fn(conn, "profile.ask_every") if config_fn else 10) or 10)
        print(f"距上次提问已写 {since} 篇（阈值 {every}）"
              + ("，**已到点**" if due else "，还没到点"))
        for q in PROFILE_QUESTIONS:
            print("  " + q)
        print("回答：`fish profile ask --answer \"...\"`；不回答：`fish profile defer`")
        return 0
    reset_question_counter(conn, status="answered", by=who)
    print("已记录本轮回答并清零计数。")
    if answer.strip():
        print(f"　你的回答：{answer.strip()}")
        print("　要把它落成画像，请用 `fish profile set <组> <槽> <值>`"
              "（回答本身不改画像——那是 R1-2 的边界）")
    return 0
