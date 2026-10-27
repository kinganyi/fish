#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送管线（T02 拆包，2026-10-27）：候选生成/选择/rerank/回填。

为什么独立成模块：它是 `fish.py` 里最热、最容易长的一个领域（候选六类 + 选择 +
提示词时机 + 工具后时机 + 回填）；拆出来之后 fish.py 只留装配与 re-export。
依赖方向：fish_push → fish_common（**不 import fish**，判据 J2）。
"""

import datetime
import os
import re
import sqlite3
import time

import fish_activity
import fish_corpus
import fish_naming
import fish_surface

from fish_push_rerank import (       # T03b-R2：阶段二（重排）整段搬走的模块
    CTX_PROJECT,
    RERANK_TOP_K_DEFAULT,
    _breaker,
    _cfg_any,
    _rerank_enabled,
    _tag_kind_class,
    rerank_candidates,
)

from fish_kb import (  # T02：拆包后的跨模块引用
    _kb_search_multi,
    count_tokens,
    kb_vault_list,
    now_utc,
)

from fish_surface import (  # T02：拆包后的跨模块引用
    normalize_surface,
)

from fish_common import (
    ATTENTION_OPEN_KEYWORDS,
    ATTENTION_OPEN_WINDOW_DAYS,
    ATTENTION_TITLE_MAX_CHARS,
    CONFIG_DEFAULTS,
    _ALIAS_KINDS,
    _ALIAS_MAX_HOPS,
    _DEFAULT_DECAY,
    _alias_map,
    _board_seen_columns,
    _clip_chars,
    _index_one_fts,
    _key_type,
    _open_loop_predicate,
    _open_loop_rows,
    _presence_columns,
    _presence_has_session,
    _utc_str,
    _walk_alias,
    board_period,
    clean_prompt,
    compute_decay,
    ensure_fts,
    get_config,
    load_decay_cfg,
    local_date,
    parse_utc,
    push_outcome_window_hours,
    reindex_fts,
    resolve_name,
    resolve_project_names,
)


def mark_push_accepted(conn, codes, *, session_key=None, window_hours=None,
                       reacted_at=None) -> int:
    """回填"这次推送被接受了"（读了正文 / 校验了编码），返回更新的行数。

    四个条件（13 §2.1 / 16 §5.6）：**同一会话**、**同一编码**、**窗口内**、**尚未判过**。
    前三条一条都不能省：
      · 编码 —— 推过 A 却读了 B，不能算 A 被接受
      · 会话 —— 另一个会话里读的，不证明"推给这个会话"有效
      · 窗口 —— 25 小时前推的、今天才读，中间隔了一天，算不到这次推送头上
    窗口内**所有**待判行一起置 accepted：同一编码被推过两次、只读了一次，
    两次推送都该算"被回应"，否则先那次永远悬着（而它确实是同一批里被接受的）。

    window_hours<=0 视为关闭回填（与 push.outcome_window_hours=0 的约定一致）。
    """
    if isinstance(codes, str):
        codes = [codes]
    codes = [c for c in (codes or []) if c]
    if not codes:
        return 0
    window = push_outcome_window_hours(conn) if window_hours is None else int(window_hours)
    if window <= 0:
        return 0                             # 关闭判定：不猜任何反应
    ts = reacted_at or now_utc()
    now_dt = parse_utc(ts)
    if now_dt is None:
        raise ValueError(f"reacted_at 必须是 ISO8601 UTC（如 2026-10-24T01:00:00Z），收到 {ts!r}")
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    for code in codes:
        rows = conn.execute(
            "SELECT id,pushed_at FROM push_log WHERE code=? AND session_key IS ? "
            "AND reaction IS NULL AND pushed_at >= ?",
            (code, session_key, cutoff)).fetchall()
        for pid, pushed_at in rows:
            pdt = parse_utc(pushed_at)
            if pdt is None:                  # 历史坏行：不猜延迟，但反应照样记账
                conn.execute(
                    "UPDATE push_log SET reaction='accepted', reacted_at=? WHERE id=?",
                    (ts, pid))
            else:
                latency = max(int((now_dt - pdt).total_seconds()), 0)
                conn.execute(
                    "UPDATE push_log SET reaction='accepted', reacted_at=?, latency_sec=? "
                    "WHERE id=?", (ts, latency, pid))
            updated += 1
    conn.commit()
    return updated

def mark_push_accepted_prefix(conn, prefix, *, session_key=None, window_hours=None,
                              reacted_at=None) -> int:
    """按**编码前缀**回填 accepted（`board:` / `kb:` 这类非记忆候选的唯一回填口）。

    ## 为什么需要它

    `mark_push_accepted` 按**精确编码**回填记忆推送；但看板/知识库候选的 `code`
    是 `board:<label>` / `kb:<vault>:<rel>`——AI "看了看板"或"读了知识库文件"时
    没有对应的记忆编码可匹配，于是这三类推送永远等不到 accepted，只能在窗口后
    被判 ignored。度量上看起来像"从来没被采纳"，这是假象。

    ## 四条条件与 `mark_push_accepted` 相同

    同会话 / 同前缀 / 窗口内 / 尚未判过。前缀按 `LIKE 'board:%'` 精确到类，
    不会把 kb 的推送算成"看了看板"。
    """
    if not prefix:
        return 0
    window = push_outcome_window_hours(conn) if window_hours is None else int(window_hours)
    if window <= 0:
        return 0                             # 关闭判定：不猜任何反应
    ts = reacted_at or now_utc()
    now_dt = parse_utc(ts)
    if now_dt is None:
        raise ValueError(f"reacted_at 必须是 ISO8601 UTC（如 2026-10-24T01:00:00Z），收到 {ts!r}")
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "UPDATE push_log SET reaction='accepted', reacted_at=?, "
        "latency_sec=CAST((julianday(?) - julianday(pushed_at)) * 86400 AS INTEGER) "
        "WHERE code LIKE ? AND session_key IS ? AND reaction IS NULL AND pushed_at >= ?",
        (ts, ts, prefix, session_key, cutoff))
    conn.commit()
    return cur.rowcount or 0

def links_for(conn, code, rel=None, direction="both") -> list:
    """查一条记忆的关联邻居（**边单向存、这里双向查**）。

    返回 [{"code": 对方编码, "rel", "source", "weight", "evidence", "direction"}]，
    direction: out = 它是 src，in = 它是 dst。removed_at 非空的边不返回。
    """
    if direction not in ("both", "out", "in"):
        raise ValueError(f"direction 只能是 both/out/in，收到 {direction!r}")
    if direction == "both":
        where, params = "(src=? OR dst=?)", [code, code]
    elif direction == "out":
        where, params = "src=?", [code]
    else:
        where, params = "dst=?", [code]
    sql = ("SELECT src,dst,rel,source,weight,evidence FROM links "
           f"WHERE removed_at IS NULL AND {where}")
    if rel:
        sql += " AND rel=?"
        params.append(rel)
    sql += " ORDER BY id"
    out = []
    for src, dst, r, s, w, ev in conn.execute(sql, params):
        out.append({"code": dst if src == code else src, "rel": r, "source": s,
                    "weight": w, "evidence": ev,
                    "direction": "out" if src == code else "in"})
    return out

STALE_SENSITIVE_TYPES = ("fact", "episode", "artifact")
# 规范/教训按年龄打折是荒谬的：一条铁律不会因为写在两年前就变假。
FRESHNESS_EXEMPT_TYPES = ("rule", "identity", "lesson")

def memory_freshness(conn, code, *, now=None) -> dict:
    """一条记忆的**内容新鲜度**：`{factor, age_days, ref, band, note}`。

    band：`fresh`(≥0.7) / `aging`(≥0.35) / `stale`(<0.35) / `unknown`。
    硬过滤优先于折扣：`status != 'active'`（superseded/withdrawn）直接 `factor=0`，
    调用方应当**硬过滤**它，而不是靠分数把它压下去。

    `now` 接受 ISO 串或 datetime（统一转 `%Y-%m-%dT%H:%M:%SZ`；解析不了就用当前时间——
    时间参数坏了不该让整条候选消失）。
    """
    row = conn.execute(
        "SELECT mem_type, occurred_at, recorded_at, last_accessed_at, access_count, "
        "dormant, status FROM code_index WHERE code=?", (code,)).fetchone()
    if not row:
        return {"factor": 0.0, "age_days": None, "ref": None, "band": "unknown",
                "note": "查不到这条记忆"}
    mem_type, occurred, recorded, last_access, access_count, dormant, status = row
    if (status or "active") != "active":
        return {"factor": 0.0, "age_days": None, "ref": occurred or recorded,
                "band": "stale", "note": "已退役（被取代/作废）"}
    if (mem_type or "") in FRESHNESS_EXEMPT_TYPES:
        return {"factor": 1.0, "age_days": None, "ref": occurred or recorded,
                "band": "fresh", "note": "规范/教训不按年龄打折"}
    content_ref = occurred or recorded
    if not content_ref:
        return {"factor": 0.5, "age_days": None, "ref": None, "band": "aging",
                "note": "没有内容时间（occurred_at/recorded_at 都空）——无从判断，取中性值"}
    # 关键：第 2 个参数刻意传 None —— 让 compute_decay 回落到 recorded_at/
    # content_ref，而不是 last_accessed_at（见上面的理由）。
    factor, _state = compute_decay(mem_type, None, content_ref, load_decay_cfg(conn))
    now_dt = parse_utc(_utc_str(now)) if now else None
    if now_dt is None:
        now_dt = datetime.datetime.now(datetime.timezone.utc)
    ref_dt = parse_utc(content_ref)
    age_days = round((now_dt - ref_dt).total_seconds() / 86400, 1) if ref_dt else None
    if dormant:
        factor *= 0.5
    if access_count and last_access:
        last_dt = parse_utc(last_access)
        if last_dt and (now_dt - last_dt).days <= 30:
            factor = min(1.0, factor + 0.05)      # 置信度小加成，**不改年龄**
    band = "fresh" if factor >= 0.7 else ("aging" if factor >= 0.35 else "stale")
    note = ""
    if age_days is not None:
        half_life = get_config(conn, f"decay.{mem_type}_days") if isinstance(mem_type, str) else 0
        note = f"{age_days} 天前（{mem_type}，半衰期 {half_life or '∞'} 天）"
    return {"factor": round(factor, 4), "age_days": age_days, "ref": content_ref,
            "band": band, "note": note}

_SILENCE_FLOOR = 0.15      # 降权下限：**永不归零**
_SILENCE_CAP_DAYS = 90     # 封顶：超过这么久没推过 → 强制恢复 1.0
_PUSH_W = {                # `16` §5.1 的两组权重（收益侧 / 成本侧）
    "rel": 0.40, "gap": 0.40, "urgent": 0.20,
    "false": 0.50, "token": 0.30, "intr": 0.20,
}
_PUSH_TAG = "可能对你有用"

def _push_history(conn, code):
    """该编码的推送历史：(总次数, ignored 次数, 最近一次推送时间)。

    按 **code** 聚、不分会话：静默失败说的是"这条内容反复没人理"，
    跨会话才看得出——某个会话忽略它，不能证明它对所有人都没用。
    """
    rows = conn.execute(
        "SELECT reaction, pushed_at FROM push_log WHERE code=? ORDER BY id DESC",
        (code,)).fetchall()
    if not rows:
        return 0, 0, None
    ignored = sum(1 for r, _ in rows if r == "ignored")
    return len(rows), ignored, parse_utc(rows[0][1])

def _silence_weight(conn, code, *, now=None):
    """静默降权系数 ∈ [`_SILENCE_FLOOR`, 1.0]，**永不返回 0**。

    为什么不能归零（`15` 调研最重要的一条）：DAWN 论文的 silencing failure mode 就是
    「用户一直忽略 → 阈值一路上升 → 进入吸收态 → 永远不推」。
    用户原话是「等推送完善后会让 AI 意识到冷门工具的用途」——**归零就永远没有"以后"**。
    所以这里的下限是 0.15，且超过 `_SILENCE_CAP_DAYS` 天没推过就强制回到 1.0。
    """
    total, ignored, last = _push_history(conn, code)
    if total <= 0:
        return 1.0
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    if last is not None and (now_dt - last).days >= _SILENCE_CAP_DAYS:
        return 1.0                      # 封顶：太久没推，给一次机会
    return max(_SILENCE_FLOOR, 1.0 - 0.85 * (ignored / float(total)))

def _project_of(conn, code):
    row = conn.execute("SELECT project FROM code_index WHERE code=?", (code,)).fetchone()
    return row[0] if row else None

def _seen_in_session(conn, code, session_key):
    """本会话是否读过这条（read_body / recall / push 展示都算"见过"）。"""
    if not session_key:
        return False
    row = conn.execute(
        "SELECT 1 FROM memory_access_log WHERE code=? AND session_key=? LIMIT 1",
        (code, session_key)).fetchone()
    return bool(row)

def _cand(conn, code, kind, *, title, reason, current_project,
          seen=False, key=False, now=None, relevance=None, freshness=None,
          interrupt=0.1):
    """构造一条候选并打分（`16` §5.1：benefit − cost，再乘静默降权）。

    ## 2026-10-26（T04）三个新参数

      · `relevance`：外部已算好的相关性（提示词候选）。给了就用它，**不再自己算**；
      · `freshness`：外部已把陈旧性折扣乘进 `relevance` 时传 `None`
        （**避免双重折扣**：prompt 候选只折扣一次，见设计 §6.5）；
        传 dict 时由本函数乘 `(0.5 + 0.5×factor)`——供工具后/开场候选使用；
      · `interrupt`：打断成本档位（提示词发生在用户消息边界，比工具后更"打断"）。

    三条纪律写在这里，免得下一个人再加第四个折扣点。
    """
    proj = _project_of(conn, code)
    # 2026-10-25（用户实测抓到的 #9）：**跨项目候选要降权到近乎不出现**。
    #
    # 实测场景：新 AI 看 EVAL 项目的注入包，拿到两条
    # 「anyi-studio-261022-75（跨项目的意外关联（经 EVAL-261025-08））」——
    # 它根本不知道 anyi-studio 是什么，也不知道 EVAL-261025-08 是哪条，**纯噪音**。
    #
    # 分三档：
    #   · 同项目        → rel=1.0（要的就是它）
    #   · 没有当前项目  → rel=0.0（**跨项目无从比较**：连"当前"都没有，谈什么"跨"）
    #   · 有别的项目    → rel=0.1（0.3 太高：它压得过本项目的边缘候选，占的还是同一份预算）
    if relevance is not None:
        rel = max(0.0, min(float(relevance), 1.0))
    elif proj and current_project and proj == current_project:
        rel = 1.0
    elif not current_project:
        rel = 0.0
    else:
        rel = 0.1
    if freshness is not None:
        # 只对"内容可能过时"的类型打折；rule/identity/lesson 的 factor 恒为 1.0，
        # 乘上去等于没乘（免疫是算出来的，不是特判出来的）。
        rel *= 0.5 + 0.5 * float(freshness.get("factor", 1.0))
    novelty = 0.0 if seen else 1.0
    urgent = 1.0 if key else 0.2
    benefit = (_PUSH_W["rel"] * rel + _PUSH_W["gap"] * novelty
               + _PUSH_W["urgent"] * urgent)
    total, ignored, _last = _push_history(conn, code)
    false_p = (ignored / float(total)) if total else 0.5   # 无历史 = 中性先验
    token_cost = min(len(title or "") / 100.0, 1.0)
    cost = (_PUSH_W["false"] * false_p + _PUSH_W["token"] * token_cost
            + _PUSH_W["intr"] * float(interrupt))
    weight = 1.0 if key else _silence_weight(conn, code, now=now)
    out = {"code": code, "kind": kind, "title": title, "reason": reason,
           "project": proj, "benefit": round(benefit, 4), "cost": round(cost, 4),
           "weight": round(weight, 4), "key": bool(key),
           "score": round((benefit - cost) * weight, 4)}
    if freshness is not None:
        # 带上 freshness 便于度量/解释（"这条为什么没推"要能查）
        out["freshness"] = {k: freshness.get(k) for k in ("factor", "band", "age_days")}
    return out

def _kb_use_index() -> bool:
    """`FISH_KB_USE_INDEX=1` 才走索引路径（默认 legacy：零回归；显式开启后才换路径）。

    为什么默认不切：生产库里 `kb_index`/`kb_fts` 还不存在（T04 是"先交付索引层，
    再接线"）；默认 legacy 保证"合并即回归"，开启前必须先跑一次 `rebuild_kb_index`。
    """
    return (os.environ.get("FISH_KB_USE_INDEX") or "").strip() == "1"


def _kb_push_candidates_index(conn, q, *, project, exclude, limit, now) -> list:
    """索引路径：**K7 断路器** + `search_kb`（只查 FTS，不碰文件系统）。

    34 号 §1.3.3 / §1.4 的 K7：索引陈旧 / 未建 / 无 active 行 → **返回空 + 无重扫**
    （"宁可少推，也不推过期内容"）。索引新鲜时走 `search_kb`（<100ms 量级），
    不再每次全库 walk。
    """
    try:
        import fish_kb_index
    except Exception:       # noqa: BLE001 —— 索引层坏了不能拖垮推送
        return []
    try:
        fresh = fish_kb_index.kb_index_fresh(conn)
    except Exception:       # noqa: BLE001
        return []
    if not fresh.get("fresh"):
        return []                       # K7：断路器（陈旧/未建 → 空，不重扫）
    try:
        res = fish_kb_index.search_kb(conn, query=q, limit=max(int(limit) * 3, 6))
    except Exception:       # noqa: BLE001
        return []
    exclude = set(exclude or ())
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    since = (now_dt - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for hit in res.get("hits") or []:
        code = f"kb:{hit.get('vault_name')}:{hit.get('rel')}"
        if code in exclude:
            continue
        recent = conn.execute(
            "SELECT 1 FROM push_log WHERE code=? AND pushed_at >= ? LIMIT 1",
            (code, since)).fetchone()
        if recent:
            continue
        out.append({
            "code": code, "kind": "kb",
            "title": str(hit.get("rel") or ""),
            "reason": f"知识库「{hit.get('vault_name')}」命中「{q[:24]}」",
            "project": project, "benefit": 0.55, "cost": 0.05,
            "weight": 1.0, "key": False, "score": 0.5,
            "kb": {"vault": hit.get("vault_name"), "rel": hit.get("rel"),
                   "snippet": str(hit.get("snippet") or "")[:40]},
        })
        exclude.add(code)
        if len(out) >= max(int(limit), 1):
            break
    return out


def _kb_push_candidates(conn, root, *, query, project=None, exclude=(), limit=2,
                        now=None) -> list:
    """知识库命中 → 推送候选（P0-1：把「AI 推给 AI」接进推送管线）。

    ## 为什么单独一类

    记忆候选（`_cand`）的 `code` 是记忆编码；知识库文件不是记忆，走同一份
    `push_log` 只会把两种东西混在一个键空间里。所以 kb 候选的 `code` 用
    `kb:<vault>:<rel>`（可读、可去重、可回填），`kind="kb"` 独立成一类。

    ## 三条纪律

      · **只在你问到时才产**：`query` 为空（tool_after 没 prompt、没项目）→ 返回 []；
      · **vault 未登记 → 不产**（不噪音）；
      · **24h 内推过的同一个文件不重复推**（按 `push_log.code` 去重）。

    断路器（T03b）：整段计时交给 `_breaker`——超过 `push.breaker_seconds` 就
    **本次不推**（返回 []）并落一行诊断：慢到 3s 的检索进注入包时已落后上下文。
    """
    started = time.monotonic()
    q = (query or "").strip()
    if not q:
        return []
    # T04 接线（评审 🔴-1，2026-10-27）：`FISH_KB_USE_INDEX=1` 才走索引路径；
    # 默认 legacy（零回归——生产库还没有 kb_index 表）。
    if _kb_use_index():
        out = _kb_push_candidates_index(conn, q, project=project, exclude=exclude,
                                        limit=limit, now=now)
        return _breaker(conn, root, "kb", started, query, out)
    try:
        vaults = kb_vault_list(conn)
    except Exception:       # noqa: BLE001 —— 知识库是增强项，坏了不拖垮候选
        return []
    if not vaults:
        return []
    # 知识库检索是**字面子串**（不分词），用户原话常是整句（"海报排版怎么做"）。
    # 所以先拆成检索词：优先用记忆检索同一套分词（extract_terms），取前 3 个
    # 长度≥2 的词；都拿不到时退回整句（截断 200 字）。
    try:
        terms = [t for t in extract_terms(conn, q) if len(t) >= 2][:3]
    except Exception:       # noqa: BLE001 —— 分词坏了就退回整句
        terms = []
    needles = terms or [q[:200]]
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    since = (now_dt - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    exclude = set(exclude or ())
    out = []
    for vault in vaults:
        if len(out) >= limit:
            break
        # 2026-10-27：**一次 walk 跑全部检索词**（原实现对每个词各扫一遍 vault，
        # 3 个词 = 3 次全遍历 ≈ 2.7s，把 `prompt-push` 顶到 hook 的 10s 超时线上）。
        # `_kb_search_multi` 与逐个调 `kb_search` 行为等价（见其 docstring 的对照表）。
        try:
            multi = _kb_search_multi(conn, vault, [n[:200] for n in needles],
                                     limit_per_needle=2)
        except Exception:       # noqa: BLE001 —— 单个 vault 坏了不该拖垮其它
            continue
        by_rel = {}
        for needle in needles:
            res = multi.get(needle.strip()[:200]) or multi.get(needle) or {}
            for hit in res.get("hits") or []:
                rel = hit.get("rel") or ""
                cur = by_rel.get(rel)
                if cur is None or int(hit.get("occurrences") or 0) > int(
                        cur.get("occurrences") or 0):
                    by_rel[rel] = hit
        for hit in list(by_rel.values())[:2]:
            code = f"kb:{vault['name']}:{hit.get('rel') or ''}"
            if code in exclude or any(c["code"] == code for c in out):
                continue
            recent = conn.execute(
                "SELECT 1 FROM push_log WHERE code=? AND pushed_at >= ? LIMIT 1",
                (code, since)).fetchone()
            if recent:
                continue                    # 24h 内推过同一个文件 → 不重复
            snippet = ""
            if hit.get("lines"):
                snippet = str((hit["lines"][0] or {}).get("text") or "")[:40]
            out.append({
                "code": code, "kind": "kb",
                "title": str(hit.get("rel") or ""),
                "reason": f"知识库「{vault['name']}」命中「{q[:24]}」",
                "project": project, "benefit": 0.55, "cost": 0.05,
                "weight": 1.0, "key": False, "score": 0.5,
                "kb": {"vault": vault["name"], "rel": hit.get("rel"),
                       "snippet": snippet},
            })
            exclude.add(code)
    return _breaker(conn, root, "kb", started, query, out[:max(int(limit), 1)])

def _conv_push_candidates(conn, root, *, query, project=None, session_key=None,
                          exclude=(), limit=1, now=None):
    """语料候选（C-P0-6）：**取数/门槛/时间窗都在 `fish_corpus`**（M8：主体已搬走）。

    这里只交付两处主库依赖：`push_log` 里的"最近推过"与打分公式 `_cand`
    ——后者必须留在 `fish.py`（公式只有一份，见 `_cand` 的三条纪律）。
    断路器同 `_kb_push_candidates`（T03b）：慢到阈值以上就本次不推。
    """
    started = time.monotonic()
    out = fish_corpus.conv_push_for_session(
        root=root, query=query, project=project, exclude=exclude, limit=limit, now=now,
        recent_of=lambda hours, when: _recent_pushed_codes(conn, session_key, hours=hours,
                                                          now=when),
        scorer=lambda item: _cand(conn, item["code"], "conv_related", title=item["title"],
                                  reason=item["reason"], current_project=project, now=now,
                                  relevance=item["relevance"], freshness=None, interrupt=0.4))
    return _breaker(conn, root, "conv", started, query, out)

def build_push_candidates(conn, root, *, citizen_id=None, session_actor=None,
                          project=None, exclude=(), limit=12, now=None,
                          session_key=None, timing="inject", prompt=None):
    """生成候选（`16` §5.2 四类 + 看板变更 + 2026-10-26 的两个新类）。

    **不过滤 score<=0**：是否真的推由 `select_push_candidates` 决定。
    但静默降权归零的会被剔除——那才是"放弃这条"的正式语义。

    ## `timing` 决定追加什么（设计 §6.7）

      · `inject` / `tool_after`：四类 + 看板变更 + `activity_overlap`（动作事实反哺）；
      · `prompt`：**只追加 `prompt_related`（提示词相关的记忆）**。
        为什么不加 `activity_overlap`：用户消息边界该只收到"你问的这件事有什么记忆"，
        把撞车警告混进同一句会让人不知道该先做什么（设计 §6.7 的取舍）。

    返回列表，按 score 降序。
    """
    exclude = set(exclude or ())
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    out = []

    # 提示词时机：只递记忆（高置信 + 新鲜度硬门槛），不做四类通用候选——
    # 规则/联想/态势都是"开场"语义，塞进用户消息边界就是噪音。
    if timing == "prompt":
        out = build_prompt_candidates(conn, root, prompt=prompt or "", citizen_id=citizen_id,
                                      session_key=session_key, project=project,
                                      now=now_dt, limit=limit)
        out.sort(key=lambda c: (-c["score"], c["code"]))
        return _with_ctx_project(conn, out[:int(limit)], project)

    # 锚点：当前项目最近 3 条 active。候选从它们的 links 邻居长出来，
    # 而不是"全库最近更新"——后者会让别的项目的更新把候选带偏。
    anchors = []
    if project:
        anchors = [c for (c,) in conn.execute(
            "SELECT code FROM code_index WHERE project=? AND status='active' "
            "AND dormant=0 ORDER BY code DESC LIMIT 3", (project,)).fetchall()]
    skip = exclude | set(anchors)

    def seen(code):
        if session_key:
            return _seen_in_session(conn, code, session_key)
        return conn.execute(
            "SELECT 1 FROM memory_access_log WHERE code=? AND citizen_id IS ? LIMIT 1",
            (code, citizen_id)).fetchone() is not None

    # ── A 规则（50%）：未闭合事项。key=True → 不受静默降权 ──────────────
    since = (now_dt - datetime.timedelta(days=ATTENTION_OPEN_WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for code, title in _open_loop_rows(conn, since, 3, exclude=skip):
        out.append(_cand(conn, code, "rule", title=title, current_project=project,
                         seen=seen(code), key=True, now=now_dt,
                         reason="未闭合：开了头还没有结尾"))
        skip.add(code)

    # ── B 联想（25%）/ D 意外（10%）：都从锚点的 links 邻居里取 ──────────
    assoc, serendip = [], []
    for anchor in anchors:
        for nb in links_for(conn, anchor, direction="both"):
            code = nb.get("code")
            if not code or code in skip:
                continue
            row = conn.execute(
                "SELECT title FROM code_index WHERE code=? AND status='active' "
                "AND dormant=0", (code,)).fetchone()
            if not row:
                continue
            title = row[0] or ""
            nproj = _project_of(conn, code)
            if nproj and project and nproj != project:
                serendip.append((code, title, anchor, nb.get("weight") or 0))
            else:
                assoc.append((code, title, anchor, nb.get("weight") or 0))
            skip.add(code)
    for code, title, anchor, _w in sorted(assoc, key=lambda x: -x[3])[:4]:
        out.append(_cand(conn, code, "association", title=title, current_project=project,
                         seen=seen(code), now=now_dt,
                         reason=f"与 {anchor} 有关联",
                         freshness=memory_freshness(conn, code, now=_utc_str(now_dt))))
    for code, title, anchor, _w in sorted(serendip)[:2]:
        out.append(_cand(conn, code, "serendipity", title=title, current_project=project,
                         seen=seen(code), now=now_dt,
                         reason=f"跨项目的意外关联（经 {anchor}）",
                         freshness=memory_freshness(conn, code, now=_utc_str(now_dt))))

    # ── C 态势（15%）：同项目、别的会话最近 10 分钟读过、本会话没读 ──────
    since10 = (now_dt - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT DISTINCT m.code, c.title FROM memory_access_log m "
        "JOIN code_index c ON c.code = m.code "
        "WHERE m.accessed_at >= ? AND c.status='active' AND c.dormant=0 "
        "AND c.project IS ? ORDER BY m.accessed_at DESC LIMIT 8",
        (since10, project)).fetchall()
    for code, title in rows:
        if code in skip or seen(code):
            continue
        out.append(_cand(conn, code, "situational", title=title, current_project=project,
                         seen=False, now=now_dt,
                         reason="同项目其他会话刚读过",
                         freshness=memory_freshness(conn, code, now=_utc_str(now_dt))))
        skip.add(code)

    # ── E 看板变更（EVAL-261025-20，2026-10-25 修）─────────────────────
    # 背景：看板此前只在 SessionStart 注入一次——多 AI 陆续开工时，
    # A 看不到 B 后签的到（"看板只随开场注入一次，之后无任何变更推送"）。
    # 这里拿"上次注入看到的快照"（board_seen）与当前状态比：
    # 你看到之后又有人签到 / 有新动态 → 出一条候选，交给工具后推送。
    if citizen_id:
        try:
            # P0-3：快照按**会话**取（同公民多会话各看各的，互不吞推送）。
            # 旧库（无 session_key 列）自动降级回"按公民"——读侧与写侧同一套分叉。
            _bs_cols = _board_seen_columns(conn)
            sk_where = " AND s.session_key=?" if "session_key" in _bs_cols else ""
            sk_params = ((citizen_id, board_period()) if not sk_where
                         else (citizen_id, board_period(), session_key or ""))
            for bid, bscope, bkey, seen_post, seen_pres in conn.execute(
                "SELECT b.id, b.scope, b.scope_key, s.last_post_id, s.last_presence_at "
                "FROM board_seen s JOIN boards b ON b.id = s.board_id "
                "WHERE s.citizen_id=? AND b.status='open' AND b.period_key=?" + sk_where,
                sk_params).fetchall():
                cur_post = conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM board_posts WHERE board_id=?",
                    (bid,)).fetchone()[0]
                cur_pres = conn.execute(
                    "SELECT COALESCE(MAX(last_seen_at),'') FROM presence WHERE board_id=?",
                    (bid,)).fetchone()[0]
                new_posts = int(cur_post or 0) - int(seen_post or 0)
                new_pres = (cur_pres or "") > (seen_pres or "")
                if new_posts <= 0 and not new_pres:
                    continue
                bits = []
                if new_posts > 0:
                    bits.append(f"{new_posts} 条新动态")
                if new_pres:
                    bits.append("有人新签到/更新")
                label = bkey if bscope == "project" else "root"
                out.append({
                    "code": f"board:{label}",
                    "kind": "situational",
                    "title": f"看板 {label} 有更新",
                    "reason": "、".join(bits),
                    "project": (project if bscope == "project" and bkey == project
                                else None),
                    "benefit": 0.6, "cost": 0.05, "weight": 1.0, "key": False,
                    "score": 0.55,
                })
        except Exception:   # noqa: BLE001 —— 看板候选是增强项，不拖垮候选生成
            pass

    # ── F 动作重叠（2026-10-26 · T04）：**让动作事实反哺推送** ─────────────
    # 只在 inject/tool_after 追加（prompt 已在函数开头直接 return）。
    # 判据是"别的会话 30 分钟内写过的目录" ∩ "我声明占用的面"，或反过来
    # （我 30 分钟内写的目录 ∩ 别人声明的面）——两个方向都是"可能撞车"。
    try:
        out.extend(_activity_overlap_candidates(
            conn, project=project, citizen_id=citizen_id, session_key=session_key,
            now=now_dt))
    except sqlite3.Error:
        pass                    # 动作反哺是增强项：库坏了不该让候选整体失败

    # ── G 知识库（P0-1）：vault 命中 → 候选（有 query 才产）────────────────
    # 对外承诺的"做设计时自动推知识库文档"就落在这里：tool_after/inject 用
    # 项目名做 query；prompt 时机用用户原话（在 build_prompt_candidates 里）。
    try:
        out.extend(_kb_push_candidates(
            conn, root, query=(prompt or project or ""), project=project,
            exclude=skip, limit=2, now=now_dt))
    except Exception:       # noqa: BLE001 —— 知识库候选是增强项
        pass
    # T04：语料候选（只 prompt 时机有 query；门槛与取数在 `fish_corpus`）。
    # 与 kb 同规格的 try/except（M3）：语料是**增强项**，采集中库被写锁占住时最多
    # 少推一条，绝不能把整批候选（乃至注入包）拖垮——旧实现这里没有兜底，
    # 一次 `database is locked` 就让 tool_after 整轮抛异常。
    try:
        out.extend(_conv_push_candidates(conn, root, query=prompt, project=project,
                                         session_key=session_key, now=now_dt))
    except Exception:       # noqa: BLE001 —— 语料候选是增强项
        pass

    # 静默权重归零 = **正式放弃这条**（`_silence_weight` 正常永不返回 0，
    # 只有被显式改坏时才会——这里兜住它，让"归零"这个动作真的生效）。
    # 为什么过滤放在这里而不是 _cand 里：`_cand` 要如实报出 weight，
    # 让调用方看得见"这条被降了多少"，隐藏它等于让降权不可观测。
    out = [c for c in out if c["weight"] > 0]

    # ---- 2026-10-25（#9）：跨项目候选**只在本项目候选不足时才补**----
    #
    # 实测场景：在 EVAL 项目下，两条 `anyi-studio-*` 的「跨项目的意外关联」排进前四。
    # 而 AI 根本不知道 anyi-studio 是什么、也不知道引用的 `EVAL-261025-08` 是哪条——
    # 它不是"意外惊喜"，是**占预算的噪音**。
    #
    # 为什么不在 `_cand` 里把 rel 调到 0：那会让"跨项目关联"这个能力彻底死掉，
    # 而它在"本项目确实没东西可推"时是有价值的。所以改成**门槛**而不是**权重**：
    # 本项目候选够 3 条 → 跨项目一条都不出；不足 3 条 → 按分数补到 3 条。
    if project:
        same = [c for c in out if c.get("project") == project]
        other = [c for c in out if c.get("project") != project]
        if len(same) >= 3:
            out = same
        else:
            other.sort(key=lambda c: (-c["score"], c["code"]))
            out = same + other[:3 - len(same)]

    out.sort(key=lambda c: (-c["score"], c["code"]))
    return _with_ctx_project(conn, out[:int(limit)], project)

def _recent_pushed_codes(conn, session_key, *, hours=24, now=None) -> set:
    """本会话最近 N 小时**已经推过**的编码（去重用；prompt 与 tool_after 共用一份）。"""
    if not session_key:
        return set()
    window = int(hours or 0)
    if window <= 0:
        return set()
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now_dt - datetime.timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM push_log WHERE session_key=? AND pushed_at >= ? "
        "AND code IS NOT NULL", (session_key, cutoff)).fetchall()}

def _declared_row_fresh(last_seen, auto_seen, window_min, now_dt) -> bool:
    """声明行是否**还在有效窗口内**（`find_collisions` 的声明面口径，评审 F5）。

    两条撞车通道（推送候选 / PreToolUse 快照判定）必须对"这条声明还算不算数"
    给同一个答案：`find_collisions` 用 `declared_window_min=board.overlap_window_min`，
    这里就用同一份窗口读 `last_seen_at` / `auto_seen_at`（任一在窗内即算）。

    时间戳缺失（空串/解析失败）→ **算有效**：宁可多判一条声明面，也不许把人的
    声明静默从判定里抹掉（漏报比多一句提示更伤，而"声明了却永远不被看见"正是
    `EVAL-261025-09` 那个病）。
    """
    window = int(window_min or 0)
    if window <= 0:
        return True                      # 0 = 不限窗口（沿用 0=永不的约定）
    stamps = [t for t in (parse_utc(last_seen), parse_utc(auto_seen)) if t is not None]
    if not stamps:
        return True
    return any((now_dt - t).total_seconds() <= window * 60 for t in stamps)

def _activity_overlap_hit(mine, target_abs, min_segments) -> str:
    """我的面 vs 对方的**一个**目标（目录或文件）：命中返回给人看的共同前缀。

    为什么目标要分两种形态（评审 F4）：`path_hit` **不对称**——文件面只做后缀匹配
    （"一个文件不能包含更深的路径"）。于是：

      · 我声明目录 `p0/data`、对方写 `i:/other/p0/data/x.db` → 对方的**目录键**
        `i:/other/p0/data` 就命中；
      · 我声明**文件** `p0/fish.py`、对方在同一个文件里写 → 对方的目录键是
        `i:/other/p0`（比我的面**短**），`path_hit('p0/fish.py', 'i:/other/p0')`
        是 `None`——文件面不做分量链包含。只有拿对方的**实际文件路径**
        `i:/other/p0/fish.py` 再判一次才命中。

    所以目标清单必须是「对方的实际目录 ∪ 对方的实际文件」（`auto_surfaces.files`）：
    缺了后者 = "同一文件被两个人改"这一类真实撞车**永远沉默**。
    """
    for one in mine or ():
        if not one or not target_abs:
            continue
        hit = fish_surface.path_hit(one, target_abs, min_segments=min_segments)
        if hit:
            return hit
    return None

def _session_labels_for(conn, pairs) -> dict:
    """`{(citizen_id, session_key): label}` —— 查库补名字后交给 `fish_naming.session_labels`。

    为什么要这一个入口：会话维度的标签要同时满足两件事——**同公民内**按会话短标识消歧
    （4→6→8 位加长）、**跨公民**按重名规则消歧（`名字·host#2`）。两件事都在
    `fish_naming` 里（渲染单点），这里只负责把 `display_name/host/created_at` 取出来
    ——**不猜名字**（查不到的公民按空处理，标签自然是 `未命名`）。
    """
    pairs = list(pairs or ())
    cids = sorted({str(cid) for cid, _sk in pairs if cid})
    names = {}
    for start in range(0, len(cids), 500):
        chunk = cids[start:start + 500]
        for cid, dname, host, created in conn.execute(
                "SELECT citizen_id, display_name, host, created_at FROM citizens "
                "WHERE citizen_id IN (" + ",".join("?" * len(chunk)) + ")", chunk):
            names[cid] = {"display_name": dname, "host": host, "created_at": created}
    rows = []
    for cid, sk in pairs:
        info = names.get(str(cid)) or {}
        rows.append({"citizen_id": cid, "session_key": sk,
                     "display_name": info.get("display_name"),
                     "host": info.get("host"), "created_at": info.get("created_at")})
    return fish_naming.session_labels(rows)

def _activity_overlap_candidates(conn, *, project=None, citizen_id=None,
                                 session_key=None, now=None) -> list:
    """「动作事实 → 撞车候选」：`activity:<dir>`，24h 内同会话同目录最多一次。

    为什么它值 0.8 benefit / 0.05 cost（设计 §6.7 表）：
    真撞车（两个人同时在改同一块）是**高收益低打扰**的信号——一句话就能避免互相覆盖；
    而它的触发条件很窄（30 分钟窗口 + 有目录 + 不是我），所以噪音天然很低。

     ## 三条纪律（评审 L5：这里曾经绕过 `min_segments`）

      1. **候选生成走 `path_hit(..., min_segments=board.collision_min_segments)`**——
         与 `find_collisions` 同一份门槛。旧代码用无门槛的 `surface_hit`
         （只比同基准前缀），于是 `p0` 这种**单段同名目录**能跨项目命中，制造假撞车；
      2. **两边都换成绝对路径**再比：我的声明按**本项目根**解析（`make_abs`），
         我/别人的实际目录取事实列推出来的 `dir_abs`。相对 vs 绝对的字符串比较
         是"同名单段目录误报"的另一个来源；
      3. **临时/缓存目录跳过**（`fish_activity.is_noise_dir`，`%TEMP%`/`node_modules`…）：
         那里到处同名，也没有"两个人同改一块"这回事。

     ## 两条补充（2026-10-26 评审 F4 / F5）

      4. **目标的两种形态都要判**（F4）：`path_hit` 对文件面只做后缀匹配（"一个文件
         不能包含更深的路径"）。我声明文件面 `p0/fish.py`、对方在同一个文件里写时，
         对方给的是目录键 `i:/other/p0`（比我的面短），正向永远 `None`——所以目标
         清单取「对方的实际目录 ∪ 对方的实际文件路径」（`auto_surfaces.files`），
         见 `_activity_overlap_hit`；
      5. **我的声明面也只在窗口内算**（F5）：`find_collisions` 用
         `board.overlap_window_min` 判声明面的有效期，而这里曾经"今日板 status!=left
         即算"——两条撞车通道的时间窗口径不一致，等于同一个事实两个答案。
         现在两边都读 `board.overlap_window_min`（`last_seen/auto_seen` 任一在窗内即算，
         时间戳缺失时**算有效**：宁可多判一条声明，也不许把人的声明静默抹掉）。
    """
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    now_text = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    my_session = str(session_key or "").strip()
    window_min = int(get_config(conn, "board.collision_window_min"))
    try:
        auto = fish_activity.auto_surfaces_by_session(
            conn, window_min=window_min, now=now_text)
    except sqlite3.Error:
        return []
    # 公民级聚合**只在"我的会话未知"（`my_session == ''`）时才取**：设计 §3.6 第二处
    # 是**两档**——有会话键就只认自己那个会话，不许拿同公民别的会话的实际面冒充"我的"。
    citizen_auto = {}
    if not my_session:
        try:
            citizen_auto = fish_activity.auto_surfaces(
                conn, window_min=window_min, now=now_text)
        except sqlite3.Error:
            citizen_auto = {}
    min_segments = int(get_config(conn, "board.collision_min_segments") or 2)
    declared_window = int(get_config(conn, "board.overlap_window_min"))
    # 我的声明面（今日看板上我自己那一行）+ 我实际的目录
    mine_declared, mine_actual = [], []
    base = None                                  # 只用于**展示**（相对路径）
    if citizen_id:
        try:
            base = fish_activity.project_base(conn, project) if project else None
            # 我的声明面只认**我这一行**（判据 S9/M1 盯的就是它）：不加过滤的话，
            # "我的"会把同公民另一个会话的声明面也算进来，于是别人写那块时
            # 我会收到"你也占了这块"——而我根本没声明过（那是别人窗口的事）。
            #
            # 为什么写成两句而不是一句 `p.session_key=?`：既有判据/历史写入方是用
            # `agent_id` 记会话键的（`session_key` 列那时还不存在）。列是空串时认旧列，
            # 与快照条目的 F1 双读同一口径——只认列会让这些行的声明面整个消失，
            # 撞车候选集体静默（假绿比误报更坏）。
            has_sk = _presence_has_session(conn)
            sk_filter = (" AND (p.session_key=?"
                         " OR (p.session_key='' AND p.agent_id=?))"
                         if (my_session and has_sk) else "")
            sql = ("SELECT p.surface, b.scope, b.scope_key, p.last_seen_at, p.auto_seen_at "
                   "FROM presence p JOIN boards b ON b.id = p.board_id "
                   "WHERE p.citizen_id=? AND b.period_key=?" + sk_filter +
                   " AND p.status != 'left'")
            params = [citizen_id, board_period()]
            if sk_filter:
                params.extend([my_session, my_session])
            rows = conn.execute(sql, tuple(params)).fetchall()
            bases = {}
            for (surf, scope, scope_key, last_seen, auto_seen) in rows:
                if not _declared_row_fresh(last_seen, auto_seen, declared_window, now_dt):
                    continue          # 声明行已过窗 → 与 `find_collisions` 一样不算
                # 声明是**相对它所在看板的项目根**写的（`surface='p0/fish.py'`），
                # 所以按**那一行**的项目根解析——用"当前 cwd 的项目"会解析错板
                # （没项目根的根板声明保持相对，交给 `path_hit` 的保守路径，1 段面被挡住）。
                board_project = scope_key if scope == "project" else None
                if board_project not in bases:
                    bases[board_project] = (fish_activity.project_base(conn, board_project)
                                           if board_project else None)
                row_base = bases[board_project]
                for item in normalize_surface(surf):
                    mine_declared.append(fish_surface.make_abs(item, row_base))
            # 我的实际面：有会话键 → 只取自己那个会话（第二档）；没有 → 退公民级
            my_info = (auto.get((citizen_id, my_session)) if my_session
                       else citizen_auto.get(citizen_id)) or {}
            mine_actual = [d.get("dir_abs") or "" for d in my_info.get("dirs") or []]
        except sqlite3.Error:
            mine_declared, mine_actual = [], []
    mine = [m for m in mine_declared + mine_actual if m]
    if not mine:
        return []
    pushed = _recent_pushed_codes(
        conn, session_key, hours=int(get_config(conn, "push.prompt_exclude_hours")),
        now=now_dt)
    # 标签走**会话维度**（同公民多会话的标题要带 `·dsh#88f9`，判据 S10 钉住）。
    # 集合里**必须包含我自己**：会话短标识的消歧是"同一公民内 ≥2 个会话才加后缀"，
    # 只喂对方那一对时，我自己的会话不在集合里 → 同公民另一会话看起来像"单人单会话"，
    # 标题就不带后缀，读者分不清"是别人还是我另一个窗口"（S10 抓的正是这个）。
    pairs = list(auto.keys())
    if citizen_id:
        pairs.append((citizen_id, my_session))
    labels = _session_labels_for(conn, pairs)
    out, seen = [], set()
    for (cid, other_session), info in auto.items():
        # 排除键 = `(citizen, session)`：同会话绝不报（那是我自己），
        # 同公民**另一会话要报**——两个窗口同改一个文件是真撞车（设计 §3.2）。
        # 旧实现的排除键只有公民，于是同公民另一会话的真实撞车被整个吞掉（P4）。
        if (cid, other_session) == (citizen_id, my_session):
            continue
        # 目标清单 = 对方的**实际目录** ∪ 对方的**实际文件路径**（评审 F4）。
        # 每项 `(绝对路径, 展示兜底串, 次数)`；展示列只用于没有 base 时的兜底。
        targets = []
        for item in info.get("dirs") or []:
            dir_abs = item.get("dir_abs") or ""
            if not dir_abs:
                continue                     # 证明不了绝对目录 → 不判定（不编）
            if fish_activity.is_noise_dir(dir_abs):
                continue                     # %TEMP% / node_modules：同名不同处
            targets.append((dir_abs, item.get("dir") or "", int(item.get("count") or 0)))
        for item in info.get("files") or []:
            file_abs = item.get("path") or ""
            if not file_abs:
                continue
            if fish_activity.is_noise_dir(fish_surface.dir_of(file_abs) or ""):
                continue                     # 临时/缓存目录里的文件同样不判
            targets.append((file_abs, file_abs, int(item.get("count") or 0)))
        # 每会话最多一条，取**最具体**的那块（设计 S10 的期望就是 1 条；
        # 与 `find_collisions` 的"每会话最多 1 条"同口径）。一个会话写一个文件会同时
        # 命中目录键与文件键，不去重就会把同一次重叠印两遍——读者会当成两处冲突。
        best = None                       # (len(shown), code, shown, count)
        for target_abs, shown_hint, count in targets:
            if not _activity_overlap_hit(mine, target_abs, min_segments):
                continue
            # 展示用相对路径（绝对路径不进展示面），判定已经用绝对路径做完。
            shown = (fish_surface.relativize(target_abs, base) if base
                     else (shown_hint or target_abs))
            code = f"activity:{shown}"
            if code in pushed:
                continue
            if best is None or len(shown) > best[0]:
                best = (len(shown), code, shown, count)
        if best is None:
            continue
        _len, code, shown, count = best
        if code in seen:
            continue
        seen.add(code)
        who = labels.get((cid, other_session)) or fish_naming.UNNAMED_LABEL
        out.append({
            "code": code, "kind": "situational",
            "title": f"{who} 刚在写 {shown}（{count} 次）",
            "reason": "你也占了这块——可能撞车",
            "project": project, "benefit": 0.8, "cost": 0.05, "weight": 1.0,
            "key": False, "score": 0.75,
        })
    return out

_PROMPT_STOPWORDS = frozenset("""
的 了 是 在 和 与 请 帮 我们 你们 这个 那个 如何 什么 怎么 一下 继续 下一步
以及 还有 但是 因为 所以 如果 可以 需要 现在 已经 一个 没有 就是 只是 这样
""".split())
_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{0,19}-\d{6}-\d{2,}")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
PROMPT_MAX_TERMS = 12

def extract_terms(conn, prompt, *, limit=PROMPT_MAX_TERMS):
    """提示词 → 检索词（编码 / 项目名 / 关键词 / 中文 2-4 字 n-gram），上限 12 个。

    为什么自己切中文：FTS 的 unicode61 不做中文分词（ADR-005），
    「撞车检测」整串匹配不到「撞车」。2-4 字 n-gram 是**够用且可解释**的折中：
    全切 1 字会把"的"也算命中，切太长又会漏。
    """
    text = clean_prompt(prompt)
    terms = []

    def add(term):
        term = (term or "").strip()
        if not term or term in terms:
            return
        if term.lower() in _PROMPT_STOPWORDS:
            return
        terms.append(term)

    for match in _CODE_RE.findall(text):
        add(match)
    # 项目名（code_registry 的 prefix 与 namespace，长度 ≥2）
    for prefix, namespace in conn.execute(
            "SELECT prefix, namespace FROM code_registry").fetchall():
        for name in (prefix, namespace):
            name = (name or "").strip()
            if len(name) >= 2 and name and name in text:
                add(name)
    for word in _LATIN_RE.findall(text):
        add(word)
    for run in _CJK_RUN_RE.findall(text):
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            for start in range(0, min(len(run) - size + 1, 6)):
                add(run[start:start + size])
    return terms[:int(limit)]

def search_memories_any(conn, terms, *, project=None, limit=8, include_superseded=False):
    """多关键词检索（编码精确优先 + FTS OR + 按命中词数排序）。

    为什么不用 `search_memories`：它是**单串**入口（用户敲一个词），
    而提示词是一句话、抽出来的是十几个词。这里按"命中词数"排序：
    命中 3 个词的条目比命中 1 个的更像"用户在问的那件事"。

    只读、只 active、只 dormant=0 —— 硬过滤写在 SQL 里，不靠调用方记得。
    """
    terms = [t for t in (terms or []) if t]
    if not terms:
        return []
    limit = max(1, int(limit))
    hits = {}                            # code -> 命中的词集合

    for term in terms:
        for (code,) in conn.execute(
                "SELECT code FROM code_index WHERE code=?", (term,)).fetchall():
            hits.setdefault(code, set()).add(term)
    expr_terms = [t for t in terms if len(t) >= 2 and not _CODE_RE.fullmatch(t)]
    if expr_terms:
        # FTS5 的 OR 查询：每个词都加引号（否则 `-`/`:` 会被当运算符）。
        expr = " OR ".join(f'"{t}"' for t in expr_terms)
        try:
            for (code,) in conn.execute(
                    "SELECT code FROM memory_fts WHERE memory_fts MATCH ? LIMIT 200",
                    (expr,)).fetchall():
                hits.setdefault(code, set())
        except sqlite3.Error:
            pass                         # 表达式非法 → 只保留编码命中，绝不炸
    if not hits:
        return []
    projects = resolve_project_names(conn, project) if project else []
    placeholders = ",".join("?" * len(hits))
    sql = ("SELECT code, title, project, mem_type, tags FROM code_index "
           "WHERE code IN (" + placeholders + ")")
    params = list(hits)
    if not include_superseded:
        # 硬过滤写在 SQL 里（不是靠调用方记得）：被取代/作废的条目**永远**不进候选。
        sql += " AND status='active' AND dormant=0"
    if projects:
        sql += " AND project IN (" + ",".join("?" * len(projects)) + ")"
        params.extend(projects)
    rows = []
    for code, title, mem_project, mem_type, tags in conn.execute(sql, params).fetchall():
        text = f"{code} {title or ''} {tags or ''}".lower()
        matched = {t for t in terms if t.lower() in text} | hits.get(code, set())
        rows.append((code, title or "", mem_project or "", mem_type or "",
                     tuple(sorted(matched))))
    rows.sort(key=lambda r: (-len(r[4]), r[0]))
    return rows[:limit]

def build_prompt_candidates(conn, root, *, prompt, citizen_id=None, session_key=None,
                            project=None, now=None, limit=3) -> list:
    """提示词命中的高置信记忆 → 候选（`prompt_related`）。

    打分（设计 §6.4）：
        1.00 编码精确命中
      + 0.40 项目名命中且记忆 project 一致
      + 0.30 × 标题命中的不同词数（≤0.6）
      + 0.15 仅正文/标签命中
      （cap 1.0）

    然后 `rel_eff = relevance × (0.5 + 0.5 × freshness.factor)`，
    门槛 `push.prompt_min_relevance`（默认 0.6），且 `stale` **硬过滤**。

    ⚠️ 传给 `_cand` 时用 `relevance=rel_eff, freshness=None`：
    折扣只乘一次（设计 §6.5 的"避免双重折扣"）。
    """
    if not prompt:
        return []
    now_dt = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        ensure_fts(conn, root)      # 空索引自愈（幂等）：老库升级后不重建会"静默查不到"
    except (sqlite3.Error, OSError, TypeError):
        pass                        # 自愈失败不该让提示词推送整体失败
    terms = extract_terms(conn, prompt)
    # P0-1：知识库命中与记忆命中**并行**产——记忆没命中不代表知识库没有
    # （"做设计时推知识库文章"恰恰常发生在记忆里没有对应条目时）。
    kb_cands = []
    try:
        kb_cands = _kb_push_candidates(conn, root, query=prompt, project=project,
                                       limit=1, now=now_dt)
    except Exception:       # noqa: BLE001 —— 知识库候选是增强项
        pass
    # T04：语料候选（M3：与 kb 同规格兜底——语料读不到只是少一条候选，
    # 绝不许把用户消息边界上的 prompt-push 整个打断）
    try:
        conv_cands = _conv_push_candidates(conn, root, query=prompt, project=project,
                                           session_key=session_key, limit=1, now=now_dt)
    except Exception:       # noqa: BLE001 —— 语料候选是增强项
        conv_cands = []
    if not terms:
        return _with_ctx_project(conn, kb_cands + conv_cands, project)
    rows = search_memories_any(conn, terms, project=None, limit=max(int(limit) * 4, 12))
    if not rows:
        return _with_ctx_project(conn, kb_cands + conv_cands, project)
    pushed = _recent_pushed_codes(
        conn, session_key, hours=int(get_config(conn, "push.prompt_exclude_hours")),
        now=now_dt)
    title_terms = [t for t in terms if not _CODE_RE.fullmatch(t)]
    min_rel = float(get_config(conn, "push.prompt_min_relevance"))
    stale_floor = float(get_config(conn, "push.prompt_stale_floor"))
    out = []
    for code, title, mem_project, mem_type, matched in rows:
        if code in pushed:
            continue                                  # 24h 内推过
        if session_key and _seen_in_session(conn, code, session_key):
            continue                                  # 本会话已读过，不需要再递
        fresh = memory_freshness(conn, code, now=_utc_str(now_dt))
        # **唯一**的新鲜度硬门槛就是 `push.prompt_stale_floor`（默认 0.35，与
        # band 的 stale 阈值同值但可独立调）。刻意**不**再加一条
        # `band == 'stale'` 的判断：两条门槛同时存在时，调其中一条不生效
        # （判据会以为门槛坏了），而设计 §6.5 只允许一个。
        if mem_type in STALE_SENSITIVE_TYPES and fresh["factor"] < stale_floor:
            continue                                   # 宁可不推，不递可能错的
        relevance = 0.0
        if code in matched or code in terms:
            relevance += 1.0
        if project and mem_project and mem_project == project:
            relevance += 0.40
        title_hits = sum(1 for t in title_terms if t and t in (title or ""))
        relevance += min(0.30 * title_hits, 0.60)
        if relevance == 0.0:
            relevance = 0.15                          # 仅正文/标签命中
        rel_eff = min(1.0, relevance) * (0.5 + 0.5 * fresh["factor"])
        if rel_eff < min_rel:
            continue
        reason = f"你问的这件事有记忆（{fresh['note'] or '新'}"
        if fresh["band"] == "aging":
            reason += "，可能过时"
        reason += "）"
        cand = _cand(conn, code, "prompt_related", title=title, reason=reason,
                     current_project=project, seen=False, key=False, now=now_dt,
                     relevance=rel_eff, freshness=None, interrupt=0.4)
        cand["relevance"] = round(rel_eff, 4)
        cand["freshness"] = {"factor": fresh["factor"], "band": fresh["band"],
                             "age_days": fresh["age_days"]}
        out.append(cand)
    # P0-1：知识库候选与记忆候选同批排序、同一预算（"做设计的 AI 拿到设计文章"）。
    out.extend(kb_cands)
    out.extend(conv_cands)          # T04：原话候选同批（同一份预算，不另开一格）
    out.sort(key=lambda c: (-c["score"], c["code"]))
    return _with_ctx_project(conn, out[:max(int(limit), 1)], project)

# ---------------------------------------------------------------- 六特征重排（T03b）
#
# T03b-R2（2026-10-28）把重排**整段**搬进 `fish_push_rerank`（评审 S4：本文件当时贴着
# J4 的 1,500 行上限，只剩 1 行余量）。这里只留三样"生成侧才有的知识"：
#   · `_with_ctx_project`：当前项目由本模块知道，贴给候选（`_infer_project` 不必猜）；
#   · `select_push_candidates`：开关 / top-K / now 的唯一入口；
#   · `_kb_push_candidates` / `_conv_push_candidates` 的断路器调用点。
# 依赖方向不变（J2/J6）：fish_push → fish_push_rerank → fish_common / fish_corpus_rank。

def _with_ctx_project(conn, cands, project):
    """重排开启时给候选贴 `_ctx_project`（当前项目）；关着时原样返回（off 逐字节等价）。

    为什么要贴在候选上：prompt 池是**跨项目**的（`search_memories_any` 传的是
    `project=None`），"当前项目"只有生成侧知道。R3 收口（2026-10-28）：`_infer_project` 已
    收窄成"**只认唯一值**"（混合多项目 → None → 全池 0.5 中性），所以不贴标签**不会再造出
    假 1.0**；但"没标签"也意味着 prompt 路径丢掉 `project_match` 与 24h 去重窗口这两条真
    信号。根因是 `fish.py:9405` 没把 `project`/`now` 传下来——**那是 A 的必做项**（不在本
    批写作用域），不是"不补也能正确"。
    """
    if not _rerank_enabled(conn):
        return cands
    name = str(project or "").strip()
    if not name:
        return cands
    for cand in cands or ():
        if isinstance(cand, dict):
            cand[CTX_PROJECT] = name
    return cands


def select_push_candidates(conn, cands, *, limit=4, prefer_kind=None, project=None,
                           now=None, rerank=None):
    """按分数选，并保证**意外类至少 1 条**（防茧房闸 2）。

    只选 score > 0 的（`16` §5.1：收益 > 成本）；开启重排后作用于六特征加权和
    （值域 −0.30..0.95，四项中等也可能落到 0 以下；跨类只保序不保距）。
    `prefer_kind` 给某类留第一格（分档属于 build_push_candidates，这里只排序+配额）。
    T03b：`rerank=None` 读 `push.rerank_enabled`（默认 False，关闭时逐字节等价）；
    True/False 显式覆盖；开启时先给全池贴 `kind_class`，再对 top-K 重排；project/now
    只在重排时用（now 当场取当前 UTC，不采信候选里的旧值）。
    `cands` 先 list()：`_tag_kind_class` 会遍历池子，生成器会被吃光（QA F-1）。
    """
    cands = list(cands)
    if _rerank_enabled(conn, rerank):
        cands = _tag_kind_class(cands)
        cands = rerank_candidates(
            conn, cands,
            top_k=_cfg_any(conn, "push.rerank_top_k", RERANK_TOP_K_DEFAULT),
            now=now or now_utc(), project=project)
    cands = [c for c in cands if c["score"] > 0]
    picked, seen_kinds = [], {}
    quota = int(limit)
    # 先给"意外"留一格——不靠排序运气（否则它在冷启动时永远排不上）
    ser = [c for c in cands if c["kind"] == "serendipity"]
    if ser:
        picked.append(ser[0])
        seen_kinds["serendipity"] = 1
    if prefer_kind:
        wanted = [c for c in cands if c["kind"] == prefer_kind and c not in picked]
        if wanted:
            picked.append(wanted[0])          # 指定类别优先占第一格
    for c in cands:
        if len(picked) >= quota:
            break
        if c in picked:
            continue
        picked.append(c)
    picked.sort(key=lambda c: (-c["score"], c["code"]))
    return picked[:quota]

def build_push_block(conn, root, *, budget_tokens=None, **kw):
    """把候选渲染成注入包的一段（`16` §5.7 的"开场"档）。

    空就不出现（与 ③④⑤ 同一纪律：不写"暂无候选"这种噪音，噪音会训练 AI 跳过这段）。
    """
    if not get_config(conn, "push.enabled"):
        return {"text": "", "codes": [], "trimmed": [], "candidates": [], "omitted": 0}
    budget = int(budget_tokens or get_config(conn, "push.open_budget_tokens"))
    cands = build_push_candidates(conn, root, **kw)
    # T03b：`project` / `now` 从 `kw` 透传给选择器——重排要用它们做 project_match 与
    # 24h 去重窗口。重排关着时这两个参数无人使用（默认关的等价性由 R-兼容判据钉住）。
    picked = select_push_candidates(conn, cands, project=kw.get("project"),
                                    now=kw.get("now"))
    line_budget = budget
    lines = []
    for c in picked:
        line = f"- {c['code']} {_clip_chars(c['title'], ATTENTION_TITLE_MAX_CHARS)}（{c['reason']}）"
        if count_tokens("\n".join(lines + [line])) > line_budget:
            break
        lines.append(line)
    if not lines:
        # `omitted`：选出来了但一行都没印（预算为 0 或首行就超）——声明要用它。
        return {"text": "", "codes": [], "trimmed": [], "candidates": [],
                "omitted": len(picked)}
    text = _PUSH_TAG + "：\n" + "\n".join(lines) + "\n"
    codes = [ln[2:].split(" ", 1)[0] for ln in lines]
    # 2026-10-26（FB-02）：`omitted` = 被预算挡在门外的候选条数。旧实现只回
    # "印了哪几条"，于是"还有 3 条没印"这件事在回包里彻底消失——读者会以为候选就这些。
    return {"text": text, "codes": codes, "trimmed": [],
            "omitted": max(0, len(picked) - len(lines)),
            "candidates": [{"code": c["code"], "kind": c["kind"], "score": c["score"],
                            "reason": c["reason"]} for c in picked]}


__all__ = [
    'FRESHNESS_EXEMPT_TYPES',
    'PROMPT_MAX_TERMS',
    'STALE_SENSITIVE_TYPES',
    '_CJK_RUN_RE',
    '_CODE_RE',
    '_LATIN_RE',
    '_PROMPT_STOPWORDS',
    '_PUSH_TAG',
    '_PUSH_W',
    '_SILENCE_CAP_DAYS',
    '_SILENCE_FLOOR',
    '_activity_overlap_candidates',
    '_activity_overlap_hit',
    '_cand',
    '_conv_push_candidates',
    '_declared_row_fresh',
    '_kb_push_candidates',
    '_project_of',
    '_push_history',
    '_recent_pushed_codes',
    '_seen_in_session',
    '_session_labels_for',
    '_silence_weight',
    'build_prompt_candidates',
    'build_push_block',
    'build_push_candidates',
    'extract_terms',
    'links_for',
    'mark_push_accepted',
    'mark_push_accepted_prefix',
    'memory_freshness',
    'search_memories_any',
    'select_push_candidates',
]
