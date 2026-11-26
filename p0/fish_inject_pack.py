#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入装配 + 工具审计域（`fish.py` 拆包 · W3-c · 144 刀三）：注入包组装
（`build_injection_pack` 549 行）+ 规则/关注/教训块 + 项目解析与渲染 + 工具审计与知识盲区。
**activity-audit 是 inject 的专属数据源**（全仓只有 inject 读它），合并为一条纵切。

唯一外依赖是 `fish_session`（`current_open_stints` / `env_session_alias` /
`resolve_project_by_cwd` / `find_citizen_by_name` / `load_citizen`）；其余只 import 下层
模块（`fish_common` / `fish_schema` / `fish_inject` / `fish_visibility` / `fish_board` /
`fish_push` / `fish_activity` / `fish_git_store` / `fish_profile` / `fish_surface`），
**禁止 import fish**（J2）。46 个函数 + 30 个常量逐字搬自 `fish.py`，函数体零改动
（AST 等价复核）。`__all__` = 46 个设计图函数名（含 `_` 私有）；常量由 fish.py 另行
import 再导出，旧名字一个不少。
"""
import datetime
import json
import re
import sqlite3
from pathlib import Path

import fish_activity
import fish_git_store
import fish_profile
import fish_surface
from fish_board import board_block
from fish_common import (ATTENTION_OPEN_WINDOW_DAYS, ATTENTION_TITLE_MAX_CHARS,
                         CONFIG_DEFAULTS, _clip_chars, _open_loop_predicate,
                         _open_loop_rows, count_tokens, get_config, load_decay_cfg,
                         now_utc, parse_utc, resolve_owner, resolve_project_names,
                         warn_ignored)
from fish_inject import (_recent_lesson_rows, _rule_rows, count_lesson_rows,
                         count_rule_rows)
from fish_push import build_push_block
from fish_schema import AUDIT_DIR_NAME, FALLBACK_PROJECT_NAME
from fish_session import (current_open_stints, env_session_alias,
                          find_citizen_by_name, load_citizen, resolve_project_by_cwd)
from fish_visibility import _visibility_sql

# ------------------------------------------------------ 推送地基 · 会话画像（S1，13 §3 / 16 §5.2）
# 分工：**别的步骤决定"推什么"，这一步只回答"这个会话缺什么"**。
# 本步**只读**：不写任何表（落库是 S3）、不改 build_injection_pack（S4）、
# 不加 MCP 工具（mcp_server 仍是 11 个）。
#
# 为什么画像必须分成「本会话」与「同类其他会话」两栏：
#   `tool_usage` / `knowledge_gaps` 里的"我"是**会话级事实**（这个会话做过什么）；
#   但要判断"某项算不算遗漏"，必须拿**背景**来比——一个会话只有 5 次调用时，
#   凭它自己看不出什么是常态。所以 peer 一栏取同账号（无账号则同项目）的
#   其他会话，且**必须排除自己**：不排除的话 my_calls 恒等于 peer_calls，
#   盲区永远是空的（判据 C 钉的就是这一条）。
#
# 为什么画像不落库：落库要回答"多久更新一次、旧画像怎么失效"，那是 S3 的事；
# 先让计算侧能被判据钉死，再谈持久化。这也是 20 §6「先做 S1+S2 干跑」的要求。

# 11 个官方 MCP 工具（20 §1.3）。**盲区只在这个集合里报**：
# 集合外的名字（`fish://me` 之类的资源读、CLI 命令名）不是"没用过这个工具"，
# 而是"这个概念不成立"——报出去就是噪音。
TOOL_UNIVERSE = ("remember", "recall", "add_project", "supersede", "read_body",
                 "neighbors", "resolve", "whoami", "register", "inject",
                 "healthcheck")

# 工具别名 → 规范名。实测（2026-10-24）：`resolve_code` 32 次、`resolve` 0 次；
# MCP 里叫 resolve，早期审计记的是 resolve_code——**同一个工具的两个名字**。
# 不归一就会出现两个键，于是"我用了 5 次"和"同类会话用了 8 次"各算各的，盲区判定失真。
TOOL_ALIASES = {"resolve_code": "resolve"}

# 判据门槛（13 §3.3 的算式）。提成常量是为了让判据里的"门槛 5 次"能被单独钉住
# （判据 D：把 5 改成 4，盲区就必须多出来一条）。
BLIND_SPOT_MIN_PEER_CALLS = 5        # 同类其他会话累计调用 ≥5 次才算"他们常用"
BLIND_SPOT_WINDOW_DAYS = 30          # "常用"的观察窗
KNOWLEDGE_GAP_MIN_PEER_READS = 3     # 同类其他会话累计读过 ≥3 次才算"别人常读"
KNOWLEDGE_GAP_WINDOW_DAYS = 14       # "常读"的观察窗
KNOWLEDGE_GAP_LIMIT = 10             # 遗漏最多报这么多条

AUDIT_TOOL_CALLS_FILE = "tool-calls.jsonl"

# 同类判定依据的人话（why 字段要让 AI 一眼看懂"跟谁比"）。
_BASIS_TEXT = {"actor": "同账号", "project": "同项目"}


def normalize_tool_name(name):
    """工具名归一化（`resolve_code` → `resolve`）。空/非字符串 → None。

    归一化是**读侧**的事：审计文件是历史事实，不改写它；两个名字在统计时合并。
    """
    if not isinstance(name, str):
        return None
    n = name.strip()
    if not n:
        return None
    return TOOL_ALIASES.get(n, n)


def _parse_iso_utc(raw):
    """ISO8601 → UTC aware datetime；解析不了返回 None。

    **与 `fish_clock._parse_iso_utc` 是两个语义，不要合并（P2-6 审查项）**：
    本函数服务审计文件——必须吃 `2026-10-24T18:32:10+08:00` 这类带偏移行、小写 `z`、
    以及 3.10 之前只认 3/6 位小数秒的形态，并统一 `astimezone(UTC)`；`fish_clock` 的
    版本是通用宽松解析（保留原偏移、不做小数秒回退）。审计窗口过滤若换成后者，会把
    带偏移行当"解析失败"丢掉，静默漏掉最新那批调用（2026-10-24 实测教训）。

    为什么不复用 `parse_utc`：它只吃 `...SZ` 一种形态，而审计文件里真实存在
    `2026-10-24T18:32:10+08:00` 这样的带偏移行（2026-10-24 实测）。窗口过滤若把
    这类行当"解析失败"丢掉，近 30 天的盲区统计会静默漏掉最新那批调用。
    """
    if not isinstance(raw, str):
        return None
    txt = raw.strip()
    if not txt:
        return None
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        # 3.10 及更早只认 3 位或 6 位小数秒；去掉小数部分再试一次（不猜时区）
        try:
            dt = datetime.datetime.fromisoformat(re.sub(r"\.\d+", "", txt))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _audit_ts(rec):
    """审计行的时刻：优先 `ts_utc`（机器换算过的 UTC），回退 `ts`。"""
    for key in ("ts_utc", "ts"):
        dt = _parse_iso_utc(rec.get(key))
        if dt is not None:
            return dt
    return None


def _audit_in_window(rec, since):
    """是否落在观察窗内。

    缺时间戳的行**保留**：审计行是调用结束后写的，没有 ts 是记录方的缺陷，
    不能当成"旧行"——丢掉它就等于把"实测调用 6 次"静默改写成 3 次。
    """
    ts = _audit_ts(rec)
    return True if ts is None else ts >= since


def read_tool_call_audit(root):
    """读 `<root>/_audit/tool-calls.jsonl`，返回 dict 列表（坏行跳过）。

    **文件不存在 = 没有证据，不是错误**（判据 G）：新库、或宿主还没接审计工具时
    这里必然什么都没有。此时画像各项为空，但 `stats.computed_at` 仍然给出——
    "这一刻算过、结果是空"与"根本算过没有"必须能区分开。
    """
    if root is None:
        return []
    path = Path(root) / AUDIT_DIR_NAME / AUDIT_TOOL_CALLS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不到 = 没有证据：文件不存在、权限不够、编码坏了都走这里。
        # 画像只读且非关键路径，坏文件该由审计侧报警，不该让画像崩。
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _session_label(rec):
    """审计行的会话标签：`session_actor` 优先，回退 `actor`。

    两种形态在真实文件里同时存在：新行 `{actor: <公民>, session_actor: <会话名>}`，
    老行只有 `{actor: <名字>}`。只认一个会把自己另一半调用算到"未标注"里。
    """
    for key in ("session_actor", "actor"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _iter_tool_calls(audit):
    """遍历「成功记过的工具调用」，产出 (规范工具名, 原始行)。

    两条过滤都是铁的：
      · `kind == 'tool'` —— resource 读（`fish://me`）不是工具调用（20 §5.2）；
      · `ok is True`     —— 失败的调用不算"用过"（一次报错的 remember 什么都证明不了）。
    """
    for rec in audit:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") != "tool":
            continue
        if rec.get("ok") is not True:
            continue
        tool = normalize_tool_name(rec.get("name"))
        if tool:
            yield tool, rec


def _citizen_by_value(conn, value):
    """一个可能是 `display_name` 也可能是 `citizen_id` 的值 → (citizen_id, display_name)。

    **复用既有的两个读点**（`find_citizen_by_name` 按名字查活跃公民、`load_citizen`
    按 id 查冷热一致视图），不新开 citizens 查询面：P1-a 判据 G 冻结了全库
    citizens 引用函数的白名单，画像这一步没有理由再加一个口子（要加就得先声明，
    那是 P1-d 的事）。查不到 → (None, None)，绝不猜。
    """
    if not isinstance(value, str) or not value.strip():
        return None, None
    val = value.strip()
    try:
        row = find_citizen_by_name(conn, val)
    except sqlite3.Error:         # 稀有：老库缺表/缺视图。查不到 ≠ 画像可以崩
        row = None
    if row:
        return row[0], row[1]
    try:
        rec = load_citizen(conn, val)
    except sqlite3.Error:
        rec = None
    if rec:
        return rec["citizen_id"], rec["display_name"]
    return None, None


def _citizen_aliases(conn, values, *, must_exist=False):
    """把账号标识展开成「同一个人」的全部写法（display_name ↔ citizen_id）。

    `must_exist=True` 时只有库里真认得的标识才留下——会话名常常只是个纯标签
    （`anyi-studio-开发AI` 不是任何公民），把标签当账号会把不相干的会话当成"同账号"。
    另一个用途：审计里的 `actor` 有时写 display_name、有时写 citizen_id，而访问日志
    里存的是 citizen_id，展开后才能对上号。
    """
    aliases, todo = set(), [v.strip() for v in values if isinstance(v, str) and v.strip()]
    while todo:
        val = todo.pop()
        if val in aliases:
            continue
        cid, name = _citizen_by_value(conn, val)
        for other in (cid, name):
            if other and other not in aliases:
                todo.append(other)
        if (cid or name) or not must_exist:
            aliases.add(val)
    return aliases


def _display_name_of(conn, citizen_id):
    """citizen_id → display_name；查不到返回 None。"""
    return _citizen_by_value(conn, citizen_id)[1]


def _citizen_id_of(conn, value):
    """display_name / citizen_id → citizen_id；查不到返回 None。"""
    return _citizen_by_value(conn, value)[0]


def _audit_actor_values(audit, label):
    """本会话审计行里出现过的 `actor`（= 同账号的直接证据）。"""
    out = []
    for rec in audit:
        if _session_label(rec) != label:
            continue
        actor = rec.get("actor")
        if isinstance(actor, str) and actor.strip() and actor.strip() not in out:
            out.append(actor.strip())
    return out


def _resolve_profile_session(conn, audit, session_actor=None, citizen_id=None):
    """解析「这是哪个会话 / 哪个公民」，返回 {"label", "session"}。

    `label` 是后续一切匹配用的键——审计的 `session_actor` 与访问日志的 `session_key`
    用的就是这个字符串（2026-10-24 实测两处同名同形），所以它必须是**入参原样**。

    `session.citizen_id` 三级解析，都不猜：
      ① 入参 citizen_id；
      ② `citizens.display_name == 会话名`（老审计里 actor 写的就是账号名）；
      ③ 本会话审计行的 `actor` 反查公民（实测 `anyi-studio-开发AI` 的行里
         actor=`workbuddy-main`，而它确实是一个公民的 display_name）。
    """
    label = (session_actor or "").strip() or None
    if label:
        resolved_by = "session_actor"
    elif citizen_id:
        resolved_by = "citizen_id"
    else:
        resolved_by = "none"

    cid = (citizen_id or "").strip() or None
    if cid is None and label:
        cid = _citizen_id_of(conn, label)
    if cid is None and label:
        for actor in _audit_actor_values(audit, label):
            cid = _citizen_id_of(conn, actor)
            if cid:
                break
    if label is None and cid:
        label = _display_name_of(conn, cid)

    return {"label": label,
            "session": {"session_actor": label, "citizen_id": cid,
                        "resolved_by": resolved_by}}


def _project_scope(conn, label):
    """本会话可能归属的项目键集合（**只认库里已有的项目名**，不编新键）。

    两个证据源（3.2 的第 2 级）：
      ① 会话名前缀：`anyi-studio-开发AI` → `anyi-studio`。实测库里项目键是
         ANYI / 企业智能 / CDQL，所以这一条**常常什么都不给**——那正是"不许猜"
         的效果：给不出同项目就不给，而不是拿全库凑数；
      ② 本会话写入的记忆的 project（`code_index.session_actor = 会话名`）。
    """
    if not label:
        return set()
    projects = set()
    try:
        for (proj,) in conn.execute(
                "SELECT DISTINCT project FROM code_index WHERE session_actor=? "
                "AND project IS NOT NULL AND project != ''", (label,)):
            projects.add(proj)
        prefix = label.split("-")[0].strip()
        if prefix and prefix != label:
            for (proj,) in conn.execute(
                    "SELECT DISTINCT project FROM code_index WHERE LOWER(project)=LOWER(?)",
                    (prefix,)):
                projects.add(proj)
    except sqlite3.Error:
        return set()
    return projects


def _label_in_project(label, projects):
    """审计行的会话标签是否属于这些项目（`ANYI-开发AI` / `ANYI` 都算 ANYI）。"""
    if not label or not projects:
        return False
    head = label.split("-")[0].strip().lower()
    return any(label.lower() == p.lower() or head == p.lower() for p in projects)


def _candidate_scopes(conn, audit, label, citizen_id):
    """按 3.2 的优先级排出「同类」判定依据（有序，最多两个）。

    ① 同 actor（同一个 AI 账号）：账号标识来自入参 citizen_id、会话名（若它本身
       就是公民）、以及本会话审计行里的 `actor`——三条都是**已知事实**；
    ② 同 project：会话名前缀 / 本会话写过的记忆所属项目。
    两者都没有 → 返回空列表，调用方返回空结果（**绝不退回全库**）。

    为什么返回**列表**而不是一个 scope：当高优先级的依据在数据里一条同类记录
    都找不到时（例如账号在 `memory_access_log` 里从没写过行），继续用它只会
    得到"没有遗漏"这个**假阴性**——静默失败正是 16 §5.4 要防的。所以调用方
    按顺序取第一个"真的找得到同类"的依据，并把用了哪一档写进 `why`。
    """
    scopes = []
    accounts = _citizen_aliases(conn, [citizen_id, *_audit_actor_values(audit, label)])
    accounts |= _citizen_aliases(conn, [label], must_exist=True).difference({label})
    if accounts:
        scopes.append({"basis": "actor", "accounts": accounts, "projects": set()})
    projects = _project_scope(conn, label)
    if projects:
        scopes.append({"basis": "project", "accounts": set(), "projects": projects})
    return scopes


def _record_in_scope(rec, scope):
    """审计行是否属于这个"同类"范围（不含"排除自己"，那一步在调用方做）。"""
    basis = scope.get("basis")
    if basis == "actor":
        actor = rec.get("actor")
        return isinstance(actor, str) and actor.strip() in scope["accounts"]
    if basis == "project":
        return _label_in_project(_session_label(rec), scope["projects"])
    return False


def _tool_usage(audit, label):
    """本会话的工具调用次数（成功才算），按次数降序、同数按名字排。"""
    usage = {}
    for tool, rec in _iter_tool_calls(audit):
        if _session_label(rec) == label:
            usage[tool] = usage.get(tool, 0) + 1
    return {name: usage[name] for name in sorted(usage, key=lambda t: (-usage[t], t))}


def _peer_tool_counts(audit, scope, my_label, since):
    """同类**其他**会话在窗口内的工具调用次数（本会话的行一律不算）。"""
    counts = {}
    for tool, rec in _iter_tool_calls(audit):
        if _session_label(rec) == my_label:
            continue
        if not _record_in_scope(rec, scope):
            continue
        if not _audit_in_window(rec, since):
            continue
        counts[tool] = counts.get(tool, 0) + 1
    return counts


def _tool_blind_spots(audit, my_label, usage, scopes, since):
    """同类其他会话常用、本会话 0 次的工具（3.2：peer_calls ≥5 且 my_calls == 0）。"""
    for scope in scopes:
        peer = _peer_tool_counts(audit, scope, my_label, since)
        if not peer:
            continue                     # 这一档找不到同类证据 → 看下一档
        spots = []
        for tool in TOOL_UNIVERSE:       # 只报官方 11 个工具，集合外的一律不报
            peer_calls = peer.get(tool, 0)
            my_calls = usage.get(tool, 0)
            if peer_calls >= BLIND_SPOT_MIN_PEER_CALLS and my_calls == 0:
                spots.append({
                    "tool": tool,
                    "peer_calls": peer_calls,
                    "my_calls": my_calls,
                    "why": (f"{_BASIS_TEXT[scope['basis']]}其他会话近 "
                            f"{BLIND_SPOT_WINDOW_DAYS} 天调用 {peer_calls} 次，本会话 0 次"),
                })
        spots.sort(key=lambda s: (-s["peer_calls"], s["tool"]))
        return spots
    return []


def _access_scope_where(scope):
    """把 scope 翻成 `memory_access_log` 的 WHERE 片段与参数。

    访问日志与审计文件的"同类"键不同：这里只能按 `citizen_id`（谁读的）或
    `project`（在哪个项目读的）分——`session_key` 是会话，不是同类。
    """
    if scope.get("basis") == "actor" and scope["accounts"]:
        keys = sorted(scope["accounts"])
        return "m.citizen_id IN (" + ",".join("?" * len(keys)) + ")", list(keys)
    if scope.get("basis") == "project" and scope["projects"]:
        keys = sorted(scope["projects"])
        return "m.project IN (" + ",".join("?" * len(keys)) + ")", list(keys)
    return "1 = 0", []


def _peer_access_count(conn, scope, my_label, since_z):
    """同类其他会话在窗口内的访问行数（= 这一档有没有同类证据）。"""
    where, params = _access_scope_where(scope)
    sql = ("SELECT COUNT(*) FROM memory_access_log m WHERE m.accessed_at >= ? "
           "AND COALESCE(m.session_key, '') != ? AND " + where)
    try:
        return conn.execute(sql, [since_z, my_label or "", *params]).fetchone()[0]
    except sqlite3.Error:
        return 0


def _knowledge_gap_rows(conn, scope, my_label, since_z, limit):
    """遗漏候选（3.3 的四条判据全部落在 SQL 里，口径只有一份）。

    ```text
    ① 该条 status='active'
    ② 同类其他会话近 M 天读过 ≥3 次
    ③ 本会话 0 次（NOT EXISTS：本会话任何一次读过就不算遗漏）
    ④ 按"别人读的次数"降序，取前 limit
    ```
    为什么不用 `code_index.access_count`：那是**全局累计**，分不出"谁读的"——
    用它算遗漏会把本会话自己的阅读也算成"别人常读"，判据 ③ 立刻失效。
    """
    where, params = _access_scope_where(scope)
    sql = ("SELECT m.code, COUNT(*) AS n FROM memory_access_log m "
           "JOIN code_index ci ON ci.code = m.code AND ci.status = 'active' "
           "WHERE m.accessed_at >= ? AND COALESCE(m.session_key, '') != ? AND "
           + where +
           " AND NOT EXISTS (SELECT 1 FROM memory_access_log s "
           "                 WHERE s.code = m.code AND s.session_key = ?) "
           "GROUP BY m.code HAVING COUNT(*) >= ? "
           "ORDER BY n DESC, m.code ASC LIMIT ?")
    try:
        return conn.execute(sql, [since_z, my_label or "", *params, my_label or "",
                                 KNOWLEDGE_GAP_MIN_PEER_READS, int(limit)]).fetchall()
    except sqlite3.Error:
        return []


def _titles_of(conn, codes):
    """批量取标题（一条 SQL，不做 N 次查询）。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    try:
        return {c: (t or "") for c, t in conn.execute(
            "SELECT code,COALESCE(title,'') FROM code_index WHERE code IN ("
            + ",".join("?" * len(codes)) + ")", codes)}
    except sqlite3.Error:
        return {}


