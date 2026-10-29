#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送二次重排（T03b-R4 收口 · 34 §3.3/§3.4/§3.5）：六特征取数 + 装配 + 断路器。

为什么独立成模块（评审 S4 / J4）：首轮把重排整段塞进 `fish_push.py`，顶到 1,499 / 1,500 行
——下一个人加两行就红；重排与候选生成之间只有 `rerank_candidates` 一个接口，是干净切点。
依赖方向（J2）：本模块 → `fish_corpus_rank` / `fish_common` / 标准库，**不 import `fish` /
`fish_push`**；默认关时本模块全部函数不被调用（`select` 与 T03b 之前逐字节等价）。

四处最容易写错的口径（各有判据）：① `project_match` 对 kb/corpus 恒 0.5 中性（它们的
`project` 是生成上下文）；② 当前项目**只认唯一值**——`_ctx_project` 或"池内只剩一个项目"，
混合多项目 → `None` → 全池 0.5（R3）；③ 配置坏值**回落默认 + 落一条诊断**，不抛、不穿到
注入包（R3；R4 起含"已登记键的坏字面量"）；④ 权重与总分**都不许是 NaN/Inf**——`NaN`/
`Infinity` 是合法 JSON，进 `score` 后 `score > 0` 会把整池筛空（静默空注入），R4 逐项校。
"""

import datetime
import hashlib
import json
import math
import sqlite3
import time
from pathlib import Path

import fish_common                        # 静默清零出口（模块引用，不进 __all__）
import fish_corpus_rank as rank_mod      # 特征名/刻度/默认权重/Normalize 只有这一份

from fish_common import (
    warn_ignored,
    _alias_map,
    _utc_str,
    _walk_alias,
    now_utc,
    parse_utc,
    resolve_project_names,
)

RERANK_NAME = "push_six_features"       # `cand["rerank"]` 的读数：这条是谁排的
RERANK_TOP_K_DEFAULT = 50
RERANK_TOP_K_MAX = 500                  # 配置写坏（10**9）时的上界：top-K 必须真的是 K
RERANK_ENABLED_DEFAULT = False          # T03b 裁决 4：默认关，保既有判据零回归
PROJECT_NEUTRAL = 0.5                   # 推不出当前项目 / 非记忆类的 project_match
BREAKER_SECONDS_DEFAULT = 3.0           # 断路器阈值；<=0 = 关闭（沿用 0=永不 的约定）
WEIGHT_ABS_MAX = 10.0                   # 权重上界：34 §3.3 量级是 0.x，写坏成 1e9 = 重排失效
BOOL_WORDS = {"1": True, "true": True, "yes": True, "on": True,
              "0": False, "false": False, "no": False, "off": False}
BREAKER_AUDIT = ("_audit", "push_breaker.jsonl")
CONFIG_DIAG = ("_audit", "push_config.jsonl")   # 坏配置留证（不抛、不穿到注入包）
CODES_PER_QUERY = 400                   # IN (...) 的分批大小（SQLite 变量上限 999，留余量）
CTX_PROJECT = "_ctx_project"            # 生成侧贴的"当前项目"（重排开启时才贴）
RERANK_WEIGHTS_JSON = json.dumps(rank_mod.FEATURE_WEIGHTS, ensure_ascii=False)  # 34 §3.3 逐字

_DIAG_SEEN = set()      # 同进程内同一个 (键, 原值) 只落一条：坏配置不许刷爆 _audit

def _diag(conn, key, raw, fallback, reason):
    """坏配置留证：一行 JSONL 到 `<data>/_audit/push_config.jsonl`（原值截断 80 字，同进程去重）。

    为什么不抛（评审 S3）：一条 `push.rerank_top_k=abc` 抛出去会让整个注入包消失；静默回落默认
    的症状是"配置写了但没生效"，谁都查不出来——留一条诊断两头都顾。
    """
    token = (str(key), str(raw)[:80])
    if token in _DIAG_SEEN:
        return
    _DIAG_SEEN.add(token)
    record = {"ts": now_utc(), "key": str(key), "raw": str(raw)[:80],
              "fallback": fallback, "reason": reason}
    try:
        main = [row[2] for row in conn.execute("PRAGMA database_list").fetchall()
                if row[1] == "main" and row[2]]
        if not main:
            return
        path = Path(main[0]).parent / CONFIG_DIAG[0] / CONFIG_DIAG[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        warn_ignored('fish_push_rerank.py:75 _diag', exc)

def _cfg_any(conn, key, default):
    """读配置：config 表原值 → 按**默认值的类型** cast；转不动/超范围 → 默认 + 一条诊断。

    为什么不"已登记的键就直接信 `get_config`"（R4 · QA B）：它只按登记类型转换，**不设范围
    也不设字面量白名单**——`push.breaker_seconds` 登记成 string 后 `"abc"` 会原样穿到
    `float()`（炸点在 `_breaker`，整包注入消失）；登记成 bool 的键写 `"abc"` 则被静默读成
    `False`。cast 放在唯一出口：登记与否，坏值都只能是"默认 + 诊断"。
    """
    try:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    except sqlite3.Error as exc:
        _diag(conn, key, "config 表读不到", default, "%s: %s" % (type(exc).__name__, exc))
        return default
    if not row or row[0] is None:
        return default
    text = str(row[0]).strip()
    if isinstance(default, bool):
        if text.lower() in BOOL_WORDS:
            return BOOL_WORDS[text.lower()]
        _diag(conn, key, text, default, "布尔值不认识（只认 1/0/true/false/yes/no/on/off）")
        return default
    if isinstance(default, (int, float)):
        try:
            value = float(text)
        except (TypeError, ValueError):
            _diag(conn, key, text, default, "不是数字")
            return default
        if not math.isfinite(value):
            _diag(conn, key, text, default, "不是有限数（nan/inf）")
            return default
        return int(value) if isinstance(default, int) else value
    return text

def _rerank_enabled(conn, override=None) -> bool:
    """重排开关：显式 `override` 优先（判据与回滚都用得着），否则读配置。"""
    if override is not None:
        return bool(override)
    return bool(_cfg_any(conn, "push.rerank_enabled", RERANK_ENABLED_DEFAULT))

def _rerank_weights(conn) -> dict:
    """六特征权重：`push.rerank_weights`（JSON）优先，坏值退默认 + 诊断（**不抛**）。

    三条容错都是为了"只改一个权重"的写法成立：未登记的特征名**丢掉**（不诊断——"多写一个
    名字"与"值写坏"不是一类）；缺的键按默认补齐（半份配置 = 其余按默认，不是按 0）；单个值
    写坏只影响它自己 + 一条诊断。R4：`NaN`/`Infinity`/`"nan"` 与 `|w| > 10` 都算写坏——NaN
    权重会让**每一条**候选的 score 都成 NaN（整池被筛空）。
    """
    default = dict(rank_mod.FEATURE_WEIGHTS)
    raw = _cfg_any(conn, "push.rerank_weights", RERANK_WEIGHTS_JSON)
    try:
        table = raw if isinstance(raw, dict) else json.loads(str(raw or ""))
    except (TypeError, ValueError) as exc:
        _diag(conn, "push.rerank_weights", raw, "默认权重", "JSON 解析失败：%s" % exc)
        return default
    if not isinstance(table, dict):
        _diag(conn, "push.rerank_weights", raw, "默认权重", "JSON 不是对象")
        return default
    out = dict(default)
    for name, value in table.items():
        if name not in rank_mod.FEATURE_NAMES:
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError, OverflowError):   # 超大整数字面量 → OverflowError
            weight = None
        if weight is None or not math.isfinite(weight) or abs(weight) > WEIGHT_ABS_MAX:
            _diag(conn, "push.rerank_weights.%s" % name, value, default[name],
                  "权重必须是 |w|≤%s 的有限数（这个特征用默认，其余照配）" % WEIGHT_ABS_MAX)
            continue
        out[name] = weight
    return out

def _infer_project(cands):
    """当前项目：`_ctx_project` 优先；否则**池内 mem 项目唯一**时才采用它；否则 None。

    四条纪律（评审 S1 / 修法 a）：① `_ctx_project` 由生成侧贴，比"池内多数票"可信；
    ② kb/corpus 的 `project` 是**生成上下文**，不进候选集；③ **混合多项目 → None**：多数票
    （3 跨 + 1 同）会把**别的项目**推成当前项目、让跨项目记忆白拿 1.0——宁可承认"不知道"
    （全池 0.5 中性），也不猜；④ 单项目池仍采用（退化批 ≠ 多数票赢了）。
    """
    ctx, projects = "", set()
    for cand in cands or ():
        if not isinstance(cand, dict):
            continue
        name = str(cand.get(CTX_PROJECT) or "").strip()
        if name and not ctx:
            ctx = name
        if rank_mod.semantic_kind(cand) != rank_mod.KIND_CLASS_MEM:
            continue                # 非记忆类不参与（见纪律 2）
        project = str(cand.get("project") or "").strip()
        if project:
            projects.add(project)
    if ctx:
        return ctx
    return next(iter(projects)) if len(projects) == 1 else None

def _tag_kind_class(cands, *, notes=None) -> list:
    """给**所有**候选贴 `kind_class ∈ {mem, kb, corpus}`（`kind` 一字不改）。整池都贴、不只
    贴 top-K：类别是"这段文字是什么"的**事实**，不是排序产物；未知 kind 归 mem 且不抛。"""
    for cand in cands or ():
        if isinstance(cand, dict):
            cand["kind_class"] = rank_mod.semantic_kind(cand, notes=notes)
    return list(cands or ())

def _freshness_of(cand) -> float:
    """`freshness.factor`（dict）/ 旧形态 float / 缺 → 1.0（没记时间 ≠ 很旧）。"""
    fresh = (cand or {}).get("freshness")
    value = fresh.get("factor") if isinstance(fresh, dict) else fresh
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    return max(0.0, min(float(value), 1.0))

def _alias_group_for(conn, project):
    """当前项目的**别名组**（同 canonical 的全部名字）→ 交给 `project_match`；组装不了 → None。
    别名展开只走 `fish_common._walk_alias`（唯一实现；自己拼 `name_aliases` 的传递闭包一定
    会和写入侧漂开）。
    """
    name = str(project or "").strip()
    if not name:
        return None
    try:
        amap = _alias_map(conn, "project")
    except (KeyError, sqlite3.Error):
        return None
    canonical, _chain = _walk_alias(amap, name)
    names = {name, canonical}
    names.discard("")
    for alias in amap:
        if alias != canonical and _walk_alias(amap, alias)[0] == canonical:
            names.add(alias)
    return [names] if len(names) > 1 else None

def _chunks(items, size=CODES_PER_QUERY):
    """把 code 列表切成 IN (...) 安全的批（一次 SQL 的变量数有上限）。"""
    items = [item for item in (items or ()) if item]
    for start in range(0, len(items), int(size)):
        yield items[start:start + int(size)]

def _count_by_code(conn, codes, template, params=()) -> dict:
    """按 code 分批 COUNT → `{code: 次数}`；库坏了返回已读到的部分（不抛）。`template` 的 `{}`
    是 code 的 `?` 位置，`params` 排在 code 之后（code 必须是第一条件）。"""
    out = {}
    for chunk in _chunks(codes):
        try:
            rows = conn.execute(template.format(",".join("?" * len(chunk))),
                                list(chunk) + list(params)).fetchall()
        except sqlite3.Error:
            return out              # 读不到就当 0 次：一个读数不该让重排炸掉
        out.update({code: int(count or 0) for code, count in rows})
    return out

def _access_counts(conn, codes) -> dict:
    """`{code: 被读过几次}`（`memory_access_log`；"被读过"是 access_ratio 的唯一来源）。"""
    return _count_by_code(conn, codes, "SELECT code, COUNT(*) FROM memory_access_log "
                                      "WHERE code IN ({}) GROUP BY code")

def _dup_counts(conn, codes, *, now_text) -> dict:
    """`{code: 24h 内被推过几次}`（**全局**：不分会话——某个会话没见过它 ≠ 它不冗余）。"""
    now_dt = parse_utc(now_text)
    if now_dt is None:
        return {}
    cutoff = _utc_str(now_dt - datetime.timedelta(hours=float(rank_mod.DUP_WINDOW_HOURS)))
    return _count_by_code(conn, codes, "SELECT code, COUNT(*) FROM push_log "
                                      "WHERE code IN ({}) AND pushed_at >= ? GROUP BY code",
                          [cutoff])

def _link_strengths(conn, codes, project, *, anchor_limit=3) -> dict:
    """`{code: 与当前项目锚点的 links 权重和}`（`removed_at IS NULL` + `suppressed=0`；锚点 =
    当前项目最近 `anchor_limit` 条 active，与 `build_push_candidates` 同一句 SQL）。四条口径
    （评审 D/S3）：① 锚点走 `resolve_project_names` 展开（否则别名项目恒 0）；② 双向取边（links
    无方向语义）；③ 候选自己就是锚点时不计数；④ `sqlite3.Error` 返空（老库缺列会穿到 builder）。"""
    name = str(project or "").strip()
    target = [code for code in (codes or ()) if code]
    if not name or not target:
        return {}
    try:
        names = resolve_project_names(conn, name) or [name]
        marks = ",".join("?" * len(names))
        anchors = [row[0] for row in conn.execute(
            "SELECT code FROM code_index WHERE project IN (" + marks + ") "
            "AND status='active' AND dormant=0 ORDER BY code DESC LIMIT ?",
            list(names) + [int(anchor_limit)]).fetchall() if row[0]]
    except sqlite3.Error:
        return {}
    if not anchors:
        return {}
    anchors_set = set(anchors)
    out = {}
    for chunk in _chunks(target, size=max(1, (CODES_PER_QUERY - len(anchors)) // 2)):
        own = [code for code in chunk if code not in anchors_set]
        if not own:
            continue
        own_set, marks = set(own), ",".join("?" * len(own))
        anchor_marks = ",".join("?" * len(anchors))
        try:
            rows = conn.execute(
                "SELECT src, dst, weight FROM links WHERE removed_at IS NULL AND suppressed=0 "
                "AND ((src IN (" + marks + ") AND dst IN (" + anchor_marks + ")) OR "
                "(dst IN (" + marks + ") AND src IN (" + anchor_marks + ")))",
                list(own) + anchors + list(own) + anchors).fetchall()
        except sqlite3.Error:
            return out
        for src, dst, weight in rows:
            value = float(weight or 0.0)
            if src in own_set and dst in anchors_set:
                out[src] = out.get(src, 0.0) + value
            if dst in own_set and src in anchors_set:
                out[dst] = out.get(dst, 0.0) + value
    return out

def _relevance_norms(scores) -> list:
    """首阶段分 → 批内 0..1（**越高越好**）：`rank_norm` 是"越小越好"（为 FTS5 的负数
    `bm25()` 写的），所以先取负再归一；退化批（全相等）仍是全 1.0。NaN/Inf 当 0.0：一个坏读数
    不许把**整批**的 relevance 一起毒成 NaN（那会把整池 `score > 0` 全灭）。"""
    values = [float(s) if isinstance(s, (int, float)) and math.isfinite(float(s)) else 0.0
              for s in scores]
    return rank_mod.rank_norm([-value for value in values])

def _project_match_of(cand, current_project, alias_group, kind_class=None) -> float:
    """候选的 `project_match`（六特征里最容易造假的一条，评审 S2 / QA F-3）。

    记忆的 `project` 是**归属项目**（可比较：1 / 0.5 别名 / 0）；kb 与 corpus 的是**生成上下
    文**，恒 0.5 中性——给 1.0 会让它们白拿 `0.15`，再叠加"kb 没有 `freshness` → 取 1.0"的
    另一个 `0.15`（首轮 🟠 S2 的假特征）。当前项目未知时记忆类同样 0.5。
    """
    kind = kind_class or rank_mod.semantic_kind(cand)
    if kind in (rank_mod.KIND_CLASS_KB, rank_mod.KIND_CLASS_CORPUS):
        return PROJECT_NEUTRAL
    current = str(current_project or "").strip()
    if not current:
        return PROJECT_NEUTRAL
    return rank_mod.project_match((cand or {}).get("project"), current, alias_group)

def rerank_candidates(conn, cands, *, top_k=50, now=None, project=None) -> list:
    """对 first-phase 的 **top-K** 做二次打分（34 §3.3/§3.4 · T03b）。

    返回**新列表**：头部（top-K，新分降序）在前，尾部原样跟在后面（两段分数不同量纲）。
    五条纪律：① 只算 top-K；② 原子替换（`score`/`score_breakdown` 同阶段替换、
    `first_phase_score` 留档、`score_stage` 标阶段）；③ 分项和 == 总分（先 round 再求和）；
    ④ 不改入参字典（头部返回副本）；⑤ 分项和必须是**有限数**——NaN/Inf 的候选 `score=0`
    （= 不入选）并记一条诊断，宁可少一条也不许让一个坏读数毒掉整池（R4）。
    `top_k`（QA D-1）：`<1` 夹到 1、`>500` 夹到 500、解析失败退 50。`now`（评审 S3）：
    解析不了 ISO8601 UTC 就用**当前 UTC + 诊断**——绝不静默把 24h 去重窗口关掉。
    """
    cands = [cand for cand in (cands or ()) if isinstance(cand, dict)]
    if not cands:
        return []
    try:
        head_size = min(max(int(top_k), 1), RERANK_TOP_K_MAX)    # 配置写坏时 K 仍是 K
    except (TypeError, ValueError):
        head_size = RERANK_TOP_K_DEFAULT
    ordered = sorted(cands, key=lambda c: (-float(c.get("score") or 0.0),
                                           str(c.get("code") or "")))
    head, tail = ordered[:head_size], ordered[head_size:]
    now_text = _utc_str(now) if now else now_utc()
    if parse_utc(now_text) is None:
        fallback = now_utc()
        _diag(conn, "now", now_text, fallback, "不是 ISO8601 UTC 串（%Y-%m-%dT%H:%M:%SZ）")
        now_text = fallback
    project_name = str(project or "").strip() or _infer_project(ordered)
    weights = _rerank_weights(conn)
    codes = [str(cand.get("code") or "") for cand in head]
    access = _access_counts(conn, codes)
    dups = _dup_counts(conn, codes, now_text=now_text)
    links = _link_strengths(conn, codes, project_name)
    alias_group = _alias_group_for(conn, project_name) if project_name else None
    norms = _relevance_norms([cand.get("score") for cand in head])
    top_access = max(list(access.values()) or [0])
    top_link = max(list(links.values()) or [0.0])
    scored, rejected = [], []
    for cand, relevance in zip(head, norms):
        code = str(cand.get("code") or "")
        kind = rank_mod.semantic_kind(cand)
        features = {
            "relevance": round(relevance, 6),
            "freshness": round(_freshness_of(cand), 6),
            "access_ratio": round(rank_mod.access_ratio(access.get(code, 0), top_access), 6),
            "project_match": round(
                _project_match_of(cand, project_name, alias_group, kind), 6),
            "link_strength": round(rank_mod.link_strength(links.get(code, 0.0), top_link), 6),
            "dup_penalty": round(rank_mod.dup_penalty(dups.get(code, 0)), 6),
        }
        fresh = dict(cand)
        fresh["first_phase_score"] = cand.get("score")
        score = round(rank_mod.combine_features(features, weights), 6)
        if not math.isfinite(score):        # R4：NaN/Inf 不许进 score（会毒穿整池）
            rejected.append(code)
            score = 0.0                     # 0 = 不入选（`score > 0` 门槛挡掉）
        fresh["score"] = score
        fresh["score_breakdown"] = features
        fresh["score_stage"] = rank_mod.SCORE_STAGE_RERANK
        fresh["rerank"] = RERANK_NAME
        scored.append(fresh)
    if rejected:
        _diag(conn, "push.rerank_score", ",".join(rejected[:3]), 0.0,
              "%d 条候选的六特征加权和不是有限数（NaN/Inf → 该候选不入选）" % len(rejected))
    scored.sort(key=lambda c: (-float(c["score"]), str(c.get("code") or "")))
    return scored + tail

def _breaker(conn, root, component, started, query, out) -> list:
    """断路器：单次取候选用时超过 `push.breaker_seconds`（**<=0 = 关闭**）→ 本次不推。

    为什么是"不推"而不是"推慢的结果"（P0-1 的教训）：慢到 3s 的检索进注入包时已经落后于
    用户这句话的上下文。这个阈值与"hook 端到端 < 2s"**是两件事**（评审 W2）：默认 3.0s 守
    10s 超时线；2s 是**预算线**，由 R6 判据在决策路径上守（断路器是后置判定，省不了墙钟）。
    `<=0` = 关闭（沿用 `decay.*=0` 的"0 = 永不"约定，`BreakerOffTest` 钉住）。阈值读坏回落
    默认 + 诊断（评审 S3）；只留 `{ts, component, seconds, query_hash}`。
    """
    elapsed = time.monotonic() - float(started or 0.0)
    limit = float(_cfg_any(conn, "push.breaker_seconds", BREAKER_SECONDS_DEFAULT))
    if limit <= 0 or elapsed <= limit:
        return out
    record = {"ts": now_utc(), "component": component, "seconds": round(elapsed, 3),
              "query_hash": hashlib.sha256(
                  str(query or "").encode("utf-8")).hexdigest()[:12]}
    try:
        path = Path(root) / BREAKER_AUDIT[0] / BREAKER_AUDIT[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:      # 断路器审计写不进去 → 出声（静默清零）
        fish_common.warn_derived(conn, "breaker_audit", exc)
    return []
