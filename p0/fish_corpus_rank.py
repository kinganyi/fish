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

两阶段共用一份特征定义（T03a · 2026-10-27 R2 修订）
------------------------------------------------
阶段一（本模块 `DefaultRanker`）：BM25 + 命中词比 + 时间衰减，产出**首阶段分**；
阶段二（`fish_push.rerank_candidates`，T03b）：六特征加权重排（34 号 §3.3）。

两个阶段共享的是**常量与刻度定义**（`FEATURE_NAMES` / `FEATURE_WEIGHTS` /
`FRESHNESS_HALF_LIFE_DAYS` 与下面这组纯函数）——**不是同一个公式**：
`DefaultRanker` 仍然自己算 `recency = exp(-age / half_life)`（首阶段问的是"这一批里
谁更近"，与"这条内容多新"同源同刻度，但调用点不同、将来可能分叉；写清楚比假装
共用一份更诚实）。第二阶段才是 `combine_features`（分项和 == 总分，明细可审计）。

## 两个"跨阶段"字段的合同（T03a R2，给 T03b 的接口）

  · `score_stage`：候选此刻处于哪个阶段。
    `SCORE_STAGE_FIRST="first_phase"` 表示 `score` 是**阶段一**的数
    （`fish.py._cand` 的 `(benefit − cost) × weight`），**不是** `score_breakdown`
    四项的加权和——两者量纲不同，禁止互相验算（34 §3.5 的 R3 只在重排**之后**成立）。
    T03b 重排后必须**原子替换** `score` 与 `score_breakdown`，把 `score_stage` 置
    `SCORE_STAGE_RERANK`，并把 `rerank`（T03a 恒 `None`）写成重排器的名字
    ——"这是谁排的"必须能查，否则同一个分数有两种含义。
  · `kind_class`：合流池的语义类别（`KIND_CLASSES` 三类）。`semantic_kind(cand)`
    是**唯一**归一入口：先认 `kind_class`，再按 `kind` 兜底（`conv_related`→corpus，
    `kb`/`kb_related`→kb，其余→mem）。未知 kind 归 mem 且**不抛**——合流池里
    "多一类"比"整批炸"轻，但要能记账（`notes` 参数）。
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


# ------------------------------------------------------------------ 六特征（T03a · 34 号 §3.3/§3.4）
#
# 为什么特征定义必须放在**这一层**而不是 `fish_push.py`：第一阶段（BM25 + 命中词比 +
# 时间衰减）与第二阶段（六特征加权重排）如果各写一份特征名，改名时必然漏一处，症状是
# "权重调了但分数没动"。所以名字与默认权重只在这里定义**一次**，并且每个特征都是
# **纯函数**（不摸库、不留全局状态）：
#
#     fish.py / fish_push.py   —— 负责取数（access_count / links / push_log）
#     fish_corpus_rank.py      —— 只负责"数值 → 0..1 的刻度"，可单测、可复算
#
# 刻度为什么全在 [0,1]：只有同一刻度上，`FEATURE_WEIGHTS` 才是"可解释的百分比"
# （0.45 的 relevance 到底占几分，取决于它和别的特征是不是同一量纲）。唯一例外是
# `dup_penalty` 的权重为负——负号放在**权重**上，而不是把读数做成负数，这样
# `score_breakdown` 里每一项都还能单独看懂。

FEATURE_NAMES = ("relevance", "freshness", "access_ratio",
                 "project_match", "link_strength", "dup_penalty")

# 默认权重（34 号 §3.3 逐字）：先给默认值，后续用真实采纳率调（可调/可回滚）。
FEATURE_WEIGHTS = {"relevance": 0.45, "freshness": 0.15, "access_ratio": 0.10,
                   "project_match": 0.15, "link_strength": 0.10, "dup_penalty": -0.30}

# 时间衰减：与第一阶段 `recency` **同一个半衰期**。两处若各取一个数，"新鲜度"在
# 第一阶段和第二阶段就是两个词，读者没法把 score 的差归因到哪一个。
FRESHNESS_HALF_LIFE_DAYS = RECENCY_HALF_LIFE_DAYS
DUP_WINDOW_HOURS = 24                  # 去重窗口（与 `push_log` 的 24h 口径同一个数）
DUP_PENALTY_COUNT_CAP = 5              # 推第 6 次不再加重：0..1 的线性刻度要有个头
PROJECT_MATCH_SAME = 1.0
PROJECT_MATCH_ALIAS = 0.5
PROJECT_MATCH_CROSS = 0.0