def _knowledge_gaps(conn, scopes, my_label, since_z, limit):
    """别人常读、本会话没读过的记忆（取第一个找得到同类证据的那一档）。"""
    for scope in scopes:
        if _peer_access_count(conn, scope, my_label, since_z) == 0:
            continue
        rows = _knowledge_gap_rows(conn, scope, my_label, since_z, limit)
        titles = _titles_of(conn, [r[0] for r in rows])
        return [{
            "code": code,
            "title": titles.get(code, ""),
            "others_read": int(n),
            "my_reads": 0,
            "why": (f"{_BASIS_TEXT[scope['basis']]}其他会话近 "
                    f"{KNOWLEDGE_GAP_WINDOW_DAYS} 天读 {int(n)} 次，本会话 0 次"),
        } for code, n in rows]
    return []


def _access_log_total(conn, label):
    """本会话在 `memory_access_log` 里的行数（表不在 → 0，绝不崩）。"""
    if not label:
        return 0
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_access_log WHERE session_key=?",
            (label,)).fetchone()[0]
    except sqlite3.Error:
        return 0


def compute_session_profile(conn, *, session_actor=None, citizen_id=None,
                            root=None) -> dict:
    """算一个会话的画像：**只读**，不写任何表、不碰注入包。

    返回结构（**字段固定，不多不少**——S3 落库与 S4 注入都按这个契约读）：

    ```python
    {
      "session": {"session_actor", "citizen_id", "resolved_by"},
      "tool_usage": {"remember": 128, ...},          # 本会话成功调用次数
      "tool_blind_spots": [{"tool", "peer_calls", "my_calls", "why"}],
      "knowledge_gaps": [{"code", "title", "others_read", "my_reads", "why"}],
      "stats": {"tool_calls_total", "access_log_total", "computed_at"},
    }
    ```

    三个子算法的口径（13 §3.3 / 16 §5.3）：
      · `tool_usage`   —— `data/_audit/tool-calls.jsonl` 里 `session_actor` 精确匹配、
                          `kind='tool'` 且 `ok=True`（`resolve_code` 归一成 `resolve`）；
      · `tool_blind_spots` —— 同类其他会话近 30 天调用 ≥5 次、本会话 0 次，且只在
                          11 个官方工具里报；
      · `knowledge_gaps`   —— `memory_access_log` 里同类其他会话近 14 天读过 ≥3 次、
                          本会话 0 次、该条 status='active'，按别人读的次数降序取前 10。

    `root` 是数据目录（`connect(root)` 的那个 root）；缺省时读不到审计文件，
    `tool_usage` / `tool_blind_spots` 自然为空——这**不是错误**，见
    `read_tool_call_audit` 的说明。
    """
    computed_at = now_utc()
    now_dt = parse_utc(computed_at) or datetime.datetime.now(datetime.timezone.utc)
    since_audit = now_dt - datetime.timedelta(days=BLIND_SPOT_WINDOW_DAYS)
    since_access = now_dt - datetime.timedelta(days=KNOWLEDGE_GAP_WINDOW_DAYS)

    audit = read_tool_call_audit(root)
    resolved = _resolve_profile_session(conn, audit, session_actor=session_actor,
                                       citizen_id=citizen_id)
    label = resolved["label"]

    profile = {
        "session": resolved["session"],
        "tool_usage": {},
        "tool_blind_spots": [],
        "knowledge_gaps": [],
        "stats": {"tool_calls_total": 0, "access_log_total": 0,
                  "computed_at": computed_at},
    }
    if not label:
        # 连"是哪个会话"都说不出来 → 不猜：返回空画像（统计口径仍是"这一刻算过"）。
        return profile

    usage = _tool_usage(audit, label)
    scopes = _candidate_scopes(conn, audit, label, resolved["session"]["citizen_id"])
    profile["tool_usage"] = usage
    profile["tool_blind_spots"] = _tool_blind_spots(audit, label, usage, scopes, since_audit)
    profile["knowledge_gaps"] = _knowledge_gaps(
        conn, scopes, label, since_access.strftime("%Y-%m-%dT%H:%M:%SZ"), KNOWLEDGE_GAP_LIMIT)
    profile["stats"]["tool_calls_total"] = sum(usage.values())
    profile["stats"]["access_log_total"] = _access_log_total(conn, label)
    return profile

# ------------------------------------------------------ L1「当前项目」的四档解析
# 2026-10-24（R1 根治）：项目归属必须有证据，按下面顺序取**第一个有证据的**档：
#   ① explicit      调用方显式给了 project 参数（最高优先：调用方知道自己在问什么）
#   ② stint         该公民当前开着的项目档期（`fish session start --project` 写的会话事实）
#   ③ workspace_map 调用方给了 cwd，且命中一条路径映射（环境事实）
#   ④ inferred      **兜底**：都没有 → 退回"最近更新"，但**必须在输出里标注这是推断**
# 第 ④ 档保留而不是删掉，是为了不让老调用方（与老判据）失去 L1 的项目行；但措辞必须
# 让人一眼看出"这是猜的"——猜错项目比不显示项目危险得多（EVAL-261024-02 R1 的定性）。
# 连"最近更新"都没有（空库）时，第 ④ 档也只能说"未声明"，不许编一个项目名。

_INFERRED_PROJECT_NOTE = "据最近更新推断 —— 建议用 `fish session start --project` 声明"
_UNDECLARED_PROJECT_LINE = (
    "当前项目：未声明（未传 project、无开档期、workspace_map 无匹配；系统不替你猜）")
_PROJECT_SOURCES = ("explicit", "stint", "workspace_map", "inferred", "undeclared")

# 兜底项目（2026-10-25 · T1）：cwd 解析不出具体项目时，会话落在哪个项目上。
#
# 为什么需要它：决策 12 定的是「生活 / 工作 / 未分类 都是**真实项目**」——用户可能
# 从不提项目名，那些对话必须有个容器接住，否则记忆无处落脚。实证代价就摆在这儿：
# `project_stints` 在生产库**恒为 0**，因为 cwd 未匹配 → 干脆不开档期，于是
# 「这个会话什么时候在哪个项目干活」这条最基础的事实，系统一条都没记过。
#
# ⚠️ 名字是**数据**不是代码常量：只有它在 code_registry 里真实登记过才用；没登记就
# 维持"不开档期"的老行为——代码里凭空造一个项目，等于在库里写一条假事实。
def fallback_project(conn):
    """兜底项目前缀；库里没登记过就返回 None（只读，不建项目）。"""
    row = conn.execute(
        "SELECT prefix FROM code_registry WHERE prefix=? AND status='active'",
        (FALLBACK_PROJECT_NAME,)).fetchone()
    return row[0] if row else None


