#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排序：`Ranker` 协议 + 默认实现（原始对话语料层 · T03 · 设计 §6/D07）。

为什么排序要能换（D07）
---------------------
"哪条原话最该被递出去"是研究问题，不是工程问题：BM25 只是**当下**最好的起点。
把 BM25 写进 SQL 的后果是——换算法要改调用面（CLI / MCP / 推送三处都要动），
于是没人愿意换。所以这里定一个极窄的协议：

    rank(cands, q, ctx) -> [(pointer, score, why), ...]

`cands` 是**已经过滤好**的段行（过滤在 SQL 里下推，见 §6"过滤先于排序"），
`why` 是给人看的解释（`K2/K8` 复盘靠它，没有它就只能猜"为什么推了这条"）。

默认公式（设计 §6 逐字）
    score = 0.6 × rank_norm(bm25) + 0.2 × 命中词比 + 0.2 × recency
    recency = exp(-age_days / 180)

`rank_norm` 为什么要在**本批候选内**归一：FTS5 的 `bm25()` 没有绝对尺度（它随
查询词、语料规模漂），跨查询比较毫无意义；批内 min-max 归一让"这一批里谁更强"
变成一个稳定的读数。

切词单源（D08/D09）
-----------------
查询表达式**不自己切**：token 由 `fish_common.index_tokens` 给（与索引侧
`fts_text` 同一套 bigram），本模块只负责"把 token 拼成 FTS5 能吃的 MATCH 表达式"。
`OR` 连接是刻意的：召回优先，精度交给排序——用 `AND` 时多打一个字就 0 结果，
而用户不会知道自己少打了一个字。
"""
from __future__ import annotations

import datetime
import math
from typing import Protocol, runtime_checkable

import fish_common

# 默认权重（0.6 + 0.2 + 0.2 = 1.0，所以 score 天然落在 [0, 1]：
# 推送门槛 `min_relevance` 才能直接拿它比，不用再猜量纲）。
W_RANK = 0.6
W_TERMS = 0.2
W_RECENCY = 0.2
RECENCY_HALF_LIFE_DAYS = 180.0

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


class RankerError(ValueError):
    """排序层的参数错误（未知 ranker / 注册名冲突）。**不许静默回落默认算法**。"""


@runtime_checkable
class Ranker(Protocol):
    """排序协议（设计 §3 类图）。`name` 用于回执声明"这次是谁排的"。"""

    name: str

    def rank(self, cands: list, q: str, ctx: dict) -> list:
        """返回 `[(pointer, score, why), ...]`，按分数降序。"""
        ...


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def parse_utc(text):
    """`2026-10-27T04:06:43Z` → datetime；解析失败返回 None（不猜、不抛）。"""
    if not text:
        return None
    try:
        return datetime.datetime.strptime(str(text), _UTC_FMT).replace(
            tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_days(occurred_at, now) -> float:
    """段的年龄（天）。时间缺失/坏 → 0.0（当作"刚发生"，不比假装很旧更糟）。"""
    stamp = parse_utc(occurred_at)
    if stamp is None:
        return 0.0
    delta = (now - stamp).total_seconds() / 86400.0
    return max(0.0, delta)


def age_text(age_days: float) -> str:
    """`why` 里的人类读数（"14 天前" / "今天"）。"""
    if age_days < 1:
        return "今天"
    if age_days < 30:
        return "%d 天前" % int(age_days)
    if age_days < 365:
        return "%d 个月前" % max(1, int(age_days // 30))
    return "%.1f 年前" % (age_days / 365.0)


def rank_norm(values) -> list:
    """批内 min-max 归一；退化解（全相等 / 只有一个）→ 全部 1.0。

    FTS5 的 `bm25()` **越小越相关**（它是负数），所以归一后 `1.0` = 本批最强。
    为什么退化批给 1.0 而不是 0.0：只有一条命中时，"它是最相关的"成立；
    给 0 会让单结果查询的分数凭空掉 0.6，推送门槛随即误杀。
    """
    vals = [float(v or 0.0) for v in values]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [1.0 for _ in vals]
    return [(hi - v) / (hi - lo) for v in vals]


def _doc_tokens(text) -> set:
    return set(fish_common.index_tokens(text or ""))


class DefaultRanker:
    """BM25 + 命中词比 + 时间衰减（设计 §6 的默认实现，权重视为契约）。"""

    name = "default"

    def __init__(self, *, w_rank=W_RANK, w_terms=W_TERMS, w_recency=W_RECENCY,
                 half_life_days=RECENCY_HALF_LIFE_DAYS):
        total = float(w_rank) + float(w_terms) + float(w_recency)
        if abs(total - 1.0) > 1e-9:
            raise RankerError("三个权重必须和为 1（score 要落在 [0,1]），收到和 %r" % total)
        self.w_rank = float(w_rank)
        self.w_terms = float(w_terms)
        self.w_recency = float(w_recency)
        self.half_life_days = float(half_life_days) or RECENCY_HALF_LIFE_DAYS

    def rank(self, cands, q, ctx) -> list:
        cands = list(cands or [])
        if not cands:
            return []
        ctx = ctx or {}
        terms = [t for t in (ctx.get("terms") or []) if t]
        project = ctx.get("project") or ""
        now = ctx.get("now") or utc_now()
        norms = rank_norm([c.get("bm25") for c in cands])
        scored = []
        for cand, norm in zip(cands, norms):
            doc = _doc_tokens(cand.get("text"))
            matched = sum(1 for term in terms if term in doc)
            ratio = (matched / float(len(terms))) if terms else 0.0
            age = _age_days(cand.get("occurred_at"), now)
            recency = math.exp(-age / self.half_life_days)
            score = (self.w_rank * norm + self.w_terms * ratio
                     + self.w_recency * recency)
            why = []
            if terms:
                why.append("命中 %d/%d 个词" % (matched, len(terms)))
            if project and (cand.get("project") or "") == project:
                why.append("同项目")
            why.append(age_text(age))
            scored.append((cand["pointer"], round(score, 6), why))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored


# 注册表（D07）。值是**工厂**而不是实例：`register_ranker` 让实验算法可以带参数。
RANKERS = {"default": DefaultRanker}


def register_ranker(name, factory) -> None:
    """登记一个算法。重名**直接报错**（静默覆盖会让"我换的算法没生效"无从查起）。"""
    key = (name or "").strip().lower()
    if not key:
        raise RankerError("ranker 名字不能为空")
    if not callable(factory):
        raise RankerError("ranker 工厂必须可调用：%r" % (factory,))
    if key in RANKERS:
        raise RankerError("ranker %r 已登记（要替换请先显式改注册表）" % key)
    RANKERS[key] = factory


def get_ranker(name="default") -> Ranker:
    """按名字取实例；**未登记 → ValueError**（设计 §6：不许静默回落默认）。"""
    key = (name or "default").strip().lower()
    factory = RANKERS.get(key)
    if factory is None:
        raise RankerError("未知 ranker：%r（已登记：%s）"
                          % (name, ", ".join(sorted(RANKERS))))
    instance = factory() if isinstance(factory, type) else factory()
    if not isinstance(instance, Ranker):
        raise RankerError("ranker %r 的产物不符合协议（缺 name/rank）" % key)
    return instance


def match_expr(terms) -> "str | None":
    """token 列表 → FTS5 MATCH 表达式（每个 token 加双引号，OR 连接）。

    为什么必须加引号：不加的话 `CR-12` 会被 FTS5 当成"列 CR 含 12"，
    直接抛 `no such column: 12`（ADR-005 §背景 1，2026-10-22 实测）。
    为什么用 OR：召回优先（见模块头）。
    """
    tokens = [str(t).strip() for t in (terms or ()) if str(t or "").strip()]
    if not tokens:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