# ---------------------------------------------------------------- 跨阶段合同（T03a R2）
#
# `score_stage`：候选的分处于哪个阶段。两个名字在这里定义**一次**，`fish_corpus.py`
# 与 T03b 都不许再写字面量（两份字面量的症状是"阶段标错但分数看着对"）。
SCORE_STAGE_FIRST = "first_phase"      # 阶段一：`_cand` 的 (benefit−cost)×weight
SCORE_STAGE_RERANK = "rerank"          # 阶段二：六特征加权（T03b 才实现）
SCORE_STAGES = (SCORE_STAGE_FIRST, SCORE_STAGE_RERANK)

# 语义层类别（合流池分三类）。`kind` 是**语料/记忆侧的既有字段**（`conv_related`、
# `rule`、…），`kind_class` 才是合流侧认的类别；两者不许打架（见 `semantic_kind`）。
KIND_CLASS_MEM = "mem"
KIND_CLASS_KB = "kb"
KIND_CLASS_CORPUS = "corpus"
KIND_CLASSES = (KIND_CLASS_MEM, KIND_CLASS_KB, KIND_CLASS_CORPUS)
CORPUS_KIND = "conv_related"           # 语料候选的 `kind`（`test_corpus_push` 锁死）
KB_KINDS = ("kb", "kb_related")        # 记忆侧两种 kb 形态都归 kb 类

# 新鲜度分档阈值：与 `fish.memory_freshness` 的 0.7 / 0.35 **同一组数**（那边是函数
# 内的字面量，`fish.py` 不在本批写作用域，所以只能在这里再抄一次常量并加判据钉住）。
FRESHNESS_BAND_FRESH = 0.7
FRESHNESS_BAND_AGING = 0.35