def _project_activity(conn, project) -> tuple:
    """该项目的活跃量：(活跃条数, 最近一次 recorded_at)。

    口径与 L1 一致：dormant=0 且 status='active'——已替代/已作废的不算活跃记忆。

    **2026-10-25：按别名展开统计**。同一个项目的两个入口（`cdql` 9 条 / `CDQL` 2 条）
    如果只算规范名，会显示「活跃 2 条」而实际有 11 条——数字偏小比偏大更危险，
    因为它会让 AI 以为"这个项目没什么东西"从而不去查。
    """
    pnames = resolve_project_names(conn, project)
    if not pnames:
        pnames = [project]
    ph = ",".join("?" * len(pnames))
    row = conn.execute(
        f"SELECT COUNT(*), MAX(recorded_at) FROM code_index "
        f"WHERE project IN ({ph}) AND dormant=0 AND status='active'", pnames).fetchone()
    return int(row[0] or 0), (row[1] or "")


def _resolve_injection_project(conn, citizen_id, project=None, cwd=None) -> tuple:
    """按四档优先级解析 L1 的"当前项目"，返回 (项目|None, 来源, 其他项目列表)。

    来源取值见 _PROJECT_SOURCES；"其他项目"只在 ② 档有值（多档期时**主项目以外**的
    那些，按开档顺序去重）。为什么只在 ② 档列：其余档的项目来源本身只有一个值，
    硬凑"同时在其他项目"会把"调用方只问了 ANYI"说成"你还在企业智能"。

    project/cwd 的类型校验放在这里：它们来自 CLI 或 MCP 入参，非字符串（误传 dict）
    应该响亮报错——静默 str() 会把调用方写错的 bug 埋进输出里。
    """
    if project is not None and not isinstance(project, str):
        raise TypeError(f"project 必须是字符串，收到 {type(project).__name__}")
    if cwd is not None and not isinstance(cwd, str):
        raise TypeError(f"cwd 必须是字符串，收到 {type(cwd).__name__}")

    explicit = (project or "").strip()
    if explicit:
        # 2026-10-25：**显式声明也要走别名归一**。
        # 不归一的后果实测过：`inject --project ANYI`（前身名）显示「活跃 0 条」，
        # 而记忆全在 `anyi-studio` 下（活跃 77 条）——**第一屏就给了个空项目**，
        # 正是 `08` §2.1 说的"注入包的第一屏经常是错的，而它不会说我不确定"。
        # 归一取规范名（`resolve_project_names` 的第一个元素），但**来源仍是 explicit**：
        # 调用方的声明优先级不变，变的只是名字被解析成了它当前的真名。
        canon = resolve_project_names(conn, explicit)
        return (canon[0] if canon else explicit), "explicit", []
    if citizen_id:
        stints = current_open_stints(conn, citizen_id)
        if stints:
            primary = stints[-1]["project"]          # 最近开的那个 = 主项目
            others = []
            for st in stints[:-1]:
                name = st["project"]
                if name != primary and name not in others:
                    others.append(name)
            return primary, "stint", others
    if (cwd or "").strip():
        by_map = resolve_project_by_cwd(conn, cwd)
        if by_map:
            return by_map, "workspace_map", []
    # 第 ④ 档：兜底。这个查询就是 R1 点名的"最近更新者即当前项目"——保留作兜底，
    # 但调用方（_render_project_head）会把 source='inferred' 标注进输出。
    #
    # 2026-10-25 加一道前置（用户实测抓到的）：**只对"来过的人"用兜底**。
    # 一个第一次接入的 AI 没有任何痕迹，"最近更新的项目"是**别人**的工作痕迹——
    # 拿它当这个 AI 的当前项目，等于给它一个错误前提（它会按别人的语境去工作）。
    # `12` 的原话是「猜错项目比不报项目危险得多」，而这里连"猜"的资格都不该有。
    #
    # "来过"的判据取**任一痕迹**：开过档期 / 写过记忆 / 读过东西。
    # 为什么不能只看档期：老数据里很多公民有记忆但从未开过档期（`project_stints`
    # 长期恒 0 就是那个 bug），只判档期会把它们也打成"没来过"（判据 F 当场抓到）。
    if citizen_id:
        touched = conn.execute(
            "SELECT 1 FROM project_stints WHERE citizen_id=? "
            "UNION ALL SELECT 1 FROM code_index WHERE owner=? "
            "UNION ALL SELECT 1 FROM memory_access_log WHERE citizen_id=? LIMIT 1",
            (citizen_id, citizen_id, citizen_id)).fetchone()
        if not touched:
            return None, "undeclared", []
    row = conn.execute(
        "SELECT project FROM code_index "
        "WHERE dormant=0 AND status='active' AND project IS NOT NULL AND project <> '' "
        "GROUP BY project ORDER BY MAX(recorded_at) DESC LIMIT 1").fetchone()
    if row:
        return row[0], "inferred", []
    return None, "undeclared", []


def _render_repo_line(conn, cwd) -> str:
    """L1 的仓库行：当前工作目录所属仓库的最近一次观测（P3-3 · 2026-11-20）。

    ## 三个刻意的设计

    **① 判不到就返回空串（不出现）。**
    不写「未知仓库」这种噪音——噪音会训练 AI 跳过整个 status 段（③④ 两段
    的老注释里有同一条纪律）。

    **② `unknown` 绝不说成「净」。**
    `unknown` 的真实含义是「没采到」，与 `clean` 是**不同的事实**。
    所以它渲染成「未采集（不是「干净」，是没采到）」而不是「净」。
    J4 判据的**反向断言**就守这一条。

    **③ 用最长前缀匹配找 checkout，不 spawn git。**
    cwd 通常是仓库内的子目录。这里在内存里对已登记的 checkout 做分量级
    最长前缀匹配（`path` 是 `cwd` 的祖先），比 `discover_root` 省 145ms。
    """
    if not cwd:
        return ""
    norm = fish_surface.normalize_path(cwd)
    if not norm:
        return ""
    try:
        rows = conn.execute(
            "SELECT c.checkout_id, c.path, r.display_name, r.identity, c.repo_id "
            "FROM git_checkout c JOIN git_repo r ON r.repo_id = c.repo_id "
            "WHERE c.missing_since IS NULL").fetchall()
    except sqlite3.Error as exc:
        # 老库还没有 git 表：这一行就不出现。但不许静默——走统一出口。
        warn_ignored('fish.py _render_repo_line', exc)
        return ""
    best = None
    for cid, path, name, ident, rid in rows:
        base = (path or "").rstrip("/")
        if not base:
            continue
        if norm == base or norm.startswith(base + "/"):
            if best is None or len(base) > len(best[1].rstrip("/")):
                best = (cid, base, name, ident, rid)
    if best is None:
        return ""
    cid, base, name, ident, rid = best
    label = name or (ident.rsplit("/", 1)[-1] if ident else base.rsplit("/", 1)[-1])
    try:
        obs = fish_git_store.latest_obs(conn, cid)
    except sqlite3.Error:
        obs = None
    if obs is None:
        return f"当前仓库：{label}（未采集——不是「干净」，是还没采过）"
    state = obs.get("state") or "unknown"
    if state == "clean":
        status = "净"
    elif state == "dirty":
        status = "有未提交改动"
    elif state == "degraded":
        status = f"状态不可靠（{obs.get('degraded_reason') or '原因未知'}）"
    else:
        status = "未采集——不是「干净」，是没采到"
    branch = obs.get("branch") or "(detached)"
    # ── P5-3（2026-11-20）：最近提交 ────────────────────────────────────
    # 协作价值：撞车机制只能告诉你「有人**正在写**」，提交事件能告诉你
    # 「有人**已经写完并提交**」——那是完全不同的信息（方案 §6 缺口三）。
    try:
        last = conn.execute(
            "SELECT sha, committed_at FROM git_commit WHERE repo_id=? "
            "ORDER BY committed_at DESC LIMIT 1", (rid,)).fetchone()
    except sqlite3.Error:
        last = None
    extra = f"；最近提交 {last[0][:8]} {last[1][:10]}" if (last and last[1]) else ""
    return f"当前仓库：{label}（{branch}，{status}{extra}）"


def _render_project_head(project, count, last_seen, source) -> str:
    """L1 的项目行。走 ④ 档（inferred）时**必须**带上"这是推断"的标注。

    2026-10-25（T1）：落兜底项目时也必须带标注。否则 AI 看到「当前项目：未分类」
    会把它当成一个正常项目名——而它真正的含义是"你没说在哪，系统替你接住了"。
    诚实信号不能因为"有了容器"就丢掉（21 号文档 §2.1 的保留疑问，这是它的答案）。
    """
    if source not in _PROJECT_SOURCES:
        # 来源词表是 meta 契约的一部分（调用方按它决定怎么显示/是否告警）。
        # 拼错一个词就应该当场响，而不是让上层把它当"某种有证据的来源"。
        raise ValueError(f"未知的项目来源：{source!r}（可用：{_PROJECT_SOURCES}）")
    if source == "inferred":
        return f"当前项目：{project}（活跃 {count} 条，{_INFERRED_PROJECT_NOTE}）"
    tail = f"，最近更新 {last_seen[:10]}" if last_seen else ""
    line = f"当前项目：{project}（活跃 {count} 条{tail}）"
    if project == FALLBACK_PROJECT_NAME:
        line += ("\n　（这是兜底项目：你给的工作目录没映射到具体项目，记忆暂时落在这里；"
                 "用 `fish workspace set` 声明后可归到真项目）")
    return line

# ------------------------------------------------------ ③ 规矩 / ④ 该注意什么
# 2026-10-24（全局复盘）：新 AI 接入时缺两段——「这里的规矩」与「我该注意什么」。
# 这两段**不是新架构**，是「让新来的人少踩坑」：
#   ③ 规矩      = 长期有效的偏好/硬规则（只给 code+标题，正文按需 resolve_code 读）；
#   ④ 该注意什么 = 临时信号（未闭合 / 最近的坑 / 别处常读而我没读）。
# 段的次序本身就是优先级：规矩在注意事项**之前**、也在裁剪顺序里更该保留——
# 规矩是长期的，注意事项是临时的；临时信息比长期规范更可弃。
#
# 两条贯穿两段的纪律（都是踩过的坑）：
#   · **空就不出现**：不写"暂无规矩"这种噪音。噪音会训练 AI 跳过这一段标题，
#     等于把"有信号"和"没信号"在观感上抹平。
#   · 只列标题不列正文：注入包是每会话开场都用的路径，正文会把预算吃光；
#     AI 真正需要的是"知道有这条"，读全文走 resolve_code（那是主动行为，可计量）。
#
# ③ 的分栏口径：**按适用范围，不按作者**（2026-10-25 修正，上一版是错的）。
#   · 上一版按 `owner` 分栏（「主人规矩」+「本项目规矩」）——语义错位：
#     `code_index.owner` 记的是"**谁写的**"，不是"这条规范**约束谁**"。
#     生产库实证：resolve_owner() = 安逸，而安逸名下 rule = 0 条 → 「主人」栏永远空；
#     全库 39 条 active rule 的作者是 workbuddy-main(37) / codex-main(1) / 无归属(1)。
#     所以「按作者筛规矩」筛的是写的人，不是适用范围——从语义上就不成立。
#   · 现行口径两栏，且两栏**谓词互斥**（同一条规矩不可能既绑项目又不绑项目）：
#       ① 本项目规矩 ← project = 当前项目
#       ② 全局规矩   ← project IS NULL（不绑定任何项目，才算"处处适用"）
#   · **全局栏在生产库目前是 0 条**（所有 rule 都带 project）→ 空就不出现，这是正常态，
#     不是 bug；也不许为了"让栏好看"把某个项目的规矩提成全局规矩。
#   · rule 标题里含「必须/不得/一律/所有」等通用词的实测只有 1 条：
#     现在的 rule 绝大多数是**项目特定**的，这也支持"按项目分栏、不硬造全局栏"。

# 2026-10-26（FB-02）：这两个数原来是写死的常量。写死的问题不是"数不对"，而是
# **"想多看一条规矩"要么改代码、要么放弃**——而"看几条"本来就是口味差异。
# 现在默认值只在配置表里写一次（`CONFIG_DEFAULTS`），这里派生引用：
# 两份各写一个 3，迟早出现"改了一处、另一处还在用旧值"的漂移。
RULES_PER_SOURCE_LIMIT = int(CONFIG_DEFAULTS["inject.rules_per_source"][0])
RULES_PER_SOURCE_MIN = 1            # 下限：0 条规矩等于"这段没有意义"
RULES_PER_SOURCE_MAX = 10           # 上限：再多就把整包预算吃掉了（每条约 15-25 token）
RULES_BLOCK_MAX_TOKENS = 300        # ③ 段自身的硬上限（超了少列一条，**不截断标题**）
BOARD_MAX_RATIO = float(CONFIG_DEFAULTS["inject.board_max_ratio"][0])
ATTENTION_PER_TYPE_LIMIT = 2        # ④ 每类信号各取 top N
ATTENTION_BLOCK_MAX_TOKENS = 400    # ④ 段自身的硬上限（超了从末尾丢行）
ATTENTION_LESSON_WINDOW_DAYS = 7    # B 最近的坑：近 7 天
# A 类判据是**标题里的词**（不是正文）：注入包只看标题，正文里的"待办"看不见。
# 词表用**小写**存，SQL 侧对 LOWER(title) 做 INSTR —— 中文不受大小写影响，
# 但 `TODO` 在本库真实标题里既有大写也有小写，所以统一走小写匹配。
# ④ 段 C 类的**扫描上限**（评审 🟡5 · 2026-10-26）：段头要印「别处读过 b/B」，
# B 必须来自一次"不设 per_type 闸"的取数，否则"这条信号到底有几条"永远不可知。
# 50 是防御性上限（该信号在生产库实测是 0-2 条量级）：真到 50 条时，
# 段头写的是"50"而不是"更多"——那仍然比"只印 2"诚实一个量级。
ATTENTION_GAP_SCAN_LIMIT = 50

# ④ 段三类信号的行前缀。也是"这条为什么出现在这里"的可读说明。
_ATTENTION_LABELS = {"open": "未闭合", "lesson": "最近的坑", "gap": "别处读过但你没看"}


def _clamp_rules_per_source(value) -> int:
    """把每栏条数夹到 1–10（配置写错不该让注入包报错，也不该静默变成 0 条）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return RULES_PER_SOURCE_LIMIT
    return max(RULES_PER_SOURCE_MIN, min(RULES_PER_SOURCE_MAX, n))


def build_short_rules_block(conn, *, project=None, per_source=None,
                            max_tokens=RULES_BLOCK_MAX_TOKENS,
                            viewer_citizen_id=None) -> dict:
    """段③「规矩」：本项目规矩 + 全局规矩，各取最近 N 条。

    返回 `{"text", "codes", "trimmed", "total", "omitted"}`：
      · `text`    —— 印进注入包的整段（**结尾带换行**；两栏都空时为 ""，调用方据此整段不出现）；
      · `codes`   —— 真正印进包里的编码（`meta["rules_codes"]` / push_log 用）；
      · `trimmed` —— 本段自己的裁剪标记（超 `max_tokens` 时**少列条目**，绝不截断标题）；
      · `total`   —— 本段两栏**共有**多少条（不受 limit / 预算影响）；
      · `omitted` —— `total - len(codes)`：被上限或预算省掉的条数。

    2026-10-26（FB-02）：段头现在会**声明自己不全**——`规矩段：显示 3/7 条…`。
    旧实现固定 3 条、且回包不提这件事，于是读者无从判断"这是全部还是抽样"。
    声明放在两栏表头**之前**（块头）而不是结尾：结尾的话，读的人早就已经
    按"这就是全部"理解完了。

    `per_source=None` → 读配置 `inject.rules_per_source`（默认 3，夹紧到 1–10）。
    为什么默认值走配置而不是形参常量：调用点（`build_injection_pack`）不该关心
    这个数——它属于"注入包的呈现口味"，与预算、风格是同一类旋钮。

    为什么"最近 N 条"而不是全部：本库 rule 已有 39 条（生产库实测），全列会吃光预算；
    规矩的**新旧**代表当前在用的版本，老的还在库里（recall 查得到），不必占据开场那一屏。

    `project` 为空（当前项目未声明）时**只有全局栏**——不拿"某个项目"冒充你的项目。
    **不再接受 `owner_id`**：按作者筛规矩是 2026-10-25 证伪的口径（见本节开头）。
    两栏标题：本项目规矩（ANYI）：/ 全局规矩：；空栏整块不出现（不写"暂无"）。
    """
    per_source = _clamp_rules_per_source(
        get_config(conn, "inject.rules_per_source") if per_source is None else per_source)
    proj_key = (project or "").strip() or None
    proj_items = _rule_rows(conn, project=proj_key, limit=per_source,
                                viewer_citizen_id=viewer_citizen_id) if proj_key else []
    taken = {code for code, _ in proj_items}
    global_items = _rule_rows(conn, project=None, limit=per_source, exclude=taken,
                                  viewer_citizen_id=viewer_citizen_id)
    total = (count_rule_rows(conn, project=proj_key,
                             viewer_citizen_id=viewer_citizen_id) if proj_key else 0) \
        + count_rule_rows(conn, project=None, viewer_citizen_id=viewer_citizen_id)

    def render(proj_list, global_list):
        lines, codes = [], []
        if proj_list:
            lines.append(f"本项目规矩（{proj_key}）：")
            for code, title in proj_list:
                lines.append(f"- {code} {title}")
                codes.append(code)
        if global_list:
            lines.append("全局规矩：")
            for code, title in global_list:
                lines.append(f"- {code} {title}")
                codes.append(code)
        if lines:
            # 段头的"我列了多少 / 一共有多少"声明（FB-02）。只在**真的少了**的时候
            # 才说"其余因预算/上限省略"——N==M 时还这么写就是一句假话。
            shown = len(proj_list) + len(global_list)
            head = (f"规矩段：显示 {shown}/{max(total, shown)} 条"
                    f"（本项目 {len(proj_list)} + 全局 {len(global_list)}）")
            if shown < total:
                head += "；其余因预算/上限省略"
            lines.insert(0, head)
        return ("\n".join(lines) + "\n") if lines else "", codes

    text, codes = render(proj_items, global_items)
    if not text:
        return {"text": "", "codes": [], "trimmed": [], "total": total, "omitted": 0}

    proj_list, global_list = list(proj_items), list(global_items)
    trimmed = []
    # 自身硬上限：从**全局栏末尾**往回删（本项目规矩更针对手头工作，更该留）；
    # 全局栏空了才动本项目栏。只丢整条，不截断标题。
    while text and count_tokens(text) > max_tokens and (proj_list or global_list):
        if global_list:
            global_list.pop()
        else:
            proj_list.pop()
        text, codes = render(proj_list, global_list)
        if "规矩截断" not in trimmed:
            trimmed.append("规矩截断")
    return {"text": text, "codes": codes, "trimmed": trimmed,
            "total": total, "omitted": max(0, total - len(codes))}


def count_open_loop_rows(conn, since_z, exclude=()):
    """A 类在窗口内**总共有多少条**（不受 `per_type` 限制）——段头 N/M 的 M。

    读失败返回 **None**，不返回 0（2026-10-26 复审 ⚪N8）：段头会把 M 念成
    「显示 2/2 条」，读者据此相信"这一类就这两条"。而真相是**读不出来**——
    把未知说成 0 是比"少列几条"更自信的假，且无法从文本上分辨。
    调用方（`build_attention_block`）见到 None 就写「总数未知（读取失败）」。
    """
    where, keywords = _open_loop_predicate()
    sql = "SELECT COUNT(*) FROM code_index WHERE " + where
    # 参数顺序 = 占位符顺序：`recorded_at >= ?` 在前，关键词在后（`_open_loop_predicate`
    # 只回关键词，窗口起点由调用方给——它是"这一刻"的证据，不属于谓词定义）。
    params = [since_z] + list(keywords)
    extra = [c for c in (exclude or ()) if c]
    if extra:
        sql += " AND code NOT IN (" + ",".join("?" * len(extra)) + ")"
        params += extra
    try:
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return None


def _attention_gaps(conn, root, citizen_id, session_actor, limit):
    """C 类「别处读过但你没看」：**直接复用 S1 的 `_knowledge_gaps`**，不另写一份。

    为什么必须复用而不是重写：这条判据有四个口径（`status='active'` / 别人读 ≥3 次 /
    本会话 0 次 / 14 天窗口）加一条"同类依据怎么选"。两处各写一份 SQL，改门槛时
    必有一处漏改——那正是"两份实现互相漂移"的 bug 来源。所以这里只做**参数准备**
    （会话标签、同类范围、窗口起点），判定与取数全部交给 S1。
    """
    audit = read_tool_call_audit(root)
    resolved = _resolve_profile_session(conn, audit, session_actor=session_actor,
                                        citizen_id=citizen_id)
    label = resolved["label"]
    if not label:
        # 连"我是哪个会话"都不知道 → C 类**没有**可说的（不拿全库凑数）。
        return []
    scopes = _candidate_scopes(conn, audit, label, resolved["session"]["citizen_id"])
    if not scopes:
        return []
    since_z = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=KNOWLEDGE_GAP_WINDOW_DAYS)).strftime(
                   "%Y-%m-%dT%H:%M:%SZ")
    try:
        return _knowledge_gaps(conn, scopes, label, since_z, int(limit))
    except sqlite3.Error:
        return []


def build_attention_block(conn, root, *, citizen_id=None, session_actor=None,
                          project=None,
                          per_type=ATTENTION_PER_TYPE_LIMIT,
                          max_tokens=ATTENTION_BLOCK_MAX_TOKENS) -> dict:
    """段④「该注意什么」：未闭合 / 别处读过但你没看，各取 top N。

    返回 `{"text", "codes", "trimmed"}`（与另两段同构）：
      · `text`    —— 两类**全空时为 ""**（整段不出现，不写"暂无"）；
      · `codes`   —— 印进包里的编码（`meta["attention_codes"]`）；
      · `trimmed` —— 本段自己的裁剪标记（超 `max_tokens` 时从末尾丢行）。

    2026-10-25（T9）：原本的第三类「最近的坑」（lesson）已独立成段⑤
    `build_lesson_block`（理由见那个函数——rule 与 lesson 是两种性质的约束）。
    因此段④**不再因 `project` 改变行为**：A 类未闭合跨项目都要看见（别人停在哪儿
    与你在哪个项目无关），C 类的高价值未读有自己的 S1 口径。
    `project` 参数保留**只为兼容既有调用方**，本段不读它。
    """
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    open_since = (now_dt - datetime.timedelta(days=ATTENTION_OPEN_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    bags = {"open": [], "gap": []}
    seen = set()
    # 第四轮评审 🟠-4：传 viewer，否则开关打开后主人自己也看不到自己的未闭合事项。
    for code, title in _open_loop_rows(conn, open_since, per_type,
                                       viewer_citizen_id=citizen_id):
        if not code or code in seen:
            continue
        seen.add(code)
        bags["open"].append((code, title))
    # 2026-10-26（评审 🟡5）：C 类先按**扫描上限**取全，再切 `per_type`——段头要印
    # 「别处读过 b/B」，B 得是这条信号的真实规模。旧实现只拿到 `per_type` 条，
    # 于是"到底有几条"永远不可知（而 `_（无）_` 与"只有 0 条"必须能分开）。
    gap_scan = _attention_gaps(conn, root, citizen_id, session_actor,
                               ATTENTION_GAP_SCAN_LIMIT)
    gap_total = len(gap_scan)
    for item in gap_scan[:per_type]:
        code = (item or {}).get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        bags["gap"].append((code, (item.get("title") or "").strip()))
    open_total = count_open_loop_rows(conn, open_since)

    order = ("open", "gap")

    def _count_part(label, shown, total, capped=False):
        """段头里的一个「标签 N/M」片段。

        `total is None` = 读数失败（`count_*` 返回 None）→ 写「未知」，**不写 0**：
        「未闭合 2/0」会被读者当成"总共 0 条却列了 2 条"的荒谬，"未知"才是事实。
        `capped` = M 是**下限**（C 类扫描到上限就停，真实规模只会更大）→ 加 `≥`。
        旧实现把 `2/50` 印成确切值，读者会以为"这一类总共就 50 条"。
        """
        if total is None:
            return f"{label} {shown}/未知"
        mark = "≥" if capped else ""
        return f"{label} {shown}/{mark}{max(int(total), int(shown))}"

    def render():
        lines, codes = [], []
        for key in order:
            for code, title in bags[key]:
                lines.append(f"- {_ATTENTION_LABELS[key]}：{code} "
                             + _clip_chars(title, ATTENTION_TITLE_MAX_CHARS))
                codes.append(code)
        if not lines:
            return "", []
        # 段头的"我列了多少 / 一共有多少"声明（评审 🟡5）：与③规矩同一款。
        # 为什么必须声明：④⑤ 是**固定条数**（每类 2 条）的段，而固定条数在文本上
        # 与"总共就这么多"长得一模一样——读者会把"只列了 2 条"当成"只有 2 条"，
        # 于是漏掉的是他不知道自己缺的那部分。段头放在条目之前（读之前就看见）。
        shown = len(bags["open"]) + len(bags["gap"])
        gap_capped = gap_total >= ATTENTION_GAP_SCAN_LIMIT
        parts = []
        if bags["open"] or open_total is None or open_total:
            parts.append(_count_part("未闭合", len(bags["open"]), open_total))
        if bags["gap"] or gap_total:
            parts.append(_count_part("别处读过", len(bags["gap"]), gap_total,
                                     capped=gap_capped))
        detail = " + ".join(parts) if parts else "无信号"
        if open_total is None:
            # A 类读数失败 → 总体 M 也不可信，整段头降级成"不报总数"。
            head = (f"该注意什么：显示 {shown} 条（总数未知（读取失败）；"
                    f"{detail}；每类最多 {int(per_type)} 条）")
        else:
            total = open_total + gap_total
            total_txt = f"≥{total}" if gap_capped else f"{max(total, shown)}"
            head = (f"该注意什么：显示 {shown}/{total_txt} 条（"
                    + detail + f"；每类最多 {int(per_type)} 条）")
        return (head + "\n" + "\n".join(lines) + "\n"), codes

    text, codes = render()
    # `total=None` = 读数失败（与段头同一事实）：meta 里宁可缺一个数，也不许写 0。
    total_known = None if open_total is None else open_total + gap_total
    if not text:
        return {"text": "", "codes": [], "trimmed": [], "shown": 0, "total": total_known}

    trimmed = []
    # 自身硬上限：从**末尾**往回丢（末尾是 C 类；A 类「未闭合」最该留）。
    while text and count_tokens(text) > max_tokens and any(bags.values()):
        for key in reversed(order):
            if bags[key]:
                bags[key].pop()
                break
        text, codes = render()
        if "注意截断" not in trimmed:
            trimmed.append("注意截断")
    return {"text": text, "codes": codes, "trimmed": trimmed,
            "shown": len(codes), "total": total_known}


def build_lesson_block(conn, *, project=None, per_type=ATTENTION_PER_TYPE_LIMIT,
                       max_tokens=ATTENTION_BLOCK_MAX_TOKENS, exclude=(),
                       viewer_citizen_id=None) -> dict:
    """段⑤「错题本」：窗口内的 lesson，**同项目优先**，不足用跨项目补齐。

    为什么从「该注意什么」里独立出来（2026-10-25 · T9）：
      `rule` 是"该怎么做"，`lesson` 是"别再这样"——两种约束的**性质**不同。
      混在段④里，AI 无法从段名判断"这条是通用教训还是本项目的事"，也让已有的
      「规矩（长期）/ 注意（临时）」分层被第三种东西糊住。拆开之后三段各归其位：
      ③规矩 = 规范，④该注意什么 = 临时信号，⑤错题本 = 已经踩过的坑。

    为什么仍按「同项目优先、跨项目补齐」：通用教训（例如"判断谁在写"的证据顺序）
    跨项目有效，不该被排除，只是**排序靠后**（口径在 `_recent_lesson_rows` 里，
    本函数不另写一份——那正是"两份实现互相漂移"的来源）。

    `exclude`：段④已印过的编码，由调用方传入。**一条记忆只在一段里出现**——
    同一件事占两段预算，等于用两倍的篇幅说同一句话。

    返回 `{"text", "codes", "trimmed"}`（与另两段同构；全空时 text=""，整段不出现）。
    """
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    lesson_since = (now_dt - datetime.timedelta(days=ATTENTION_LESSON_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # **第六轮评审 🔴**：第 42 轮把 viewer 接进了 rules 与两个计数器，
    # **漏了这一行**——于是段头把主人的私有 lesson 算进 M、却不列出，
    # 也不说原因。三行之外 `count_lesson_rows` 传了 viewer，同一函数两套口径。
    rows = _recent_lesson_rows(conn, lesson_since, per_type,
                               exclude=set(exclude or ()), project=project,
                               viewer_citizen_id=viewer_citizen_id)
    items = [(code, title) for code, title in rows if code]
    # 2026-10-26（评审 🟡5）：段头声明「显示 N/M 条」。M 走 `count_lesson_rows`
    # （与取数同一谓词，扣掉已在别段印过的编码）——固定 2 条的段不声明，
    # 读者就会把"只列 2 条"当成"库里只有 2 条坑"。
    lesson_total = count_lesson_rows(conn, lesson_since, exclude=set(exclude or ()),
                                     viewer_citizen_id=viewer_citizen_id)

    def render():
        if not items:
            return "", []
        shown = len(items)
        if lesson_total is None:
            # 读数失败（`count_lesson_rows` 返回 None）→ 不报 M，明说未知。
            head = (f"错题本：显示 {shown} 条（总数未知（读取失败）；"
                    f"同项目优先，不足跨项目补齐；每类最多 {int(per_type)} 条）")
        else:
            head = (f"错题本：显示 {shown}/{max(lesson_total, shown)} 条"
                    f"（同项目优先，不足跨项目补齐；每类最多 {int(per_type)} 条）")
        lines = [f"- {code} {_clip_chars(title, ATTENTION_TITLE_MAX_CHARS)}"
                 for code, title in items]
        return (head + "\n" + "\n".join(lines) + "\n"), [c for c, _ in items]

    text, codes = render()
    if not text:
        return {"text": "", "codes": [], "trimmed": [], "shown": 0, "total": lesson_total}

    trimmed = []
    # 自身硬上限：从**末尾**丢整条（与另两段同一纪律：不截断标题）。
    while text and count_tokens(text) > max_tokens and items:
        items.pop()
        text, codes = render()
        if "错题本截断" not in trimmed:
            trimmed.append("错题本截断")
    return {"text": text, "codes": codes, "trimmed": trimmed,
            "shown": len(codes), "total": lesson_total}

# ---------------------------------------------- 注入包的"段选择"（FB-02 · 2026-10-26）
#
# 预算只回答"总共多少 token"，不回答"这一次要哪几段"。于是出现两类浪费：
#   · 只想看"我现在在哪"时，候选/看板/L2 一起进来，用预算买噪音；
#   · 想给某个场景做裁剪（如工具后推送只要态势），只能整包拿、自己再切。
# 现在 `sections` 直说"我这次要哪几段"。**L0 身份段永远保留**（不可关闭）：
# 身份不是"可选内容"，它是这个包的立身之本（设计如此，不是省事的默认）。
# 2026-11-11：新增 `profile`（主人画像，R1-6）。它与 L0 身份同档——
# 画像一旦被裁，注入包就退化成"一台不认人的机器"，所以默认在全段里。
INJECTION_SECTIONS = ("profile", "status", "board", "rules", "attention",
                      "lessons", "push", "index", "recent")
# 受控别名：调用方嘴上说的和内部键名不必一致，但**只认这张表**——
# 静默接受任意字符串会让"拼错的段名"变成"少了一段且没人报错"。
_SECTION_ALIASES = {"lesson": "lessons", "l2": "index", "rule": "rules",
                    "attentions": "attention", "indexes": "index"}
INJECTION_SECTIONS_NOTE = ("可取值：" + "/".join(INJECTION_SECTIONS)
                           + "；L0 身份段永远保留（不可关闭）")

# 包尾两行声明的最坏长度（用于预算预留）。**由 `injection_tail_worst()` 现算**，
# 不再手写一份字符串——2026-10-26 评审 🟠2 的第二半就是"手写常量与实际渲染不同步"：
# 尾句改成按段动态生成之后，手写常量必然落后（少了段名、少了 L2 那一档），
# 于是"预留 40 token"变成"实体 90 token"，包静默超预算。
#
# 归一纪律：**所有段都在、四类计数都取 4 位数**，那是这两行的最长形态；
# 真实包尾只会更短，所以"引用它来预留"永远是安全的方向。
_INJECTION_TAIL_WORST_AS_OF = "2026-10-26T00:00:00Z"
# 六类计数都必须非零：④该注意什么 / ⑤错题本 是"整段布尔"（它们没有条数可报），
# 但**只要被裁就要占字**——旧常量漏了这两项，也漏了"段名组合"这一维（见
# `injection_tail_worst`），于是"最坏 83 token"比真实最长 118 token 还短。
_INJECTION_TAIL_WORST_COUNTS = {"rules": 9999, "push": 9999, "board": 9999,
                                "l2": 1, "attention": 1, "lessons": 1}


def announce_truncation(counts) -> str:
    """包尾第一行：这次**砍了什么、砍了几条**（FB-02）。

    为什么非要有这一行：被截断的包与内容本来就少的包**长得一样**。读者据此
    以为"规矩就这 3 条"而不是"本来 7 条、省了 4 条"，于是漏掉的是他不知道自己
    缺的那部分。全 0 时写「本次无截断」——**"没有发生"也要说**，否则读者无法
    区分"没截断"和"没告诉我截没截断"。

    **2026-10-26 修（评审 🟠2-①）**：旧实现只统计 rules / push / board 三类，
    而 `meta["truncated"]` 里**有** `l2`。预算小到把 L2 整段裁掉时，包尾照样写
    「本次无截断」——自解释的那一行在说假话，而这正是它存在的唯一理由。
    两处必须同源（`truncation_counts` 一份计算），所以这里认 `l2` 且**只印非零项**：
    L2 是"整段有无"、没有条数，印成「L2 索引整段」比编一个数字诚实。

    **2026-10-26 修（复审 🟠N1-①）**：③④⑤ 里只有 ③规矩 有条数，④该注意什么 /
    ⑤错题本 也被预算整段裁过，却一个字都不报——实测 `sections=['attention','lessons']`
    + 小预算，两段都被裁掉，尾行照样写「本次无截断」。这两段同样是"整段有无"，
    所以与 L2 同一档处理：报「整段」，不编条数。
    """
    c = {k: int(counts.get(k) or 0)
         for k in ("rules", "push", "board", "l2", "attention", "lessons")}
    if not any(c.values()):
        return "本次无截断"
    parts = []
    if c["rules"]:
        parts.append(f"规矩 {c['rules']} 条")
    if c["push"]:
        parts.append(f"候选 {c['push']} 条")
    if c["board"]:
        parts.append(f"看板动态 {c['board']} 条")
    if c["attention"]:
        parts.append("该注意什么整段")
    if c["lessons"]:
        parts.append("错题本整段")
    if c["l2"]:
        parts.append("L2 索引整段")
    return "本次截断：" + " / ".join(parts)


# 段名 → 包尾"本包含 …"里给读者看的名字（顺序取 `INJECTION_SECTIONS` 的声明顺序）。
# L0 不在表里：它**永远在**（不可关闭），所以单独写、不参与拼接。
_TAIL_SECTION_LABELS = {
    "status": "L1 状态坐标",
    "board": "看板",
    "rules": "规矩",
    "attention": "该注意什么",
    "lessons": "错题本",
    "push": "候选",
    "index": "L2 索引",
    "recent": "最近三件事",
}
# 全段选择时的**旧措辞**：这句话自 2026-10-26 起被大量判据读过，且它描述的
# 正是"全段、什么都没丢"的那一种包——改它就等于改一件没坏的东西。
_TAIL_FULL_CONTAINS = "本包含 L0–L2 与候选"


def injection_contains_phrase(sections) -> str:
    """包尾第二行的「本包含 …」——**按这次真的要了哪几段**生成（评审 🟠2-②）。

    为什么要动态：旧实现写死「本包含 L0–L2 与候选」，于是 `sections=["status"]`
    的包里只有 L0+L1，尾行仍声称含 L2 与候选——读者据此以为"索引与候选都看过了、
    只是没有内容"，而真相是**这次没要它们**。自解释行说假话，比不说更坏。

    `sections=None` → 全段（旧行为，措辞也保持旧的那一句）。
    传进来的集合**应当是本包里真的非空的那几段**：预算把某段整段裁掉之后，
    "本包含某段"同样是假话（裁掉的段另有 `announce_truncation` 报出来，两行不冲突）。
    """
    if sections is None:
        return _TAIL_FULL_CONTAINS
    have = set(sections) & set(_TAIL_SECTION_LABELS)
    if have == set(INJECTION_SECTIONS):
        return _TAIL_FULL_CONTAINS
    ordered = [s for s in INJECTION_SECTIONS if s in have]
    if not ordered:
        return "本包只含 L0 身份（其余段本次未选或已被预算裁掉）"
    return "本包含 L0 身份 + " + "、".join(_TAIL_SECTION_LABELS[s] for s in ordered)


# I3（2026-11-18 W5）：**被裁掉的内容怎么要回来**。
#
# 为什么必须有这一行：包尾原本只说"砍了什么、砍了几条"（announce_truncation），
# 读者知道自己缺了东西，却**不知道去哪拿**——于是那段内容等于白白消失。
# headroom 的 CCR 把"可逆"做成一等公民正是这个道理；fish 不需要额外存储
# （原文本来就在库里），缺的只是这一句指路。
_RETRIEVAL_HINTS = (
    ("push", "候选", "`fish recall` / MCP `recall`"),
    ("l2", "L2索引", "MCP `list` / `recall`"),
    ("attention", "注意事项", "MCP `recall`"),
    ("lessons", "错题本", "MCP `recall`"),
    ("rules", "规矩", "MCP `recall`"),
    ("board", "看板", "`fish board`"),
)


def retrieval_hint(counts) -> str:
    """包尾第三行：把被裁的段映射成**可执行的取回入口**。

    没有任何段被裁时返回空串（不印"无需取回"这种噪音——包尾已经说了"本次无截断"）。
    """
    c = {k: int((counts or {}).get(k) or 0) for k, _n, _h in _RETRIEVAL_HINTS}
    parts = [f"{name}→{hint}" for key, name, hint in _RETRIEVAL_HINTS if c.get(key)]
    if not parts:
        return ""
    return "要回：" + "；".join(parts)


def injection_tail_text(counts, as_of, sections=None) -> str:
    """包尾两行：**这次砍了什么**（FB-02）+ **数据截至何时**（FB-16）。

    为什么两行都要：截断声明回答"我看到的完整吗"，as-of 回答"这些是几点的事实"。
    缺任一条，注入包都会退化成"一段看起来权威、但无法自证时效与完整性的文字"。

    `sections` 传的是**本包实际含有的段**（见 `injection_contains_phrase`）；
    `None` = 全段（保持旧措辞，既有判据不受影响）。
    """
    hint = retrieval_hint(counts)
    return (announce_truncation(counts) + "\n"
            + (hint + "\n" if hint else "")
            + f"as-of {as_of}（数据截至；{injection_contains_phrase(sections)}）")


def injection_tail_worst(as_of=None) -> str:
    """包尾两行在**最坏情况**下有多长（预算预留用）。

    最坏 = 六类计数都非零（三类 4 位数 + 三段整段标记）× 段名组合里**最长的**
    那一种。为什么要把 2^8 种组合都枚举一遍（2026-10-26 复审 🟡N3）：
      · 旧实现只算"全段都在"那**一种**，而全段都在会走**旧措辞**
        （`本包含 L0–L2 与候选`，恰好是最短的一句）；
      · 于是"预留 83 token"而真实最长 118 token——裁到 N 之后静默超预算，
        而"超预算"正是本函数所在的那段代码最该响亮报告的东西。
    枚举不是抽样：`injection_contains_phrase` 的取值空间就是这 257 种
    （256 个子集 + `sections=None` 的旧措辞），穷举才配叫"最坏"。

    比较按 `(字符数, token 数)` 取大：判据会按字符断言，预算按 token 记账，
    两个口径都要被同一份预留盖住（段名都是等长中文，两种口径的中位一致）。
    """
    moment = as_of or _INJECTION_TAIL_WORST_AS_OF
    worst = injection_tail_text(_INJECTION_TAIL_WORST_COUNTS, moment, sections=None)
    names = list(INJECTION_SECTIONS)
    for mask in range(1 << len(names)):
        subset = [n for i, n in enumerate(names) if (mask >> i) & 1]
        cand = injection_tail_text(_INJECTION_TAIL_WORST_COUNTS, moment, sections=subset)
        if (len(cand), count_tokens(cand)) > (len(worst), count_tokens(worst)):
            worst = cand
    return worst


def normalize_injection_sections(sections) -> set:
    """把 `sections` 入参归一成集合；`None` = 全段（也就是**旧行为**）。

    校验放在这里而不是"用了再说"：拼错的段名必须是**响亮失败**。
    静默忽略的表现是"包短了一截但没报错"——读者会以为那段本来就没内容。
    """
    if sections is None:
        return set(INJECTION_SECTIONS)
    if isinstance(sections, str):
        sections = [sections]
    out, unknown = set(), []
    for raw in sections:
        key = str(raw or "").strip().lower()
        key = _SECTION_ALIASES.get(key, key)
        if not key:
            continue
        if key in INJECTION_SECTIONS:
            out.add(key)
        else:
            unknown.append(str(raw))
    if unknown:
        raise ValueError(f"未知段：{', '.join(unknown)}（{INJECTION_SECTIONS_NOTE}）")
    return out


def normalize_inject_sections_arg(raw, include_board=None):
    """把 `sections` / `include_board` 归一成 `build_injection_pack` 的 `sections` 入参。

    返回 `None` = 全段（不传就不改变既有行为）；返回列表 = 只要这些段。
    **这是 MCP 与 CLI 的唯一实现**（2026-10-26 · 评审 🟠3）：MCP 的
    `_inject_sections_arg` 只是把 dict 拆开喂进来，CLI 的 `inject --sections/--no-board`
    喂同一条路。两个入口各写一份归一，必然出现"说不要看板、包里有看板"。

    为什么要归一在一个函数里：`sections=["status","board"]` 与 `include_board=false`
    可以同时出现，两处各自"再处理一下"必然出现自相矛盾的结果（比如包里还有看板）。
    `include_board` 是 `sections` 的便捷写法（`false` = 去掉 board），
    保留它是因为不少调用方只想"别看板"，不该被迫写 8 个段名。
    `include_board=true` 不往 `sections` 里加 board：它只是"没在排除"，
    与"显式要 board"不是同一句话。
    """
    if raw is None:
        if include_board is False:
            return [s for s in INJECTION_SECTIONS if s != "board"]
        return None
    if isinstance(raw, str):
        # CLI 形态：`--sections status,board` / `--sections "status board"`。
        # 逗号与空白都当分隔符（命令行里两种写法都自然），空片段丢掉。
        raw = [piece for piece in re.split(r"[,\s]+", raw) if piece]
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"sections 必须是数组（{INJECTION_SECTIONS_NOTE}）")
    # 归一化放在**边界**（这里）：拼错的段名要在调用时就响亮失败，
    # 而不是静默少一段——"包里短了一截但没报错"是最难查的那种坏。
    want = normalize_injection_sections(
        [str(s).strip() for s in raw if str(s or "").strip()])
    if include_board is False:
        want.discard("board")
    return sorted(want)

def build_injection_pack(conn, root, citizen_id, budget=None, project=None, cwd=None,
                         session_actor=None, sections=None, session_key=None):
    """生成「权威当下」：L0 身份 + L1 状态 + ③规矩 + ④该注意什么 + L2 索引，硬预算裁剪。

    返回 (pack_text, meta)。
    裁剪顺序（2026-10-24 起）：候选 → L2 → ④该注意什么 → ⑤错题本 → ③规矩
    → 看板 → L1 条目 → L0 铁律 → L1。
    为什么这个顺序：候选/索引最可弃（recall/索引命令随时补回）；注意事项是**临时的**，
    规矩是**长期的**——临时的比长期的更该让位；身份铁律与身份行最后才动（身份永不舍弃）。

    2026-10-26（FB-02 / FB-16）四处补充：
      · `sections`：只构建列出的段（`None` = 全段 = 旧行为），**L0 永不可关**；
      · 规矩段与看板段各自有上限（配置 `inject.rules_per_source` /
        `inject.board_max_ratio`），且**超了就声明**（段头 / 包尾两处都能看见）；
      · 包尾两行交代"这次砍了什么"与"数据截至什么时候"（`as_of`）；
      · `meta["truncated"]` 与那两行用**同一份计算**——声明与 meta 不一致比不声明更坏。

    `session_actor` 只影响 ④ 段的 C 类（"别处读过但你没看"要拿**本会话**去比）。
    缺省时按自报名环境变量（`FISH_SESSION_ACTOR`，其次 `FISH_ACTOR`）兜底，再不行
    由 S1 退回公民名——这是 P1-b 定过的会话标识优先级，本函数不发明第四种。
    它不是"账号"：拿账号当会话会把「同账号别的会话读过」算成「我读过了」，
    所以只在**没有更细证据**时才用它（将来 MCP 侧透传真实 `session_key` 后自然更准）。

    project / cwd 都是**可选**证据（R1 根治）：
      · project 显式声明当前项目（最高优先，如 MCP `inject` 的 project 入参）；
      · cwd 用于 workspace_map 的最长前缀匹配（调用方知道自己的工作目录时才给）；
      · 都不给时才按会话档期 → 最近更新兜底，且 meta 与 L1 都会标明来源。
    meta 里的 project / project_source / other_projects 就是这次判定的事实，
    调用方（MCP / CLI / 测试）不必再猜"这句话是怎么来的"。
    """
    budget = int(budget or get_config(conn, "inject.budget_tokens"))
    root_s = str(root)
    want = normalize_injection_sections(sections)

    # ── P1-2（2026-11-20）：cwd 缺失时按**会话键**补齐 ────────────────────
    # 为什么不给 `inject` 加 cwd 入参：那个工具的入参是**冻结契约**（三条门禁锁着
    # 「只允许新增可选 token」），加参数会一次性打红 test_corpus_search /
    # test_identity_p1c / test_push_p0。而 hook 早已把 cwd 落进 `session_context`
    # （P1-3），按 session_key 取回即可——调用方一个参数都不用多传。
    # 取不到就保持 None：下游照原样走「显式 → 档期 → workspace_map → 兜底」。
    if not cwd and session_key:
        cwd = fish_activity.session_cwd(conn, session_key) or None

    # ---- L0 身份（主人 / 行动者 必须分开）----
    # F-P2-2：**identity 记忆不进 L0、也不进注入包**（实测：12 条 identity，注入包 0 条），
    # 而设计文档说它该「L0 常驻」。**但它并不是读不到**——`search_memories` /
    # `list_memories` / `read_body_path` 走普通路径照样能读到它。所以准确说法是：
    # 「没有任何代码**按 identity 特殊处理**」，不是「没有读取路径」。
    #
    # 我上一版这里写的是「无读取路径」，那是**错的**：我用 grep 找"以 identity 为条件的
    # 查询"、找到 0 条，就把"没有专门查询"当成了"读不到"——正是验收纪律禁止的
    # 自建解析器代替实测。**真去 recall 一条就知道它读得到。**
    #
    # 接线需单独一轮：定 identity 与 citizens.display_name 的优先级 + L0 加段 + 判据。
    owner_id = resolve_owner(conn)
    actor = None
    if citizen_id:
        actor = conn.execute(
            "SELECT display_name,kind,role,host FROM citizens WHERE citizen_id=?",
            (citizen_id,)).fetchone()

    if actor:
        name, kind, role, host = actor
        # 措辞纪律：说「登记身份」而不是「你是」。
        # 「你是 X」会被 AI 理解成本体身份，与宿主身份（如 WorkBuddy）打架。
        #
        # 2026-10-25 补（T4）：把 host 印出来。
        #   `citizens.host` 本来就在上面那条 SELECT 里（一直是查出来的），只是没渲染，
        #   于是「同名两公民」在注入包里长得一模一样——生产库实测 `fish外部评估` 有两条，
        #   分别来自 ZCode 与 cli，谁是本会话只能去库里翻 host_session_key 才分得出。
        #   host 是身份的固有属性（"我从哪个宿主来"），不是新增字段，也不抬高证据级。
        host_s = (host or "").strip() or "未记录"
        ident = (f"登记身份：{name}（公民 {citizen_id}，形态 {kind}，"
                 f"角色 {role or '-'}，宿主 {host_s}）。\n"
                 "　这是你在 fish 的账户名（宿主级），与你的宿主身份不冲突。")
    elif citizen_id:
        ident = (f"身份待核：凭证解出的公民 ID 是 {citizen_id}，但库里查不到这个公民"
                 "（可能是历史遗留或凭证已失效）。先跑 `fish whoami` 核实。")
    else:
        # 2026-10-25 修（外部评估 `EVAL-261025-04`）：**绝不能说"你尚未登记身份"**。
        #
        # 无 token ≠ 没登记。最常见的真相是"这次调用漏了凭证"（会话刚重启、还没 start）。
        # 说成"没登记"会把 AI 引向重跑 `register` —— 然后反复拿到"已存在"，
        # 于是它得出"登记逻辑坏了"的错误结论。实测代价：白跑一轮 + 错误归因。
        #
        # 一条原则（`EVAL-261025-04` 的原话）：**提示语必须是可执行的下一步，
        # 而不是一个可能错误的归因。**
        ident = ("本次调用未带会话凭证，**无法确定归属**。"
                 "（这不等于你没登记——很可能只是漏了 token。）\n"
                 "　· 已登记过 → 跑 `fish session start --citizen <你的 citizen_id>` 拿 token；\n"
                 "　· 还没登记 → 先调 register，它会回执你的 citizen_id。")

    owner_line = ""
    if owner_id and owner_id != citizen_id:
        ro = conn.execute("SELECT display_name,kind FROM citizens WHERE citizen_id=?",
                          (owner_id,)).fetchone()
        if ro:
            # 注意：这里的 actor 是 (display_name, kind, role, host) 四列，
            # kind 在索引 1（不是 2——2 是 role）。索引错位会静默降级成"行动者"。
            actor_kind = actor[1] if actor else "unknown"
            word = {"visitor": "访客", "role": "角色成员",
                    "resident": "居民", "system": "系统进程"}.get(actor_kind, "行动者")
            owner_line = (f"你的主人是 {ro[0]}（公民 {owner_id}，形态 {ro[1]}）。"
                          f"你以{word}身份行动，不要自称主人。\n")

    l0_full = (ident + "\n" + owner_line
               + f"记忆根目录：{root_s}。可用 remember / recall / neighbors 操作它。\n"
               "三条铁律：\n"
               "1. 不确定的先查，不要凭记忆下结论。\n"
               "2. 编码由系统分配，不要自己编。\n"
               "3. 你的身份是系统签发的，不要根据记忆叙事推断自己是谁。\n")
    l0_min = (ident + "\n" + owner_line + f"记忆根目录：{root_s}。\n"
              "铁律：先查再答；编码由系统分配；身份由系统签发。\n")

    # ---- 主人画像（R1-6，2026-11-11）----
    # 位置：紧跟在 L0 身份之后。理由——身份回答「你是谁」，画像回答「你服务的这个人
    # 是谁」，两者合起来才是完整的"我在哪、为谁干活"。放到 L1/L2 之间会让"为谁干活"
    # 变成可裁内容，而它和身份一样是**立身之本**。
    #
    # 分级（R1-6 原话「L0 只放称呼 + 3 条最稳定字段，近期动态必须带时效标记」）逻辑
    # 全在 `fish_profile.render_profile_block` 里；这里只负责「有没有」与「接在哪」。
    # 空图像**连标题都不输出**——注入包里出现一个空壳段是噪音，也白花预算。
    #
    # 与裁剪链的关系：画像和铁律同档（都是"最后才动"），因为它一旦被裁，
    # 注入包就退化成"一台不认人的机器"。
    _profile_text = fish_profile.render_profile_block(conn) if "profile" in want else ""
    if _profile_text:
        l0_full = l0_full + "\n" + _profile_text + "\n"
        l0_min = l0_min + _profile_text + "\n"

    # ---- 画像提问（R1-4，2026-11-18 接线）----
    #
    # **这个函数写了一周没接线**：`render_questions_block` 11-11 就实现了、也有单测，
    # 但从来没人从 `build_injection_pack` 调它 → 注入包里永远没有提问段。
    # 实测证据：`profile.docs_since_ask` 攒到 **73**（阈值 10），
    # 即"该问了"这个状态成立了 63 次，一次都没出现在包面上。
    #
    # 教训写在这里：**判据测了零件、没测装配**。单测 `render_questions_block()`
    # 返回正确内容，不等于注入包里会出现它。所以补的判据必须查**真实的包文本**。
    _ask_text = fish_profile.render_questions_block(conn) if "profile" in want else ""
    if _ask_text:
        l0_full = l0_full + "\n" + _ask_text + "\n"
        l0_min = l0_min + _ask_text + "\n"

    # ---- L1 状态 ----
    # 2026-10-24 修（EVAL 外部评估 R1 + 自查），同一处两个问题：
    #   ① 当前项目不再靠"谁最近更新"猜：改走四档证据链（_resolve_injection_project），
    #      没有证据时明说"未声明"，只剩兜底时标注"据最近更新推断"；
    #   ② 状态口径：L1/L2 的四处计数与取数都必须只算 status='active'——原本没筛，导致
    #      「活跃 N 条」虚高（实测 134 vs 真实 104）、「最近三件事」推出已替代/已作废的
    #      条目（实测 TESTP/VIDEO 各中 1 条）、「今日编码」列出已作废编码、「休眠 N 条」
    #      把作废且休眠的也算进去。
    # 注入包是**每个会话开场**都用的路径，错误信息的杠杆最大。
    proj_name, proj_source, other_projects = _resolve_injection_project(
        conn, citizen_id, project=project, cwd=cwd)
    recent = []
    if proj_name:
        active_n, last_seen = _project_activity(conn, proj_name)
        l1_head = _render_project_head(proj_name, active_n, last_seen, proj_source)
        # F-P1-X（第二轮评审 🟠-2）：注入包的"最近三件事"也必须按可见性过滤。
        # 这是**最要紧的一处**：它每个会话开场都跑，不过滤等于把别人的私有标题
        # 直接印进所有人的上下文——比 search 泄露严重，因为它不需要谁去搜。
        _vis_sql, _vis_params = _visibility_sql(conn, citizen_id)
        recent = conn.execute(
            "SELECT code,title FROM code_index WHERE project=? AND dormant=0 "
            "AND status='active'" + _vis_sql + " ORDER BY code DESC LIMIT 3",
            [proj_name] + _vis_params).fetchall()
    else:
        l1_head = _UNDECLARED_PROJECT_LINE
    dormant_n = conn.execute(
        "SELECT COUNT(*) FROM code_index WHERE dormant=1 AND status='active'").fetchone()[0]
    # P3-3（2026-11-20）：仓库行。判不到返回空串——**空就不出现**，不写噪音。
    repo_line = _render_repo_line(conn, cwd)

    def render_l1():
        # `status` = 坐标（当前项目 / 当前仓库 / 同时在其他项目 / 休眠计数）；
        # `recent` = L1 的"最近三件事"条目。两段分开选的理由：坐标是"我在哪"，
        # 条目是"这里有什么"——后者才是可省的那一半（要内容可以 recall）。
        lines = []
        if "status" in want:
            if l1_head:
                lines.append(l1_head)
            # P3-3：仓库行紧跟在项目行后面——两者都是"我在哪"的坐标
            if repo_line:
                lines.append(repo_line)
            if other_projects:
                lines.append("同时在其他项目：" + " / ".join(other_projects))
        if "recent" in want and recent:
            lines.append("最近三件事：")
            lines += [f"- {c} {t}" for c, t in recent]
        if "status" in want and dormant_n:
            lines.append(f"另有 {dormant_n} 条已休眠（可 recall 唤醒）")
        return ("\n".join(lines) + "\n") if lines else ""

    # ---- L2 索引 ----
    # 2026-10-25 · P1-a·A 批（研判见 `31-P1a-推送系统-开发方案` §2）。原 L2 占整包
    # 29%（400/1371 tokens），但三项里有两项是**坐标不是信息**：
    #   「今日编码」只印编码不印标题——AI 看到 ANYI-261025-01 必须再查一次才知道是什么；
    #                而它要查就会查，不查则列了也没用（实测当日 19 条只印前 6 条）。
    #   「常用标签」给了名字却不能用（查要 recall --tags），等于给了个用不了的入口。
    # 故两项删除。项目列表改为**只列有 active 记忆的**（原样列 14 个，其中 3 个 0 条），
    # 且只给计数不给 namespace——namespace 是给人看的，AI 靠计数判断"有没有东西可查"。
    # 保留「衰减参数」与「操作指引」：两者都有真实事故依据
    # （`04` §3.3 文档抄错天数导致 AI 报错半衰期 / `12` §1.1 实测绕路率 10.5%）。
    proj_rows = conn.execute(
        "SELECT r.prefix, COUNT(c.code) AS n FROM code_registry r "
        "JOIN code_index c ON c.project = r.prefix AND c.status = 'active' "
        "WHERE r.status = 'active' GROUP BY r.prefix "
        "ORDER BY n DESC, r.prefix").fetchall()
    l2_lines = []
    if proj_rows:
        l2_lines.append("可查项目：" + " ".join(f"{p}({n})" for p, n in proj_rows))
    # 生效衰减参数：让 AI 不必去读可能过期的文档
    dcfg = load_decay_cfg(conn)
    l2_lines.append("衰减(天)：" + " / ".join(
        f"{t}={int(dcfg.get('decay.' + t + '_days', 0))}"
        for t in ("episode", "fact", "lesson", "artifact")))
    # 入口指引（第 1 层引导）：把 AI 引到 MCP 工具，而不是读文件/查 SQLite
    l2_lines.append("【操作指引】记→remember ｜ 查→recall/resolve_code ｜ "
                    "上下文→neighbors ｜ 状态→inject（勿直接读写 data/ 或 sqlite）")
    # 知识库提示（2026-10-30）：把 KB 的存在告诉 AI——否则没人会想到去查
    # （生产读数：`kb_access` 2376 条里 99.9% 是 10-26/27 建库自测，真实业务查询≈0）。
    #
    # 设计纪律（开源通用性，三条都是硬要求）：
    #   · 篇数 / 目录名 **全部从 kb_index 实时聚合**：不写死任何数字或目录名，
    #     换一个库（别人的 Obsidian / 别的语言目录）自动适配；
    #   · 空库、或 kb_index 表不存在（老库 / 别的部署）→ **不追加**：
    #     不占预算，也不制造"暂无知识库"这种噪音（噪音会训练 AI 跳过这一段）；
    #   · 只交代"有什么 / 怎么查 / 别编造"，**不描述具体内容**——内容用 search 取，
    #     写进注入包就等于把过期摘要固化。
    #
    # 性能（2026-10-30 实测 296 行库）：走 ix_kb_index_status 索引，
    # 中位 0.005 ms（20 次），比同段 proj_rows 快约 100 倍。仍加两道保险：
    # ① 只在选了 `index` 段时才查（不选就不付代价）；② 开关 inject.kb_hint_enabled。
    if "index" in want:
        try:
            _kb_on = int(get_config(conn, "inject.kb_hint_enabled"))
        except (KeyError, TypeError, ValueError):
            _kb_on = 1
        if _kb_on:
            try:
                _kb_rows = conn.execute(
                    "SELECT CASE WHEN instr(rel_path,'/') > 0 "
                    "            THEN substr(rel_path,1,instr(rel_path,'/')-1) "
                    "            ELSE '(root)' END AS top, COUNT(*) AS n "
                    "FROM kb_index WHERE status = 'active' GROUP BY top ORDER BY n DESC"
                ).fetchall()
            except sqlite3.Error:
                _kb_rows = []   # 表不存在 / 列不匹配 → 静默跳过（不因 KB 拖垮注入）
            if _kb_rows:
                _kb_total = sum(r[1] for r in _kb_rows)
                _kb_parts = "、".join(f"{r[0]} {r[1]}" for r in _kb_rows[:5])
                l2_lines.append(
                    f"知识库 {_kb_total} 篇（{_kb_parts}）：需要背景资料时用 kb search 查，"
                    "不要凭印象编造。")
    l2 = ("\n".join(l2_lines) + "\n") if l2_lines else ""
    l2_built = bool(l2)         # 供"L2 是被裁掉还是本来就没有"的声明区分（见 truncation_counts）
    if "index" not in want:
        # L2 是**整段**可弃（没有"条目"可部分保留）：预算裁剪链里它也是第一个被丢的。
        l2 = ""

    # ---- ③ 规矩 / ④ 该注意什么 / ⑤ 错题本 ----
    # 三段都可能为空——**空就不出现**，不写"暂无规矩"这种噪音（噪音会训练 AI 跳过这一段）。
    # session_actor 的兜底顺序与 S1 一致：显式入参 > 自报 env > 公民名（由 S1 解析）。
    # 第五轮评审 🔴：**装配层必须传 viewer**，否则开关打开后
    # 主人自己的私有规矩/教训在注入包里看不见，而段头还把私有的算进 N/M。
    rules = build_short_rules_block(conn, project=proj_name,
                                    viewer_citizen_id=citizen_id)
    attention = build_attention_block(
        conn, root, citizen_id=citizen_id, project=proj_name,
        session_actor=(session_actor or "").strip() or env_session_alias())
    # 段⑤ 错题本（2026-10-25 · T9）：从段④拆出，理由是 rule（该怎么做）与
    # lesson（别再这样）性质不同，混在一段里 AI 分不清"通用教训"还是"本项目的事"。
    # `exclude` 传段④已印过的编码——一条记忆只在一段里出现，否则同一件事占两段预算。
    #
    # ⚠️ 2026-10-25 试过再加一条：把 L1「最近三件事」也排除掉，以消除用户看到的重复
    # （`EVAL-261025-08` 同时在 L1 与错题本）。**试了两次都退回来了**，原因是结构性的：
    #   · 全排除 → 错题本被挖空（判据 B2/B'/H/L2 五条红）；
    #   · 空了再兜底补 → 跨项目 lesson 顶替同项目 lesson（判据 B' 红，破坏"同项目优先"）。
    # 根本原因：**L1 的"最近三件事"与⑤错题本同源**（都是"最近窗口内的 lesson/fact"），
    # 而 L1 在 ③④⑤ 之前算，⑤ 想排除 L1 就得回头改已渲染的 L1 —— 顺序上不允许。
    # 结论：这个重复**保留**（约 60 token），因为去掉它要动"同项目优先"或"错题本完整性"，
    # 而那两条比 60 token 硬得多。如实记在这里，不留在代码里当"看起来修好了"。
    lesson = build_lesson_block(conn, project=proj_name,
                                exclude=set(attention["codes"]),
                                viewer_citizen_id=citizen_id)
    # 段⑥ 候选（2026-10-25 · P1-a，`16` §5）：四类候选 + 打分 + 防静默。
    # exclude 传 ③④⑤ 已印的编码——一条记忆只在一段里出现（与上面同一纪律）。
    # 为什么排在这三段之后：候选要"补它们没覆盖的"，不是重复它们。
    push = build_push_block(
        conn, root, project=proj_name, citizen_id=citizen_id,
        session_actor=(session_actor or "").strip() or env_session_alias(),
        # 只排除**这次真的会展示**的段：`sections` 只要了 push 时，③④⑤ 不进包，
        # 若仍按它们排除，等于把"没展示的记忆"当成"已展示"——那些条目就此消失，
        # 且没有任何一处会说出来。排除的语义是"别重复展示"，不是"别看"。
        exclude=((set(rules["codes"]) if "rules" in want else set())
                 | (set(attention["codes"]) if "attention" in want else set())
                 | (set(lesson["codes"]) if "lessons" in want else set())))
    # ---- 段选择（FB-02）----
    # 只在这**一处**生效：前面各段照常构建，装配点按 `want` 统一置空。
    # 为什么不提前短路：段与段之间有依赖（候选的 `exclude` 要拿 ③④⑤ 已印的编码），
    # 提前 return 会让"排除了谁"取决于本次选了哪些段——同一份数据在不同 `sections`
    # 下推出不同候选，那是比"多算几毫秒"贵得多的错误。
    rules_text = rules["text"] if "rules" in want else ""
    attention_text = attention["text"] if "attention" in want else ""
    lesson_text = lesson["text"] if "lessons" in want else ""
    push_text = push["text"] if "push" in want else ""

    # ---- 组装 + 裁剪 ----
    # 裁剪顺序（2026-10-25 起）：**候选** → L2 → ④该注意什么 → ⑤错题本 → ③规矩
    #   → L1 条目 → L0 铁律 → L1。
    # 候选排在最前：它是"可能对你有用"，不是"你必须知道"——预算紧张时它第一个让位。
    # 其次才是索引（recall 随时补回）；④临时信号 < ⑤长期教训 < ③长期规范。
    # 身份铁律与身份行最后才动（身份永不舍弃）。
    trimmed = (list(rules["trimmed"]) + list(attention["trimmed"])
               + list(lesson["trimmed"]))

    # ---- 看板段（2026-10-25 · P1-b D 批）----
    # 位置纪律：紧跟 L1（坐标）之后、③规矩之前——**它是"当前态势"，与坐标同属"此刻"**。
    # 只给最近一级看板（项目级优先）：三级全推会重复（`09` R5-6）。
    #
    # 2026-10-26（FB-02）：本段现在有自己的上限 = 整包预算 × `inject.board_max_ratio`
    # （默认 0.25）。旧实现无上限，看板一长就只能整段丢，而整段丢的代价是
    # "时效性最强的那块直接消失"——比"少几行动态"贵得多。
    board_ratio = get_config(conn, "inject.board_max_ratio")
    try:
        board_ratio = min(max(float(board_ratio), 0.0), 1.0)
    except (TypeError, ValueError):
        board_ratio = BOARD_MAX_RATIO
    board_stats = {}
    board_text = board_block(conn, proj_name, citizen_id=citizen_id,
                             session_key=session_key,
                             max_tokens=int(budget * board_ratio),
                             stats=board_stats) or ""
    if "board" not in want:
        board_text = ""
    # 包尾两行声明的预算预留（FB-02 / FB-16）：包尾是**新增内容**，必须算进预算，
    # 否则"裁到 900"之后再追加 40 token，包就悄悄超了——而"超了"正是本函数最该
    # 响亮报告的东西。按**最坏情况**估（四类计数都写成 4 位数、全段都列进来），
    # 比实际长几 token 是安全的方向：宁可少留一点内容，不许超预算。
    # 2026-10-26（评审 🟠2-③）：最坏长度改为**现算** `injection_tail_worst()`——
    # 尾句改成按段动态生成后，手写常量与实体渲染必然不同步，预留会失效且无人报错。
    budget_orig, budget = budget, max(1, budget - count_tokens(injection_tail_worst()))

    def total():
        return (count_tokens(l0) + count_tokens(l1) + count_tokens(rules_text)
                + count_tokens(attention_text) + count_tokens(lesson_text)
                + count_tokens(push_text) + count_tokens(l2) + count_tokens(board_text))

    def truncation_counts():
        """各段"被省略多少条"——包尾声明与 `meta["truncated"]` **共用这一份**计算。

        为什么不各算一份：声明说"候选 3 条"而 meta 说 2 条，比不声明更坏
        （读者不知道该信哪个，于是两个都不信）。
        段被整段裁掉时算"它本来有多少"：那才是"你没看到的部分"的诚实大小。
        """
        rules_x = 0
        if "rules" in want:
            rules_printed = len(rules["codes"]) if rules_text else 0
            rules_x = max(0, int(rules.get("total") or 0) - rules_printed)
        push_y = 0
        if "push" in want:
            push_total = len(push["candidates"]) + int(push.get("omitted") or 0)
            push_y = max(0, push_total - (len(push["candidates"]) if push_text else 0))
        board_z = 0
        if "board" in want:
            board_z = int(board_stats.get("truncated") or 0)
            if not board_text:
                board_z = max(board_z, int(board_stats.get("lines") or 0))
        # L2 没有"条目"（2026-10-25 起它只给计数与入口），所以只能按整段记 0/1。
        # 只有"本来有、被裁掉了"才算 1——本来就没有（`l2_built` 为假）不是裁剪。
        l2_w = 1 if (l2_built and not l2 and "index" in want) else 0
        # ④⑤ 与 L2 同一档（2026-10-26 复审 🟠N1-①）：这两段没有"条数"可报，
        # 但"本来有、被预算整段裁掉"必须算截断——否则尾行会在包里明明缺两段时
        # 说「本次无截断」，而这一行存在的唯一理由就是回答"我看到的完整吗"。
        attention_w = 1 if ("attention" in want and attention["text"]
                            and not attention_text) else 0
        lesson_w = 1 if ("lessons" in want and lesson["text"]
                         and not lesson_text) else 0
        return {"rules": rules_x, "push": push_y, "board": board_z, "l2": l2_w,
                "attention": attention_w, "lessons": lesson_w}

    # 注入风格决定开头（可配置：gentle / strong）
    style = get_config(conn, "inject.style")
    if style == "strong":
        header = "【必读 · 当前状态】回答任何问题前，先参考以下状态。\n"
    else:
        header = "【当前状态 · 大肥鱼生命系统注入】\n"

    l0, l1 = header + l0_full, render_l1()
    while total() > budget and push_text:
        push_text = ""
        trimmed.append("候选")
    while total() > budget and l2:
        l2 = ""
        trimmed.append("L2")
    # 看板在裁剪链里**排在注意事项之后**（2026-10-25 改 · `EVAL-261025-11`）。
    #
    # 原文把它排在第 3 位（push → L2 → 看板），理由是"没有它 AI 照样能工作"。
    # 但那个理由与工具描述**自相矛盾**——描述里写的是「开工前先 checkin，
    # 是协作协议，不是礼貌」。一边说它是协议，一边在预算紧时第一个裁掉它，
    # 等于承认它不是协议。
    #
    # 更硬的理由：**它只值 150-200 token**（`board_block` 的体积），
    # 裁它省不下多少，却裁掉了**时效性最强**的那块（"此刻谁在动哪块"）。
    # 而注意事项/错题本/规矩都可以离线查回来（recall / 注入包下一次就有）。
    #
    # 2026-10-25 再修（`EVAL-261025-15`）：上一版把注释改成了上面这段话，
    # 但代码顺序仍是 `注意事项 → 看板 → 错题本 → 规矩`——**注释说"看板该比这三者
    # 都活得久"，代码却只比注意事项久**。同一份文件里两种依据，那是新功能在注释里
    # 先欠一笔。现在按注释调齐，**最终顺序**：
    #   候选 → L2 → 注意事项 → 错题本 → 规矩 → 看板 → L1条目 → L0铁律
    # 依据：**可离线重建的让位于不可重建的**。看板是"此刻"，过了就查不回来；
    # 错题本/规矩 recall 一下就有。
    while total() > budget and attention_text:
        attention_text = ""
        trimmed.append("注意事项")
    while total() > budget and lesson_text:
        lesson_text = ""
        trimmed.append("错题本")
    while total() > budget and rules_text:
        rules_text = ""
        trimmed.append("规矩")
    # 看板放在这三者**之后**（最后被裁）——见上
    while total() > budget and board_text:
        board_text = ""
        trimmed.append("看板")
    while total() > budget and len(recent) > 1:
        recent = recent[:-1]
        l1 = render_l1()
        trimmed.append("L1条目")
    if total() > budget and l0 == header + l0_full:
        # 裁剪 L0 时保留 header（它是风格的载体）
        l0 = header + l0_min
        trimmed.append("L0铁律")
    if total() > budget and l1:
        l1 = ""
        trimmed.append("L1")

    # ---- 包尾声明（FB-02 截断 / FB-16 as-of）----
    # 放在 `strip()` 之后的**最后两行**：MCP 会在它之后追加 `<!-- tokens/budget -->`，
    # 所以这两行天然是"包正文的结尾"——读者读到最后一句话就能知道
    # "这份包完不完整、截至什么时候"。
    as_of = now_utc()
    truncation = truncation_counts()
    # 2026-10-26（复审 🟠N1-②）：尾句「本包含 …」必须按**这个包里真的有什么**生成，
    # 不能按"本次要了哪几段"（旧实现传的是 `sorted(want)`）。实测反例：
    # `sections=['attention','lessons']` + 小预算 → 两段都被裁掉（`trimmed` 里明明
    # 写着），尾句仍声称含「该注意什么、错题本」——读者据此以为那两段是空的，
    # 而真相是它们被预算拿走了。自解释行说假话，比不说更坏。
    #
    # 判"在不在"用**渲染结果**（空串 = 不在），不用"有没有被裁"：
    #   · 被预算裁掉的 → 渲染结果为空 → 不在，尾句不提（另有 `announce_truncation` 报）；
    #   · 本来就没有内容 → 同样不在，尾句也不提（它确实不在包里，这是事实不是缺陷）。
    delivered = set()
    if "status" in want and l1:
        delivered.add("status")
    if "recent" in want and recent and l1:
        delivered.add("recent")
    for _key, _text in (("board", board_text), ("rules", rules_text),
                        ("attention", attention_text), ("lessons", lesson_text),
                        ("push", push_text), ("index", l2)):
        if _text:
            delivered.add(_key)
    # 全段请求 + 预算一个段都没裁 → 沿用旧措辞（`本包含 L0–L2 与候选`）。
    # 为什么留这个例外：那句旧措辞说在**范围**上（要了全段、机器没拿走任何一段），
    # 而且它是 2026-10-26 之前所有判据读的那一句——改它就等于改一件没坏的东西。
    # 一旦有段被预算拿掉，范围就不再成立，尾句必须收紧到实际交付的段。
    _budget_cut = any(t in trimmed for t in ("候选", "L2", "注意事项", "错题本",
                                             "规矩", "看板", "L1条目", "L0铁律", "L1"))
    if not _budget_cut and want == set(INJECTION_SECTIONS):
        tail_sections = None
    else:
        tail_sections = sorted(delivered)
    tail = injection_tail_text(truncation, as_of, sections=tail_sections)
    body = (l0 + ("\n" + l1 if l1 else "")
            + ("\n" + board_text if board_text else "")
            + ("\n" + rules_text if rules_text else "")
            + ("\n" + attention_text if attention_text else "")
            + ("\n" + lesson_text if lesson_text else "")
            + ("\n" + push_text if push_text else "")
            + ("\n" + l2 if l2 else "")).strip()
    pack = (body + "\n" + tail).strip() + "\n"
    # 超预算的判据用**调用方给的**预算（`budget_orig`）：包尾预留只是内部记账，
    # 不该让"900 的预算"在外面看起来像"870"。
    final = count_tokens(pack)
    over_budget = final > budget_orig
    if over_budget:
        # 外部审查 🔴2（2026-10-28 晚）：旧实现把"仍超预算"只写进 `meta.trimmed`，
        # 而尾行在它**之前**就生成了 → **包尾永远看不到这一行**（读者会以为包是完整的）。
        # 修法：超支时把声明**追加进包正文**并重算 final（meta 与正文同源同值）。
        trimmed.append(f"仍超预算({final}>{budget_orig})")
        pack = (pack.rstrip("\n")
                + f"\n（仍超预算：{final}>{budget_orig} tokens；最小包已无可裁段）\n")
        final = count_tokens(pack)

    meta = {
        "tokens": final,
        "budget": budget_orig,
        "over_budget": over_budget,
        "layers": {"L0": count_tokens(l0), "L1": count_tokens(l1),
                   "board": count_tokens(board_text),
                   "rules": count_tokens(rules_text),
                   "attention": count_tokens(attention_text),
                   "lesson": count_tokens(lesson_text),
                   "push": count_tokens(push_text),
                   "L2": count_tokens(l2)},
        "trimmed": trimmed,
        # 2026-10-26（FB-02）：各段**被省略的条数**，与包尾那行声明同源同值。
        # `l2` 只可能是 0/1（整段有无），因为 L2 没有"条目"这个概念；
        # `attention` / `lessons` 同样是 0/1（复审 🟠N1-①：这两段也没有"条数"
        # 可报，只有"整段还在不在"）——把 0/1 当"条数"读会得出"裁掉了 1 条"，
        # 所以键名与口径都写在这里，调用方不必猜。
        "truncated": truncation,
        # 2026-10-26（FB-16）：本包的数据截止时刻（= 构建时刻，`now_utc()`）。
        # 注入包是"当下"的快照，没有时刻就无法判断它是不是过期了——
        # 与包尾 `as-of` 行同值（同一变量，不做二次取值，避免跨秒漂移）。
        "as_of": as_of,
        # 本次要了哪几段（`sections=None` 时是全段）。调用方据此判断
        # "某段为空"是"没有内容"还是"这次没要它"——两者在文本上长得一样。
        "sections": sorted(want),
        # L1「当前项目」的判定事实（R1 根治）：名字 / 来源 / 同时服务的其他项目。
        # source='inferred' = 走了"最近更新"兜底（输出里已标注这是推断）；
        # source='undeclared' = 没有任何证据（输出里明说未声明）。调用方不得再"补成"
        # 某个项目——那等于把本轮刚拆掉的猜测重新装回去。
        "project": proj_name,
        "project_source": proj_source,
        "other_projects": list(other_projects),
        # 供调用方刷新衰减：注入包展示过的记忆 = 被看见。
        # 没要 `recent` 段时为空——"没展示"不该被记成"被看见"（那会让采纳率虚高）。
        "recent_codes": [c for c, _ in recent] if "recent" in want else [],
        # L2 自 2026-10-25 起**不再展示具体编码**（研判见 `31` §2：编码是坐标不是信息），
        # 故这里恒为空。保留这个键是 meta 契约的一部分（调用方按固定形状读）。
        # 这不是"少记了一层"：原来 L2 印的只是编码、没印标题，AI 并未真看见内容；
        # 把它们算进 push_log 会让"采纳率"的分母虚高。
        "index_codes": [],
        # 段⑥ 候选（2026-10-25 · P1-a）：每条带 kind / score / reason。
        # 为什么 meta 要报候选而不只是编码：度量要看"推了什么类、打了多少分"，
        # 只报编码就算不出「哪类候选真被用上」——那是 `16` §5.5 的门控指标。
        "push_candidates": list(push["candidates"]) if push_text else [],
        # 段③/④ 里**真正印进包里**的编码（该段被预算裁掉时为空）。
        # 与 index_codes 同一纪律：这两段展示过的记忆也算"被看见"，push_log 要能记到。
        "rules_codes": list(rules["codes"]) if rules_text else [],
        "attention_codes": list(attention["codes"]) if attention_text else [],
        # 段⑤ 错题本里**真正印进包里**的编码（段被预算裁掉时为空）。与上面同一纪律：
        # 展示过就算"被看见"，push_log 要能记到；否则新拆出的这段永远算不出采纳率。
        "lesson_codes": list(lesson["codes"]) if lesson_text else [],
    }
    return pack, meta
# ------------------------------------------------------------------ 导出面
# `_IMPORTED` = 顶层 import 进来的名字（生成/校验 __all__ 时剔除）；常量不列入 __all__，
# 由 fish.py 另行 import 再导出。
_IMPORTED = frozenset({
    "datetime", "json", "re", "sqlite3", "Path",
    "fish_activity", "fish_git_store", "fish_profile", "fish_surface",
    "ATTENTION_OPEN_WINDOW_DAYS", "ATTENTION_TITLE_MAX_CHARS", "CONFIG_DEFAULTS",
    "_clip_chars", "_open_loop_predicate", "_open_loop_rows", "count_tokens",
    "get_config", "load_decay_cfg", "now_utc", "parse_utc", "resolve_owner",
    "resolve_project_names", "warn_ignored",
    "_recent_lesson_rows", "_rule_rows", "count_lesson_rows", "count_rule_rows",
    "build_push_block", "AUDIT_DIR_NAME", "FALLBACK_PROJECT_NAME",
    "current_open_stints", "env_session_alias", "find_citizen_by_name",
    "load_citizen", "resolve_project_by_cwd", "_visibility_sql", "board_block",
    "_IMPORTED",
})
__all__ = [
    "normalize_tool_name",
    "_parse_iso_utc",
    "_audit_ts",
    "_audit_in_window",
    "read_tool_call_audit",
    "_session_label",
    "_iter_tool_calls",
    "_citizen_by_value",
    "_citizen_aliases",
    "_display_name_of",
    "_citizen_id_of",
    "_audit_actor_values",
    "_resolve_profile_session",
    "_project_scope",
    "_label_in_project",
    "_candidate_scopes",
    "_record_in_scope",
    "_tool_usage",
    "_peer_tool_counts",
    "_tool_blind_spots",
    "_access_scope_where",
    "_peer_access_count",
    "_knowledge_gap_rows",
    "_titles_of",
    "_knowledge_gaps",
    "_access_log_total",
    "compute_session_profile",
    "fallback_project",
    "_project_activity",
    "_resolve_injection_project",
    "_render_repo_line",
    "_render_project_head",
    "_clamp_rules_per_source",
    "build_short_rules_block",
    "count_open_loop_rows",
    "_attention_gaps",
    "build_attention_block",
    "build_lesson_block",
    "announce_truncation",
    "injection_contains_phrase",
    "retrieval_hint",
    "injection_tail_text",
    "injection_tail_worst",
    "normalize_injection_sections",
    "normalize_inject_sections_arg",
    "build_injection_pack",
]