def _number(value, what) -> float:
    """取值必须是数字（`bool` 不算：`True` 当计数是调用方的 bug，不是"1 次"）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RankerError("%s 必须是数字，收到 %r" % (what, value))
    return float(value)


def _as_datetime(value):
    """`occurred_at`（UTC 串 / datetime）→ 带时区的 datetime；坏值 → None（不猜）。"""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    return parse_utc(value)


def freshness_factor(occurred_at, now=None,
                     half_life_days=FRESHNESS_HALF_LIFE_DAYS) -> float:
    """时间衰减因子 `exp(-age_days / half_life_days)`，落在 (0, 1]。

    时间缺失/坏 → **1.0**（与 `_age_days` 同口径：把"没记时间"当成"刚发生"，而不是
    当成"很旧"）。反过来会给那些没写 `occurred_at` 的段一个永久折扣，而它们并不是
    过时，只是没记。

    这是**可解释观测值**，不是第二次折扣：`fish.py._cand` 收到的 `freshness` 仍是
    `None`（设计 §6.5 的"避免双重折扣"）。
    """
    half = _number(half_life_days, "half_life_days")
    if half <= 0.0:
        raise RankerError("half_life_days 必须 > 0（收到 %r）" % (half_life_days,))
    stamp = _as_datetime(occurred_at)
    if stamp is None:
        return 1.0
    now_dt = _as_datetime(now) or utc_now()
    age_days = max(0.0, (now_dt - stamp).total_seconds() / 86400.0)
    return math.exp(-age_days / half)


def freshness_band(factor) -> str:
    """新鲜度因子 → 档位：`fresh`(≥0.7) / `aging`(≥0.35) / `stale`(<0.35)。

    阈值与 `fish.memory_freshness` **同一组数**（那边写在函数体里；`fish.py` 不在
    本批写作用域，所以这里只能抄常量 + 用判据钉住两边一致的口径）。语料侧不产生
    `unknown` 档：语料段一定有 `occurred_at`，读不出时按 1.0 计（"没记时间"≠"很旧"）。
    """
    value = _number(factor, "freshness factor")
    if value >= FRESHNESS_BAND_FRESH:
        return "fresh"
    if value >= FRESHNESS_BAND_AGING:
        return "aging"
    return "stale"


def freshness_reading(occurred_at, now=None,
                     half_life_days=FRESHNESS_HALF_LIFE_DAYS) -> dict:
    """新鲜度的**合流形态**：`{factor, band, age_days}`（键与 `fish.py._cand` 逐字同）。

    为什么必须是 dict 而不是 float（首轮 🟠 S4）：合流池里 mem/kb 候选的
    `freshness` 是 `_cand` 写的 dict，语料候选写 float 时，下游 `cand["freshness"]`
    `["factor"]` 一半抛 `TypeError`、一半静默算错——而这只在两类候选同池时才出现，
    单类判据全绿。所以"同池同型"是硬约束：三类候选要么是 dict、要么没有这个键。

    两个读数的分工（一次计算、两个出口，不是各算一遍）：

      · `score_breakdown["freshness"]` → **数值 factor**（明细要能直接相加/复算）；
      · 候选的 `freshness` 字段 → 本函数的 dict（解释用：哪一档、多少天）。
    """
    factor = round(freshness_factor(occurred_at, now=now, half_life_days=half_life_days), 6)
    stamp = _as_datetime(occurred_at)
    age_days = None
    if stamp is not None:
        now_dt = _as_datetime(now) or utc_now()
        age_days = round(max(0.0, (now_dt - stamp).total_seconds() / 86400.0), 1)
    return {"factor": factor, "band": freshness_band(factor), "age_days": age_days}


def semantic_kind(cand, *, notes=None) -> str:
    """候选 → 合流池的三类之一（`mem` / `kb` / `corpus`）。**唯一**归一入口。

    为什么要有它（首轮 🟠 S1）：`kind_class` 只有语料侧有，记忆侧的 `kind` 有五种
    （`rule`/`association`/`serendipity`/`situational`/`prompt_related`），所以"按
    `kind_class or kind` 分组"会得到 7 类——下游只能各自猜映射表，猜法一多就漂。

    判定顺序（先认已经归一过的字段，再按 `kind` 兜底）：

      1. `kind_class` 命中 `KIND_CLASSES` → 直接用它（**优先**：它就是为了不再猜而加的）；
      2. `kind == "conv_related"` → `corpus`；`kind in ("kb", "kb_related")` → `kb`；
      3. 其余（含空/未知/`None`）→ `mem`，**不抛**：合流池里"多一类记忆"比"整批
         候选炸掉"轻得多；但**要能记账**——传 `notes`（列表）时追加一条
         `("unknown_kind", kind)`，让"词表长出了新 kind"这件事可见。

    `kind` 与 `kind_class` 打架时以 `kind_class` 为准；候选层自己产的两个字段不许
    打架（`conv_related ↔ corpus`），这条不变量由 C1/C6 判据钉住。
    非 dict（误传 `None`/字符串）同样归 `mem`：归一函数不该成为合流池的新故障点
    ——"归错一类"能在池子里被看见，"整批抛异常"看不见。
    """
    table = cand if isinstance(cand, dict) else {}
    kind_class = str(table.get("kind_class") or "").strip()
    if kind_class in KIND_CLASSES:
        return kind_class
    kind = str(table.get("kind") or "").strip()
    if kind == CORPUS_KIND:
        return KIND_CLASS_CORPUS
    if kind in KB_KINDS:
        return KIND_CLASS_KB
    if notes is not None:
        notes.append(("unknown_kind", kind))
    return KIND_CLASS_MEM


def dup_penalty(count, *, cap=DUP_PENALTY_COUNT_CAP) -> float:
    """24h 内被推过几次 → 0..1 的**正向读数**（负向由权重表达）。

    为什么封顶 5：第 6 次和第 50 次对读者是一件事（"又是它"），继续线性增长只会让
    一次误推永久压死同一条记忆；而封顶后 `dup_penalty=1.0` 也有明确含义。
    负值按 0 处理（不抛）：次数是外部计数，宁可不罚也不误罚——但 `bool` 这类明显
    用错类型的输入仍要红（`_number` 拦）。
    """
    limit = _number(cap, "cap")
    if limit <= 0.0:
        raise RankerError("dup_penalty 的上限必须 > 0（收到 %r）" % (cap,))
    times = max(0.0, _number(count, "dup_penalty 的推送次数"))
    return min(times, limit) / limit


def _ratio(value, top, what) -> float:
    """`value / top` 夹到 [0,1]；`top <= 0` → 0.0（没有分母 = 没有读数）。"""
    numerator = max(0.0, _number(value, what))
    denominator = _number(top, "%s 的分母" % what)
    if denominator <= 0.0:
        return 0.0
    return max(0.0, min(numerator / denominator, 1.0))


def access_ratio(count, max_count) -> float:
    """被读次数 / 本批最大被读次数：0 = 从没被读过，1 = 本批最常被读。"""
    return _ratio(count, max_count, "access_ratio")


def link_strength(strength, max_strength) -> float:
    """与当前项目锚点的 links 权重和 / 本批最大权重和（归一化到 0..1）。"""
    return _ratio(strength, max_strength, "link_strength")


def _alias_groups(alias_group) -> list:
    """把 `alias_group` 归一成"若干个项目名集合"（两种写法都接受）。

      · 一组：`["anyi-studio", "anyi_studio"]`（互为别名）；
      · 多组：`[{"a", "b"}, ["c", "d"]]`。

    为什么容忍两种：别名组在库里有两种存法（一行一组 / 一张表），取数层不该为了
    "形状好看"再包一层转换（多一层就多一个漏掉的分支）。
    """
    if not alias_group:
        return []
    try:
        items = list(alias_group)
    except TypeError as exc:
        raise RankerError("alias_group 必须是可迭代的（收到 %r）" % (alias_group,)) from exc
    if all(isinstance(item, str) for item in items):
        names = {str(item).strip() for item in items if str(item).strip()}
        return [names] if names else []
    groups = []
    for item in items:
        if isinstance(item, str):
            name = str(item).strip()
            if name:
                groups.append({name})
            continue
        try:
            names = {str(name).strip() for name in item if str(name).strip()}
        except TypeError as exc:
            raise RankerError("alias_group 的元素必须是字符串或字符串集合（收到 %r）"
                              % (item,)) from exc
        if names:
            groups.append(names)
    return groups


def project_match(cand_project, current_project, alias_group=None) -> float:
    """项目亲和：同项目 1.0 / 同别名组 0.5 / 跨项目 0.0。

    任一侧为空 → 0.0：连"当前项目"都没有的时候，"同项目"不是一个可比较的命题
    （与 `fish.py._cand` 的 `elif not current_project: rel = 0.0` 同一条理由）。
    """
    cand = str(cand_project or "").strip()
    current = str(current_project or "").strip()
    if not cand or not current:
        return PROJECT_MATCH_CROSS
    if cand == current:
        return PROJECT_MATCH_SAME
    for group in _alias_groups(alias_group):
        if cand in group and current in group:
            return PROJECT_MATCH_ALIAS
    return PROJECT_MATCH_CROSS


def combine_features(features, weights=None) -> float:
    """分项 → 总分：`Σ weights[name] × features[name]`，**严格线性**（不许夹紧/取整）。

    为什么要在这里钉一条"和 == 分"：`score_breakdown` 的意义是"审计员能追到为什么
    这条被选中"（34 号 §3.4，HippoRAG 的可解释性依据）。一旦总分与分项对不上，
    读者就只能信总分、不信分项——那这份明细就白写了。

    两条口径：

      · `features` 可以只给一部分（缺的按 0）——T03a 的语料候选只有四项
        （`access_ratio`/`link_strength` 由 T03b 补）；
      · **不许给不认识的名字**（`features` 与 `weights` 两侧都查）：拼错一个特征名
        等于静默把它的权重丢掉，而这种错在"分数看起来差不多"的时候永远查不出来
        （`score_breakdown` 会多一个没人用的键；`weights` 里多一个键则整个特征白配）。

    调用方要保证总分与分项用的是**同一份数值**：先算分项（写进 breakdown）再算总分，
    否则四舍五入会让"和"差到 1e-7 量级（远超 1e-9 的容差）。

    ## 值域与"能不能当最终 score"（首轮 🟡 Y4）

    权重和不是 1：正权合计 0.95、负权 −0.30，所以值域是 **[−0.30, +0.95]**，
    **不做 clamp**——夹紧会把"负权特征的微小增量"抹成 0，看起来更整齐，实际是
    把 R4（把 `dup_penalty` 权重改 0 → 重复推送变多）这类判据做成永真。
    因此 `select_push_candidates` 的 `score > 0` 门槛在 T03b 接上后口径会变：
    "四项都中等"也可能落到 0 以下。这不是本函数能决定的，必须由 T03b 显式拍
    （要么保持 `score` 仍是阶段一的数、本函数只产 `rerank_score`，要么把门槛
    改成对 `rerank_score` 比较）。另：若 `score := combine_features(features)`，
    34 §3.5 的 R3（分项和 == 总分）就是**恒真复述**，不再有独立判别力——R3 的真
    判据要用**手算 oracle**（2~3 条硬编码期望值），而不是再调一遍本函数。
    """
    table = dict(FEATURE_WEIGHTS if weights is None else weights)
    stray = sorted(set(table) - set(FEATURE_NAMES))
    if stray:
        # 权重表里混进未登记的名字 = 那个特征永远算不进去（分数看起来"只是偏了点"）
        raise RankerError("combine_features 的权重里有未登记的特征：%s（只认 %s）"
                          % (", ".join(stray), ", ".join(FEATURE_NAMES)))
    given = {str(key): value for key, value in (features or {}).items()}
    unknown = sorted(set(given) - set(table))
    if unknown:
        raise RankerError("combine_features 收到未登记的特征：%s（只认 %s）"
                          % (", ".join(unknown), ", ".join(FEATURE_NAMES)))
    total = 0.0
    for name in FEATURE_NAMES:
        if name not in table:
            continue                            # 只算权重里声明过的特征
        value = given.get(name, 0.0)
        total += _number(table[name], "权重 %s" % name) * _number(
            0.0 if value is None else value, "特征 %s" % name)
    return total


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
