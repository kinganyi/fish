#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T03a 判据 · 语料候选与 Ranker 对齐（2026-10-27 · 54 号 §2.2 · **R2 收口轮**）。

## 这批判据在守什么

T03b 要把三类候选（mem / kb / corpus）合流到一个池子里重排。合流的前提是
**语料候选自己先说得清**：它是哪一类（`kind_class`，归一入口 `semantic_kind`）、
现在多少分（`score`）、这个分处在哪个阶段（`score_stage`）、这个分由哪几项构成
（`score_breakdown`）、新鲜度是什么形态（`freshness`，必须与 mem 侧同型）、
为什么是它（`reason`）。缺一样，合流侧就只能靠"猜"或"再算一遍"——而再算一遍
就是第二份打分公式。

| 判据 | 本文件 | 红了通常意味着 |
|---|---|---|
| C0 六特征纯函数（刻度/权重/分项和）+ 新鲜度同型读法 | `FeatureMathTest` | 特征定义源被绕开 / 读数与合流形态不同源 |
| C1 候选结构（类别/阶段/分/明细）+ `score` 不被改写 + **全局重复度** + **`freshness` 同型** | `CandidateShapeTest` | 合流侧拿不到明细 / 同名字段两种类型 / 负向特征永不生效 |
| C2 `get_ranker` 两遍完全一致 | `RankerDeterminismTest` | 随机性混进了第一阶段（无法复算） |
| C3 `register_ranker` 生效 + 未知名报错 | `RankerRegistryTest` | 换算法没生效 / 静默回落默认 |
| C4 门牌与既有锁同口径（**引用**既有断言，含 skipped） | `PointerContractTest` | `conv:` 形态漂了 / 内层用例被跳过却算通过 |
| C5 候选层 SQL ⊆ 裸检索（**结构闸，常开**）+ 真库读数（只记录）+ 合成 1k 墙钟兜底 | `CandidateSqlShapeTest` / `PerfTest` / `PerfSyntheticTest` | 候选层自己又查了一遍库 / 检索本身退化（后者归 T04） |
| C6 语义类归一（三类映射 + 未知归 mem 且不抛） | `SemanticKindTest` | "按 `kind_class or kind` 分三类"在代码里不成立 |
| C7 `score` 与 `score_breakdown` 解耦（阶段标记 + 独立手算 oracle） | `ScoreStageTest` | 两个阶段的数被画等号（T03b 会照错口径改分） |

## 三条纪律

1. **夹具复用不复制**：主库 + 语料库 + 真实 DSH 帧采集全在
   `test_corpus_push.PushCase` 里（`import` 它，不抄一份）；
2. **不另造锁**：C4 不复制"`conv:` 前缀"的断言形状，而是**直接跑**既有那条用例
   （`test_corpus_push.CandidateTest.test_candidate_is_produced`）——两份锁各写一遍
   的时候，改一处另一处照样绿；
3. **三态分明**：结构闸**常开**（不需要真库）；真库只出**读数留档**（不判定）；
   算不出真库就 `skipTest` 并写明"判不到"（**SKIP 不是 PASS**，报告里分开写）。

## R2 改了什么（首轮 🟠/QA F1–F4 的去处）

- **C5 的主闸从墙钟改成结构**：首轮"增量 < 20ms"与噪声同量级（QA F1 实测 −2.9…−125ms），
  现在断言"候选层执行的用户级 SQL ⊆ `search(count=False)` 的 SQL"——确定性、零噪声，
  M12（把 `count` 打开）在这条上必红；墙钟只留合成 1k 的 200ms 兜底；
- **真库绝对延迟不再判定**：54 号 C5 的"< 50ms"在本机**未达标**（裸检索自己就 5/8
  超线），根因在既有 FTS postings 扫描 → 记为 **T04 红项**，这里只打印读数；
- **C4 查 `result.skipped`**：内层用例被跳过 ≠ 通过（QA F6）。
"""
import collections
import datetime
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_rank as rank_mod  # noqa: E402
import fish_corpus_store as store  # noqa: E402
import test_corpus_push as push_fixture  # noqa: E402 —— 只复用夹具，不复制

# C1 的四项明细（`access_ratio`/`link_strength` 要摸库，T03a 只给纯函数，T03b 补）
BREAKDOWN_KEYS = ("relevance", "freshness", "project_match", "dup_penalty")
# `freshness` 的合流形态：与 `fish.py._cand` 写的 dict **逐键相同**（T03b 直接读）
FRESHNESS_KEYS = ("factor", "band", "age_days")
PERF_ENV = "FISH_CORPUS_PERF_DB"
NOW = datetime.datetime(2026, 10, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _stamp(days_ago: float) -> str:
    moment = NOW - datetime.timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ====================================================================== C0


class FeatureMathTest(unittest.TestCase):
    """C0：六特征纯函数（34 号 §3.3/§3.4 的刻度）+ 新鲜度的合流形态。

    为什么先测这一层：T03a 的全部价值就是"把特征定义收成一份"。如果这组函数的
    刻度不自洽（比如 `dup_penalty` 封顶 50 而不是 5），后面所有"权重是百分比"的
    说法都失真，而现象只是分数排序看起来"有点怪"——那种 bug 没人能定位。
    """

    def test_c0_feature_names_and_weights_are_the_designed_ones(self):
        self.assertEqual(rank_mod.FEATURE_NAMES,
                         ("relevance", "freshness", "access_ratio",
                          "project_match", "link_strength", "dup_penalty"))
        self.assertEqual(rank_mod.FEATURE_WEIGHTS,
                         {"relevance": 0.45, "freshness": 0.15, "access_ratio": 0.10,
                          "project_match": 0.15, "link_strength": 0.10,
                          "dup_penalty": -0.30})

    def test_c0_freshness_is_a_180_day_decay(self):
        half = rank_mod.FRESHNESS_HALF_LIFE_DAYS
        self.assertEqual(half, 180.0)                 # 与第一阶段 recency 同一个数
        self.assertEqual(rank_mod.freshness_factor(_stamp(0), now=NOW), 1.0)
        # 硬编码期望值（Y5：不拿被测函数自己复算自己）
        self.assertAlmostEqual(rank_mod.freshness_factor(_stamp(180), now=NOW),
                               0.36787944117144233, delta=1e-12)
        older = rank_mod.freshness_factor(_stamp(900), now=NOW)
        newer = rank_mod.freshness_factor(_stamp(90), now=NOW)
        self.assertGreater(newer, older)
        self.assertLess(older, 0.02)
        # 时间缺失/坏 → 1.0（"没记时间"不等于"很旧"，不给永久折扣）
        for broken in (None, "", "昨天", "2026-13-45T99:99:99Z"):
            self.assertEqual(rank_mod.freshness_factor(broken, now=NOW), 1.0)
        # 未来时间不产生 >1 的因子（时钟漂移不该变成加分项）
        self.assertEqual(rank_mod.freshness_factor(_stamp(-5), now=NOW), 1.0)

    def test_c0_freshness_rejects_bad_half_life(self):
        for bad in (0, -1, "180"):
            with self.assertRaises(rank_mod.RankerError):
                rank_mod.freshness_factor(_stamp(1), now=NOW, half_life_days=bad)

    def test_c0_freshness_reading_has_the_cand_shape(self):
        """B 的刻度侧：dict 形态与 `_cand` 逐键一致；缺时间 → 1.0/fresh/None。"""
        reading = rank_mod.freshness_reading(_stamp(180), now=NOW)
        self.assertIsInstance(reading, dict)
        self.assertEqual(tuple(reading), FRESHNESS_KEYS)
        # 180 天 → exp(-1) = 0.367879…（硬编码；不复调被测函数）
        self.assertAlmostEqual(reading["factor"], 0.36787944117144233, delta=1e-6)
        self.assertEqual(reading["band"], "aging")     # 0.368 ≥ 0.35 → aging
        self.assertAlmostEqual(reading["age_days"], 180.0, delta=0.1)
        # 没有时间戳：按"刚发生"计（factor=1.0），并且 age_days 是 None 而不是 0
        # —— 0 会被读成"就是今天"，而"没记"不是"很新"。
        self.assertEqual(rank_mod.freshness_reading(None, now=NOW),
                         {"factor": 1.0, "band": "fresh", "age_days": None})
        self.assertEqual(rank_mod.freshness_reading("昨天", now=NOW)["age_days"], None)

    def test_c0_freshness_band_thresholds(self):
        """阈值与 `fish.memory_freshness` 同组数（那边是字面量，这里钉住口径）。"""
        self.assertEqual(rank_mod.FRESHNESS_BAND_FRESH, 0.7)
        self.assertEqual(rank_mod.FRESHNESS_BAND_AGING, 0.35)
        self.assertEqual(rank_mod.freshness_band(1.0), "fresh")
        self.assertEqual(rank_mod.freshness_band(0.7), "fresh")       # 边界含在 fresh
        self.assertEqual(rank_mod.freshness_band(0.6999), "aging")
        self.assertEqual(rank_mod.freshness_band(0.35), "aging")      # 边界含在 aging
        self.assertEqual(rank_mod.freshness_band(0.3499), "stale")
        self.assertEqual(rank_mod.freshness_band(0.0), "stale")
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.freshness_band("0.5")                            # 类型错要响

    def test_c0_dup_penalty_caps_at_five_pushes(self):
        self.assertEqual(rank_mod.DUP_PENALTY_COUNT_CAP, 5)
        self.assertEqual(rank_mod.dup_penalty(0), 0.0)
        self.assertAlmostEqual(rank_mod.dup_penalty(1), 0.2, delta=1e-12)
        self.assertAlmostEqual(rank_mod.dup_penalty(2), 0.4, delta=1e-12)
        self.assertAlmostEqual(rank_mod.dup_penalty(5), 1.0, delta=1e-12)
        self.assertEqual(rank_mod.dup_penalty(6), 1.0)      # 封顶：第 6 次不再加重
        self.assertEqual(rank_mod.dup_penalty(500), 1.0)
        self.assertEqual(rank_mod.dup_penalty(-3), 0.0)     # 负计数当 0（不误罚）
        for bad in (True, None, "2"):
            with self.assertRaises(rank_mod.RankerError):
                rank_mod.dup_penalty(bad)

    def test_c0_access_ratio_and_link_strength_are_normalized(self):
        self.assertEqual(rank_mod.access_ratio(0, 10), 0.0)
        self.assertAlmostEqual(rank_mod.access_ratio(5, 10), 0.5, delta=1e-12)
        self.assertEqual(rank_mod.access_ratio(20, 10), 1.0)     # 夹到 1
        self.assertEqual(rank_mod.access_ratio(5, 0), 0.0)       # 没有分母 = 没有读数
        self.assertAlmostEqual(rank_mod.link_strength(3, 6), 0.5, delta=1e-12)
        self.assertEqual(rank_mod.link_strength(0, 0), 0.0)

    def test_c0_project_match_three_bands(self):
        self.assertEqual(rank_mod.project_match("PJ", "PJ"), 1.0)
        self.assertEqual(rank_mod.project_match("PJ", "OTHER"), 0.0)
        self.assertEqual(rank_mod.project_match("PJ", "PJ-旧名",
                                               alias_group=["PJ", "PJ-旧名"]), 0.5)
        # 多组形态：**同一组内**的两个名字互为别名
        self.assertEqual(rank_mod.project_match("PJ", "PJ-旧名",
                                               alias_group=[{"PJ", "PJ-旧名"}, {"X"}]), 0.5)
        # 只同组才给 0.5：分在两组里仍算跨项目
        self.assertEqual(rank_mod.project_match("PJ", "X",
                                               alias_group=[{"PJ", "Y"}, {"X"}]), 0.0)
        # 任一侧为空 → 0.0（"当前项目"都没有时，"同项目"不是一个命题）
        self.assertEqual(rank_mod.project_match("", "PJ"), 0.0)
        self.assertEqual(rank_mod.project_match("PJ", None), 0.0)
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.project_match("PJ", "X", alias_group=[1])

    def test_c0_combine_features_is_exactly_the_sum_of_parts(self):
        """分项和 == 总分（容差 1e-9）：明细可审计的前提。

        ⚠️ 这只对**阶段二**成立（`combine_features` 的四项加权）。候选的 `score`
        是阶段一的数，两者**不许**画等号（见 C7）——值域 [−0.30, +0.95]、不夹紧。
        """
        features = {"relevance": 0.62, "freshness": 0.75, "access_ratio": 0.4,
                    "project_match": 1.0, "link_strength": 0.25, "dup_penalty": 0.2}
        parts = {name: rank_mod.FEATURE_WEIGHTS[name] * features[name]
                 for name in rank_mod.FEATURE_NAMES}
        self.assertAlmostEqual(rank_mod.combine_features(features),
                               sum(parts.values()), delta=1e-9)
        self.assertAlmostEqual(rank_mod.combine_features(features),
                               0.45 * 0.62 + 0.15 * 0.75 + 0.10 * 0.4
                               + 0.15 * 1.0 + 0.10 * 0.25 - 0.30 * 0.2, delta=1e-12)
        # T03a 只有四项：缺的按 0，且不许抛（T03b 才有 access_ratio/link_strength）
        four = {name: features[name] for name in BREAKDOWN_KEYS}
        self.assertAlmostEqual(rank_mod.combine_features(four),
                               0.45 * 0.62 + 0.15 * 0.75 + 0.15 * 1.0 - 0.30 * 0.2,
                               delta=1e-12)
        # 权重可覆盖（"权重写进 config，可调可回滚"）
        self.assertAlmostEqual(rank_mod.combine_features({"relevance": 0.5},
                                                         {"relevance": 2.0}),
                               1.0, delta=1e-12)
        # 值域不是 0..1：负权让"四项都不低"也能落到 0 以下（T03b 的门槛口径要显式拍）
        low = {"relevance": 0.0, "freshness": 0.0, "project_match": 0.0,
               "dup_penalty": 1.0}
        self.assertAlmostEqual(rank_mod.combine_features(low), -0.30, delta=1e-12)
        # 未登记的特征名要响：拼错一个名字 = 静默丢掉它的权重
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.combine_features({"relevenace": 0.9})
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.combine_features({"relevance": "high"})
        # 权重表里混进未登记的名字同样要响（否则那个特征永远算不进去，只是"分数偏了"）
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.combine_features({"relevance": 0.5}, {"relevance": 0.45,
                                                           "relevenace": 0.15})


# ====================================================================== C1


class CandidateShapeTest(push_fixture.PushCase):
    """C1：候选结构字段齐全；`score` 不被改写；重复度是全局读数；新鲜度同型。"""

    CODE = "conv:dsh:3931d23f#seq=10"

    def _items(self, **kwargs):
        return fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            **kwargs)["items"]

    def test_c1_item_has_kind_class_score_and_breakdown(self):
        """没有 scorer 时也要是完整候选：`score` 占位到首阶段 relevance，明细齐全。"""
        self.write_session()
        self.ingest()
        res = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root)
        items = res["items"]
        self.assertEqual(len(items), 1, items)
        item = items[0]
        self.assertTrue(item["code"].startswith("conv:dsh:3931d23f#seq="), item["code"])
        self.assertEqual(item["kind_class"], "corpus")
        self.assertEqual(rank_mod.semantic_kind(item), item["kind_class"],
                         "类别归一与候选自报的 kind_class 必须一致")
        self.assertTrue(item["reason"])
        self.assertGreater(item["score"], 0.0)
        self.assertEqual(item["score"], item["relevance"],
                         "无 scorer 时 score 必须是首阶段 relevance 的占位")
        # ---- 阶段标记（C7 的字段面）----
        self.assertEqual(item["score_stage"], rank_mod.SCORE_STAGE_FIRST)
        self.assertIsNone(item["rerank"], "T03a 没有第二阶段：rerank 必须是 None")
        # ---- 新鲜度：**同型 dict** + 明细里留数值 factor ----
        self.assertIsInstance(item["freshness"], dict)
        self.assertEqual(tuple(item["freshness"]), FRESHNESS_KEYS)
        self.assertGreater(item["freshness"]["factor"], 0.0)
        self.assertLessEqual(item["freshness"]["factor"], 1.0)
        self.assertIn(item["freshness"]["band"], ("fresh", "aging", "stale"))
        self.assertAlmostEqual(item["score_breakdown"]["freshness"],
                               item["freshness"]["factor"], delta=1e-9,
                               msg="明细里的数值 factor 与 freshness 字段必须同源")
        self.assertEqual(sorted(item["score_breakdown"]), sorted(BREAKDOWN_KEYS))
        for key in BREAKDOWN_KEYS:
            self.assertIsInstance(item["score_breakdown"][key], (int, float), key)
        self.assertEqual(item["score_breakdown"]["relevance"], item["score"])
        self.assertEqual(item["score_breakdown"]["project_match"], 1.0,
                         "检索已按项目下推，明细里必须说'同项目'")
        self.assertEqual(item["score_breakdown"]["dup_penalty"], 0.0,
                         "没给 dup_counts 时不许编造推送次数")
        self.assertEqual(res["skipped"].get("dup_unknown"), 1,
                         "缺省（没有读数）也要记账：可选特征读不到不许静默")
        self.assertAlmostEqual(item["score_breakdown"]["freshness"],
                               rank_mod.freshness_factor(item["occurred_at"]),
                               delta=1e-5)

    def test_c1_scorer_path_keeps_kind_and_fills_fields(self):
        """`fish.py` 真实链路（`_cand` 打分）：`kind` 不变、新字段补齐。"""
        self.write_session()
        self.ingest()
        cands = self.candidates()
        self.assertTrue(cands)
        cand = cands[0]
        self.assertEqual(cand["kind"], "conv_related", "既有锁：kind 必须还是 conv_related")
        self.assertEqual(cand["kind_class"], "corpus")
        self.assertTrue(cand["code"].startswith("conv:dsh:3931d23f#seq="))
        self.assertGreater(cand["score"], 0.0)
        self.assertIsInstance(cand["freshness"], dict)
        self.assertEqual(tuple(cand["freshness"]), FRESHNESS_KEYS)
        self.assertLessEqual(cand["freshness"]["factor"], 1.0)
        self.assertEqual(sorted(cand["score_breakdown"]), sorted(BREAKDOWN_KEYS))
        self.assertEqual(cand["score_stage"], rank_mod.SCORE_STAGE_FIRST)
        self.assertIsNone(cand["rerank"])

    def test_c1_scorer_output_score_is_never_rewritten(self):
        """补字段≠改分：scorer 给的分与最终候选的分**逐字相等**。"""
        self.write_session()
        self.ingest()
        produced = []

        def scorer(item):
            """与 `fish.py._conv_push_candidates` 同一条接线（公式只有 `_cand` 一份）。"""
            cand = fish._cand(self.conn, item["code"], "conv_related",
                              title=item["title"], reason=item["reason"],
                              current_project=push_fixture.PROJECT, now=None,
                              relevance=item["relevance"], freshness=None,
                              interrupt=0.4)
            produced.append(dict(cand))
            return cand

        items = self._items(scorer=scorer)
        self.assertTrue(items)
        self.assertEqual(len(produced), 1)
        self.assertEqual(items[0]["score"], produced[0]["score"])
        # `relevance` 是 T04 就有的补齐（`_cand` 不产它，候选层补）；T03a 新增的是
        # 后五个键——**仅此五个**，多一个就说明有人顺手改了 `_cand` 的产物。
        self.assertEqual(set(items[0]) - set(produced[0]),
                         {"relevance", "kind_class", "score_breakdown", "freshness",
                          "score_stage", "rerank"},
                         "T03a 只许**新增**解释性字段，别的字段一个都不许动")
        self.assertEqual(items[0]["kind_class"], "corpus")

    def test_c1_dup_penalty_is_global_repetition(self):
        """A：全局 3 次 → 0.6；0 次 → 0.0；**本会话已推 → 根本不出**（挡/罚两条语义）。"""
        self.write_session()
        self.ingest()
        code = self.CODE
        item = self._items(dup_counts=lambda hours, now: {code: 3})[0]
        self.assertAlmostEqual(item["score_breakdown"]["dup_penalty"], 0.6, delta=1e-9)
        # 0 次：一条都没推过 → 不罚（也不再记 dup_unknown：读数拿到了）
        res0 = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            dup_counts=lambda hours, now: {code: 0})
        self.assertAlmostEqual(res0["items"][0]["score_breakdown"]["dup_penalty"],
                               0.0, delta=1e-12)
        self.assertNotIn("dup_unknown", res0["skipped"])
        # 封顶：5 / 6 / 50 次同分（刻度到头，误推一次不会永久压死同一条）
        for times in (5, 6, 50):
            got = self._items(dup_counts=lambda h, n, t=times: {code: t})[0]
            self.assertAlmostEqual(got["score_breakdown"]["dup_penalty"],
                                   min(times, 5) / 5.0, delta=1e-9, msg=times)
        # 本会话已推：由 `exclude`（= `recent_of` 的产物）**挡掉**，候选根本不出。
        # 这条与"降几分"是两条语义：挡是"别再给我看同一条"，罚是"它在池子里出现过很多次"。
        res_ex = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            exclude=[code], dup_counts=lambda hours, now: {code: 3})
        self.assertEqual(res_ex["items"], [])
        self.assertGreaterEqual(res_ex["skipped"].get("excluded", 0), 1, res_ex["skipped"])

    def test_c1_dup_penalty_counts_other_sessions_only(self):
        """口径（S3 修法）：只想要"**别的会话**推过没有" → 回调里排除本会话即可。

        两种口径量同一条候选、同一张 `push_log`：全局 3 次 → 0.6；排除本会话 → 0.0。
        为什么要测它：T03b 接线时若把"本会话刚推过"也算进重复度，`_silence_weight`
        （历史忽略率，按 code 跨会话统计）与这里就会对同一批推送**重复折扣**。
        """
        self.write_session()
        self.ingest()
        code = self.CODE
        for _ in range(3):                      # 本会话推了 3 次（主库 push_log）
            fish.append_push_log(self.conn, [code], channel="prompt",
                                 session_key=self.session_key, citizen_id=push_fixture.CID)

        def global_counts(hours, now):
            return dict(self.conn.execute(
                "SELECT code, COUNT(*) FROM push_log GROUP BY code").fetchall())

        def other_session_counts(hours, now):
            return dict(self.conn.execute(
                "SELECT code, COUNT(*) FROM push_log WHERE session_key<>? GROUP BY code",
                (self.session_key,)).fetchall())

        self.assertEqual(global_counts(24, NOW), {code: 3})
        self.assertEqual(other_session_counts(24, NOW), {})
        global_item = self._items(dup_counts=global_counts)[0]
        other_item = self._items(dup_counts=other_session_counts)[0]
        self.assertAlmostEqual(global_item["score_breakdown"]["dup_penalty"], 0.6,
                               delta=1e-9)
        self.assertAlmostEqual(other_item["score_breakdown"]["dup_penalty"], 0.0,
                               delta=1e-12)
        # 两条候选是同一个 code、同一批候选数据——差别只在"算不算本会话"
        self.assertEqual(global_item["code"], other_item["code"])

    def test_c1_dup_penalty_reads_the_injected_callback(self):
        self.write_session()
        self.ingest()
        seen = []

        def dup_counts(hours, now):
            seen.append((hours, now))
            return {self.CODE: 2}

        items = self._items(dup_counts=dup_counts)
        self.assertEqual(len(seen), 1, "dup_counts 必须被调用且只调一次")
        self.assertEqual(seen[0][0], fish_corpus.PUSH_DUP_WINDOW_HOURS)
        self.assertEqual(seen[0][0], 24)            # 与推送去重同一个窗口（有意共用）
        self.assertEqual(items[0]["code"], self.CODE)
        self.assertAlmostEqual(items[0]["score_breakdown"]["dup_penalty"], 0.4,
                               delta=1e-9)

    def test_c1_dup_counts_failure_is_counted_not_swallowed(self):
        """取数失败：按 0 处理继续出候选，但必须记 `dup_unknown`（不静默）。"""
        self.write_session()
        self.ingest()

        def boom(hours, now):
            raise RuntimeError("push_log 读失败")

        res = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            dup_counts=boom)
        self.assertEqual(res["skipped"].get("dup_unknown"), 1, res["skipped"])
        self.assertTrue(res["items"], "可选特征读不到不该让整批候选失败")
        self.assertEqual(res["items"][0]["score_breakdown"]["dup_penalty"], 0.0)
        # 集合形态的回包（既有 `recent_of` 就是集合）按"推过 1 次"算，不报 dup_unknown
        res2 = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            dup_counts=lambda hours, now: {self.CODE})
        self.assertNotIn("dup_unknown", res2["skipped"])
        self.assertAlmostEqual(res2["items"][0]["score_breakdown"]["dup_penalty"], 0.2,
                               delta=1e-9)
        # 字符串回包不算"集合形态"：按字符迭代会造出"每个字推过一次"的假读数
        res3 = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            dup_counts=lambda hours, now: self.CODE)
        self.assertIn("dup_unknown", res3["skipped"], res3["skipped"])
        self.assertEqual(res3["items"][0]["score_breakdown"]["dup_penalty"], 0.0)
        # 缺省（None）也记账：与"回调失败"同一条纪律
        res4 = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root)
        self.assertEqual(res4["skipped"].get("dup_unknown"), 1, res4["skipped"])

    def test_c1_conv_push_for_session_passes_dup_counts(self):
        """薄壳口要透传（否则 T03b 只能在 fish.py 里再拼一遍）。"""
        self.write_session()
        self.ingest()
        seen = []

        def scorer(item):
            return fish._cand(self.conn, item["code"], "conv_related",
                              title=item["title"], reason=item["reason"],
                              current_project=push_fixture.PROJECT, now=None,
                              relevance=item["relevance"], freshness=None,
                              interrupt=0.4)

        def dup_counts_of(hours, now):
            seen.append(hours)
            return {self.CODE: 4}

        items = fish_corpus.conv_push_for_session(
            root=self.root, query=push_fixture.QUERY, project=push_fixture.PROJECT,
            scorer=scorer, dup_counts_of=dup_counts_of)
        self.assertEqual(seen, [fish_corpus.PUSH_DUP_WINDOW_HOURS])
        self.assertTrue(items)
        self.assertEqual(items[0]["kind_class"], "corpus")
        self.assertAlmostEqual(items[0]["score_breakdown"]["dup_penalty"], 0.8, delta=1e-9)

    def test_c1_freshness_has_the_same_dict_shape_in_all_classes(self):
        """B：三类候选的 `freshness` **同型**（dict 或没有这个键），绝不许是 float。

        首轮 🟠 S4：corpus 侧写 float、mem/kb 侧写 dict，合流后 `cand["freshness"]
        ["factor"]` 会对一半候选抛 `TypeError`、对另一半静默算错——而且只在两类候选
        同时进池时才现形，单类判据全绿。所以这里连"池子里"一起量。
        """
        self.write_session()
        self.ingest()
        item = self._items()[0]
        self.assertIsInstance(item["freshness"], dict)
        self.assertEqual(tuple(item["freshness"]), FRESHNESS_KEYS)
        # mem 侧：真实 `_cand` 产出的形态（生产链路就是这样写进候选的）
        mem = fish._cand(self.conn, "MEM-T03A-R2", "rule",
                         title="跨项目撞车的判据在 overlap", reason="规则",
                         current_project=push_fixture.PROJECT,
                         freshness={"factor": 0.9, "band": "fresh", "age_days": 3.0})
        self.assertIsInstance(mem["freshness"], dict)
        self.assertEqual(tuple(mem["freshness"]), tuple(item["freshness"]))
        self.assertEqual(type(item["freshness"]), type(mem["freshness"]))
        # kb 侧：候选**没有**这个键（`_kb_push_candidates` 不写 freshness）→ `.get()` 为 None
        # 所以"同型"的判据是"dict 或 None"，而不是"必须都有 dict"。
        kb_shaped = {"code": "kb:安逸知识库/x.md", "kind": "kb"}
        self.assertEqual(rank_mod.semantic_kind(kb_shaped), "kb")
        self.assertIsNone(kb_shaped.get("freshness"), "kb 候选没有 freshness 键 → 按 None 处理")
        for cand in self.candidates():          # 真实链路（fish.py 打分后的语料候选）
            self.assertIn(type(cand.get("freshness")), (dict, type(None)), cand["code"])
            self.assertNotIsInstance(cand.get("freshness"), float, cand["code"])
        # 合流池整体：任何一类候选都不许出现 float 形态的 freshness
        pool = fish.build_push_candidates(self.conn, self.root, citizen_id=push_fixture.CID,
                                          project=push_fixture.PROJECT,
                                          session_key=self.session_key,
                                          timing="prompt", prompt=push_fixture.QUERY,
                                          limit=8)
        self.assertTrue(pool, "提示词时机的池子不该是空的（否则这条判据在空转）")
        for cand in pool:
            self.assertNotIsInstance(cand.get("freshness"), float, cand.get("code"))
            if isinstance(cand.get("freshness"), dict):
                self.assertEqual(tuple(cand["freshness"]), FRESHNESS_KEYS, cand["code"])


# ====================================================================== C2


class _ScriptedRanker:
    """确定性自定义 ranker：分数只由 pointer 长度决定（便于两遍比对）。"""

    name = "t03a-scripted"

    def rank(self, cands, q, ctx):
        rows = [(c["pointer"], round(0.001 * len(c["pointer"]), 6), ["scripted"])
                for c in cands]
        rows.sort(key=lambda item: (-item[1], item[0]))
        return rows


class RankerDeterminismTest(unittest.TestCase):
    """C2：同一候选集、同一上下文 → 两遍**逐字**一致（第一阶段不许有随机性）。

    为什么这条比"分数对不对"更重要：第二阶段（T03b）要拿第一阶段的分数重排，
    第一阶段一旦带随机性，同一句话两次推送给出不同候选，复盘时**无法归因**
    （"上次为什么推它"永远答不出）。
    """

    @staticmethod
    def _cands():
        return [
            {"pointer": "log:dsh:sa#seq=1", "bm25": -8.5,
             "text": "撞车判据 先比目录前缀", "occurred_at": _stamp(3), "project": "PJ"},
            {"pointer": "log:dsh:sb#seq=2", "bm25": -2.0,
             "text": "撞车判据 再比文件后缀", "occurred_at": _stamp(40), "project": "PJ"},
            {"pointer": "log:dsh:sc#seq=3", "bm25": -5.5,
             "text": "撞车 判据 声明窗口", "occurred_at": _stamp(400), "project": "X"},
        ]

    def _ctx(self):
        return {"terms": ["撞车", "判据"], "project": "PJ", "now": NOW}

    def test_c2_default_ranker_is_deterministic(self):
        first = rank_mod.get_ranker("default").rank(self._cands(), "撞车判据", self._ctx())
        second = rank_mod.get_ranker("default").rank(self._cands(), "撞车判据", self._ctx())
        self.assertEqual(len(first), 3)
        self.assertEqual(first, second, "同一候选集两遍结果不一致（有随机性混进来了）")
        self.assertEqual([row[1] for row in first],
                         sorted((row[1] for row in first), reverse=True))

    def test_c2_custom_ranker_is_deterministic_and_used(self):
        if _ScriptedRanker.name not in rank_mod.RANKERS:
            rank_mod.register_ranker(_ScriptedRanker.name, _ScriptedRanker)
        self.addCleanup(rank_mod.RANKERS.pop, _ScriptedRanker.name, None)
        ranker = rank_mod.get_ranker(_ScriptedRanker.name)
        first = ranker.rank(self._cands(), "撞车判据", self._ctx())
        second = ranker.rank(self._cands(), "撞车判据", self._ctx())
        self.assertEqual(first, second)
        # 与默认实现确实不同（证明"换算法"真的换掉了，而不是回落到 default）
        self.assertNotEqual(first, rank_mod.get_ranker("default").rank(
            self._cands(), "撞车判据", self._ctx()))
        # 时间因素被自定义实现忽略时，旧段也能拿同分——这正是"可换"的意义
        self.assertEqual(first[-1][1], round(0.001 * len("log:dsh:sc#seq=3"), 6))


# ====================================================================== C3


class RankerRegistryTest(push_fixture.PushCase):
    """C3：`register_ranker` 注册即生效；未知 ranker **响亮失败**（不许回落默认）。"""

    NAME = "t03a-scripted"

    def setUp(self):
        super().setUp()
        self.write_session()
        self.ingest()
        rank_mod.RANKERS.pop(self.NAME, None)
        self.addCleanup(rank_mod.RANKERS.pop, self.NAME, None)

    def test_c3_hot_swap_takes_effect_without_restart(self):
        before = fish_corpus.search(push_fixture.QUERY,
                                    filters={"project": [push_fixture.PROJECT]},
                                    root=self.root, ranker="default")
        self.assertEqual(before["meta"]["ranker"], "default")
        self.assertTrue(before["hits"])
        rank_mod.register_ranker(self.NAME, _ScriptedRanker)     # 注册发生在检索之后
        after = fish_corpus.search(push_fixture.QUERY,
                                   filters={"project": [push_fixture.PROJECT]},
                                   root=self.root, ranker=self.NAME)
        self.assertEqual(after["meta"]["ranker"], self.NAME)
        self.assertEqual(rank_mod.get_ranker(self.NAME).name, self.NAME)
        self.assertEqual([hit["score"] for hit in after["hits"]],
                         [round(0.001 * len(hit["pointer"]), 6) for hit in after["hits"]])
        self.assertNotEqual([hit["score"] for hit in before["hits"]],
                            [hit["score"] for hit in after["hits"]])

    def test_c3_unknown_ranker_is_loud(self):
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.get_ranker("no-such-ranker-t03a")
        with self.assertRaises(rank_mod.RankerError):
            fish_corpus.search(push_fixture.QUERY, root=self.root,
                               ranker="no-such-ranker-t03a")
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.register_ranker("", _ScriptedRanker)        # 空名字
        with self.assertRaises(rank_mod.RankerError):
            rank_mod.register_ranker(self.NAME, "not-callable")  # 不是工厂


# ====================================================================== C4


class PointerContractTest(push_fixture.PushCase):
    """C4：门牌只有 `store.push_pointer` 一个生成点，形态与既有锁一致。"""

    def test_c4_code_round_trips_through_push_pointer(self):
        self.write_session()
        self.ingest()
        item = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT,
            root=self.root)["items"][0]
        parsed = store.parse_pointer(item["pointer"])          # canonical `log:` 形态
        self.assertEqual(item["code"],
                         store.push_pointer(item["host"], item["session_id"],
                                            parsed["loc"], parsed["part"]))
        self.assertEqual(item["code"],
                         "conv:dsh:%s#%s" % (store.sid8(self.sid), parsed["loc"]))
        self.assertRegex(item["code"], r"^conv:dsh:[0-9a-f]{8}#(seq|line)=\d+$")
        # 同一门牌能原样读回（C-P0-6 的闭环第一格）
        self.assertEqual(fish_corpus.read(item["code"], root=self.root)["code"],
                         item["code"])

    def test_c4_existing_lock_still_passes(self):
        """**引用**既有锁，不复制断言形状（`test_corpus_push.CandidateTest`）。

        QA F6：只查 `failures`/`errors` 不看 `skipped` 时，内层用例一旦被跳过
        （`testsRun=1` 但什么也没跑）就会假绿——"跳过"不是"通过"。
        """
        case = push_fixture.CandidateTest("test_candidate_is_produced")
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual([str(test) for test, _why in result.skipped], [],
                         "内层用例被跳过 = 没测到（SKIP 不是 PASS）")
        self.assertEqual([test.id() for test, _ in result.failures], [],
                         [trace for _, trace in result.failures])
        self.assertEqual([test.id() for test, _ in result.errors], [],
                         [trace for _, trace in result.errors])


# ====================================================================== C5


_INTERNAL_SQL_PREFIX = "-- "      # sqlite 把 FTS 等内部语句标成注释前缀
_NUMBER_RE = re.compile(r"\b\d+\b")
_LITERAL_RE = re.compile(r"'[^']*'")


def _sql_shape(sql) -> str:
    """语句形态：压空白 + 抹掉字面量（允许参数不同，但结构必须一样）。"""
    text = " ".join(str(sql).split())
    text = _LITERAL_RE.sub("'#'", text)
    return _NUMBER_RE.sub("#", text)


def _trace_sql(conn, fn) -> tuple:
    """收集一段代码执行过的**用户级** SQL（内部语句带 `-- ` 前缀，滤掉）。"""
    seen = []

    def _cb(sql):
        seen.append(_sql_shape(sql))

    conn.set_trace_callback(_cb)
    try:
        out = fn()
    finally:
        conn.set_trace_callback(None)
    return [sql for sql in seen if not sql.startswith(_INTERNAL_SQL_PREFIX)], out


def assert_candidate_sql_is_subset(case, conn, query, project) -> dict:
    """候选层的用户级 SQL 必须 ⊆ `search(count=False)` 的 SQL —— **确定性**判据。

    为什么不用墙钟（QA F1 / 首轮 🟠 S5）：同一份代码、同一查询，候选层相对裸检索的
    增量实测在 −2.9…−125.4ms 之间摆动，阈值与噪声同量级，"M12 被抓住"是抽签结果。
    而这里问的本来就是**结构**问题——"候选层有没有自己又查一遍库"——一次语句级取证
    就说清了：多一条 `COUNT(*)`、多一次 `MATCH`、多一次不带 `MATCH` 的整表扫描，都是
    候选层自己又去摸了库，与机器快慢无关。

    返回候选层的回包（调用方可继续断言"命中非空"，避免判据空转）。
    """
    def _conv():
        return fish_corpus.conv_push_candidates(query=query, project=project, conn=conn)

    def _baseline():
        return fish_corpus.search(
            query, filters={"project": [project], "role": [store.ROLE_ASSISTANT]},
            limit=fish_corpus.PUSH_SEARCH_LIMIT, conn=conn, record=False, count=False)

    warm = _conv()                 # 热身：首次调用会多读 schema-cookie / FTS 配置
    _baseline()
    cand_sql, conv_res = _trace_sql(conn, _conv)
    base_sql, base_res = _trace_sql(conn, _baseline)
    case.assertTrue(warm["items"], "候选为空：这条判据会变成空转（先修夹具/门槛，别假绿）")
    case.assertTrue(base_res["hits"], "基线检索没有命中：结构判据无从比较")
    case.assertTrue(cand_sql, "一条 SQL 都没采到：trace 没挂上")
    extra = sorted((collections.Counter(cand_sql) - collections.Counter(base_sql)).elements())
    print("[C5] 候选层用户级 SQL %d 条 / 裸检索 %d 条；候选层多出 %d 条"
          % (len(cand_sql), len(base_sql), len(extra)))
    case.assertEqual(extra, [],
                     "候选层执行了裸检索没有的语句（疑似自己又查了一遍库）：%r" % (extra,))
    for sql in cand_sql:
        upper = sql.upper()
        case.assertNotIn("COUNT(", upper,
                         "候选层不许统计全库命中数（多一次 COUNT = 多扫一遍 FTS）：%s" % sql)
        case.assertFalse("FROM CORPUS_SEGMENT" in upper and "MATCH" not in upper,
                         "候选层出现不带 MATCH 的整表扫描：%s" % sql)
    case.assertLessEqual(sum(sql.upper().count("MATCH") for sql in cand_sql),
                         sum(sql.upper().count("MATCH") for sql in base_sql),
                         "候选层比裸检索多扫了一遍 FTS（额外 MATCH）")
    return conv_res


class CandidateSqlShapeTest(push_fixture.PushCase):
    """C5 结构闸（**常开**，不依赖真库、不看钟）：候选层不许自己再查一遍库。

    为什么把它放在合成夹具里：真库那条（`PerfTest`）要人工设环境变量，CI 里等于
    没有闸（QA Y7）。而"候选层多查库"是**结构**性质，1000 段的合成夹具一样判得出来
    ——多出来的语句与库大小无关。
    """

    SEGMENTS = 1000

    def test_c5_candidate_layer_sql_is_subset_of_search(self):
        sid = "9f0a0006-eeee-4de8-87b0-000000000006"
        events = [push_fixture.ev_session(sid),
                  push_fixture.ev_user(9, push_fixture.QUERY)]
        events += [push_fixture.ev_assistant(10 + i,
                                             push_fixture.LONG + "（第 %d 条）" % i)
                   for i in range(self.SEGMENTS)]
        self.write_session(sid, events)
        self.ingest()
        conn = store.connect(store.db_path(self.root), readonly=False)
        try:
            res = assert_candidate_sql_is_subset(self, conn, push_fixture.QUERY,
                                                 push_fixture.PROJECT)
        finally:
            conn.close()
        self.assertTrue(res["items"])


def _pick_query(conn, min_chars=40000):
    """从真库里挑一个**有命中的** (project, query)：取最富项目里最长段的中段汉字。

    为什么要从库里取而不是写死一个词：语料是活的（每天都在采），写死的词会随
    语料漂移变成"查不到"——那时测的是"查询词过时了"，不是"候选生成了多久"。
    """
    row = conn.execute(
        "SELECT project, COUNT(*) c FROM corpus_session WHERE project<>''"
        " GROUP BY project ORDER BY c DESC LIMIT 1").fetchone()
    if row is None:
        return None
    project = row[0]
    seg = conn.execute(
        "SELECT s.text FROM corpus_segment s"
        " JOIN corpus_session ss ON ss.host=s.host AND ss.session_id=s.session_id"
        " WHERE ss.project=? AND s.chars>=120 AND s.shadowed=0 AND s.role='assistant'"
        " ORDER BY s.chars DESC LIMIT 1", (project,)).fetchone()
    if seg is None or not seg[0]:
        return None
    run = re.search(r"[\u4e00-\u9fff]{8,}", seg[0])
    if run is None:
        return None
    body = run.group(0)
    middle = len(body) // 2
    return project, body[max(0, middle - 2):middle + 2]


class PerfTest(unittest.TestCase):
    """C5a：**真库**（4 万+ 段）上的一条闸 + 一组读数。

    ## 为什么墙上时钟不再是闸（QA F1 / 首轮 🟠 S5 的收口）

    同一份代码、同一查询，候选层相对裸检索的增量实测 **−2.9 … −125.4ms**（交错测量
    8 个查询全为负）；阈值 20ms 与噪声同量级，判据的正负由"谁后测"和机器负载决定。
    而"候选层不许自己再查一遍库"本来就是**结构**问题 —— 用语句级取证判，零噪声。

      ① **主闸（确定性）**：候选层执行的用户级 SQL ⊆ `search(count=False)` 的 SQL；
      ② **绝对读数：只记录、不判定**。54 号 C5 的"单次 < 50ms"在本机**未达标**
         （裸检索自己就 8 个查询里 5 个 > 50ms），根因是既有 FTS postings 扫描
         （`fish_corpus_store`，不在 T03a 写作用域）→ 这是 **T04 红项**的读数留档，
         **不是"已达标"**。谁把这个读数读成"通过"，就等于把 50→300ms 的退化放行了。
      ③ 每次都跑的墙钟兜底在 C5b（合成 1000 段 ≤ 200ms）。

    库由 `FISH_CORPUS_PERF_DB` 指定（文件或 `data` 目录都行）；未设/不存在/段数不足
    → `skipTest` 并写明"判不到"（**SKIP 不是 PASS**）。量之前先做 `backup` 一次性
    只读副本：生产库有活的写入方，在原文件上量到的是 WAL 检查点与页缓存争用。
    """

    MIN_SEGMENTS = 40000
    RUNS = 3
    LEGACY_C5_MS = 50.0            # 54 号 C5 的原阈值：**只用于报告措辞**，不作断言

    @classmethod
    def setUpClass(cls):
        raw = (os.environ.get(PERF_ENV) or "").strip()
        if not raw:
            raise unittest.SkipTest("判不到：未设置 %s（真语料只读路径）" % PERF_ENV)
        path = Path(raw)
        if path.is_dir():
            path = store.db_path(path)
        if not path.exists():
            raise unittest.SkipTest("判不到：%s 指向的语料库不存在（%s）" % (PERF_ENV, path))
        workspace = Path(tempfile.mkdtemp(prefix="t03a-perf-"))
        cls.addClassCleanup(shutil.rmtree, workspace, True)
        copy_path = workspace / "corpus.sqlite"
        source = store.connect(path, readonly=True, busy_timeout_ms=250)
        try:
            target = sqlite3.connect(str(copy_path))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        conn = store.connect(copy_path, readonly=False, busy_timeout_ms=1000)
        cls.addClassCleanup(conn.close)
        cls.conn = conn
        cls.segments = int(conn.execute(
            "SELECT COUNT(*) FROM corpus_segment").fetchone()[0])
        if cls.segments < cls.MIN_SEGMENTS:
            raise unittest.SkipTest("判不到：语料库仅 %d 段（要 %d+ 段）"
                                    % (cls.segments, cls.MIN_SEGMENTS))
        picked = _pick_query(conn)
        if picked is None:
            raise unittest.SkipTest("判不到：真库里挑不出可命中的 (project, query)")
        cls.project, cls.query = picked

    @staticmethod
    def _conv_once(conn, query, project) -> float:
        start = time.perf_counter()
        fish_corpus.conv_push_candidates(query=query, project=project, conn=conn)
        return (time.perf_counter() - start) * 1000.0

    @staticmethod
    def _search_once(conn, query, project) -> float:
        start = time.perf_counter()
        fish_corpus.search(query,
                           filters={"project": [project], "role": [store.ROLE_ASSISTANT]},
                           limit=fish_corpus.PUSH_SEARCH_LIMIT, conn=conn, record=False,
                           count=False)
        return (time.perf_counter() - start) * 1000.0

    def test_c5_real_corpus_candidate_layer_sql_is_subset(self):
        res = assert_candidate_sql_is_subset(self, self.conn, self.query, self.project)
        self.assertTrue(res["items"], "真库查询挑出来了却没候选：判据空转")
        print("[C5a] segments=%d project=%r query=%r" % (self.segments, self.project,
                                                        self.query))

    def test_c5_real_corpus_absolute_readings_are_recorded(self):
        """**只记录**，不断言时间（见类 docstring 的第 ② 条）。

        这里唯一的断言是"读数不是空转"：真库副本上确实命中了候选。时间的三个读数
        （候选层 / 裸检索 / 增量）进命令行日志与交付报告，作为 T04 红项的留档。
        """
        res = fish_corpus.conv_push_candidates(query=self.query, project=self.project,
                                               conn=self.conn)   # 热身
        self.assertTrue(res["items"], "真库查询挑出来了却没候选：读数空转")
        conv_ms, search_ms = [], []
        for _ in range(self.RUNS):             # 交错测量：别让后测的一批吃缓存红利
            conv_ms.append(self._conv_once(self.conn, self.query, self.project))
            search_ms.append(self._search_once(self.conn, self.query, self.project))
        best_conv = min(conv_ms)
        best_search = min(search_ms)
        print("[C5a] segments=%d project=%r query=%r"
              " conv(best=%.1f all=%s) search(best=%.1f all=%s) delta=%+.1fms"
              " → 54 C5 原阈值 %.0fms：%s（T04 红项，只记录不判定）"
              % (self.segments, self.project, self.query, best_conv,
                 ["%.1f" % t for t in conv_ms], best_search,
                 ["%.1f" % t for t in search_ms], best_conv - best_search,
                 self.LEGACY_C5_MS,
                 "单样本低于原阈值（**不作达标结论**，见报告 §6.1）"
                 if best_conv < self.LEGACY_C5_MS else "已超原阈值"))
        self.assertEqual(len(conv_ms), self.RUNS)
        self.assertEqual(len(search_ms), self.RUNS)


class PerfSyntheticTest(push_fixture.PushCase):
    """C5b：合成 1k 段夹具的墙钟上界（≤200ms）——真库缺席时的回归兜底。

    为什么还要它：真库那条只在设了环境变量时才跑（否则 SKIP），而"候选生成变慢"
    这种回退必须**每次都测得到**。合成夹具的量级小一个数量级，所以上界放宽到 200ms。
    它守的是"量级"（秒级退化），不是 54 号 C5 的 50ms（那条归 T04）。
    """

    SEGMENTS = 1000
    LIMIT_MS = 200.0

    def test_c5_synthetic_1k_segments_under_200ms(self):
        sid = "9f0a0006-eeee-4de8-87b0-000000000006"
        events = [push_fixture.ev_session(sid),
                  push_fixture.ev_user(9, push_fixture.QUERY)]
        events += [push_fixture.ev_assistant(10 + i,
                                             push_fixture.LONG + "（第 %d 条）" % i)
                   for i in range(self.SEGMENTS)]
        self.write_session(sid, events)
        self.ingest()
        timings = []
        res = None
        for _ in range(3):
            start = time.perf_counter()
            res = fish_corpus.conv_push_candidates(query=push_fixture.QUERY,
                                                   project=push_fixture.PROJECT,
                                                   root=self.root)
            timings.append((time.perf_counter() - start) * 1000.0)
        best = min(timings)
        self.assertTrue(res["items"], "合成夹具里应该有候选（否则测的是空跑）")
        print("[C5b] segments=%d best=%.1fms all=%s"
              % (self.SEGMENTS, best, ["%.1f" % t for t in timings]))
        self.assertLess(best, self.LIMIT_MS,
                        "合成 %d 段单次 %.1fms 超 %.0fms"
                        % (self.SEGMENTS, best, self.LIMIT_MS))


# ====================================================================== C6


class SemanticKindTest(push_fixture.PushCase):
    """C6：语义类**归一**到三类（`mem`/`kb`/`corpus`），单点入口 `semantic_kind`。

    首轮 🟠 S1：`kind_class` 只有语料侧有，记忆侧五种 `kind` 一个都没有 → "按
    `kind_class or kind` 分三类"在代码里不成立（会得到 7 类），下游只能各自猜映射。
    """

    def test_c6_maps_a_sample_of_each_class(self):
        # corpus：`kind` 兜底能认出来（语料候选的既有 kind）
        self.assertEqual(rank_mod.semantic_kind({"kind": "conv_related"}), "corpus")
        # kb：记忆侧两种 kb 形态都归 kb
        self.assertEqual(rank_mod.semantic_kind({"kind": "kb"}), "kb")
        self.assertEqual(rank_mod.semantic_kind({"kind": "kb_related"}), "kb")
        # mem：记忆侧五种 kind 一网打尽（原来没有 kind_class 的那批）
        for kind in ("rule", "association", "serendipity", "situational",
                     "prompt_related"):
            self.assertEqual(rank_mod.semantic_kind({"kind": kind}), "mem", kind)
        self.assertEqual(rank_mod.KIND_CLASSES, ("mem", "kb", "corpus"))

    def test_c6_kind_class_wins_and_candidates_are_self_consistent(self):
        # `kind_class` 优先：两个字段打架时以它为准（它就是为了不再猜而加的）
        self.assertEqual(rank_mod.semantic_kind({"kind_class": "kb",
                                                "kind": "conv_related"}), "kb")
        # 候选层自己产的两个字段不许打架：conv_related ↔ corpus
        self.write_session()
        self.ingest()
        item = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT,
            root=self.root)["items"][0]
        # 无 scorer 的候选还没有 `kind`（那是 `_cand` 写的），但类别已经靠 `kind_class` 定了
        self.assertIsNone(item.get("kind"))
        self.assertEqual(item["kind_class"], "corpus")
        self.assertEqual(rank_mod.semantic_kind(item), rank_mod.KIND_CLASS_CORPUS)
        # 真实链路（`_cand` 打分后）不变量：`kind == conv_related` 且 `kind_class == corpus`
        cand = self.candidates()[0]
        self.assertEqual(cand["kind"], "conv_related")
        self.assertEqual(cand["kind_class"], "corpus")
        self.assertEqual(rank_mod.semantic_kind(cand), rank_mod.KIND_CLASS_CORPUS)
        # 常量只有一份：语料层的别名指向 rank 模块（不许再写一个 "corpus" 字面量）
        self.assertEqual(fish_corpus.PUSH_KIND_CLASS, rank_mod.KIND_CLASS_CORPUS)

    def test_c6_unknown_kind_falls_back_to_mem_without_raising(self):
        notes = []
        # 未知 kind 不抛：合流池里"多一类记忆"比"整批候选炸掉"轻得多
        self.assertEqual(rank_mod.semantic_kind({"kind": "brand_new_kind"},
                                               notes=notes), "mem")
        self.assertEqual(notes, [("unknown_kind", "brand_new_kind")])
        # 空/缺字段同样如此（且不记账：这是"没有类别"，不是"词表长出了新 kind"）
        empty_notes = []
        self.assertEqual(rank_mod.semantic_kind({}, notes=empty_notes), "mem")
        self.assertEqual(empty_notes, [("unknown_kind", "")])
        self.assertEqual(rank_mod.semantic_kind(None), "mem")
        self.assertEqual(rank_mod.semantic_kind({"kind_class": "nope",
                                                 "kind": "rule"}), "mem")
        self.assertEqual(rank_mod.semantic_kind({"kind_class": "nope",
                                                 "kind": "conv_related"}), "corpus")


# ====================================================================== C7


class ScoreStageTest(push_fixture.PushCase):
    """C7：`score` 是**阶段一**的数，与 `score_breakdown` 的四项不是同一个量。

    首轮 🟠 S2：报告一边承诺"score 一个字都不改"，一边写"补两项后
    `score = combine_features(breakdown)`"——两条要求互斥，等于把口径留给 T03b 临场拍。
    R2 定的口径：T03a 的 `score` 永远等于 `_cand` 的 `(benefit − cost) × weight`
    （`score_stage="first_phase"`、`rerank=None`）。T03b 重排后**原子替换**
    `score`/`score_breakdown` 并置 `score_stage="rerank"`——"这是谁排的"必须能查。
    """

    def _scorer(self):
        def scorer(item):
            return fish._cand(self.conn, item["code"], "conv_related",
                              title=item["title"], reason=item["reason"],
                              current_project=push_fixture.PROJECT, now=None,
                              relevance=item["relevance"], freshness=None,
                              interrupt=0.4)
        return scorer

    def test_c7_score_is_first_phase_not_the_breakdown_sum(self):
        self.write_session()
        self.ingest()
        cand = fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            scorer=self._scorer())["items"][0]
        self.assertEqual(cand["score_stage"], rank_mod.SCORE_STAGE_FIRST)
        self.assertIsNone(cand["rerank"])
        # 独立 oracle：用候选自带的 benefit/cost/weight 复算**阶段一**公式
        # （不是再调一遍被打分的函数，也不是把四项加起来）
        self.assertAlmostEqual(cand["score"],
                               round((cand["benefit"] - cand["cost"]) * cand["weight"], 4),
                               delta=1e-4)
        # 明细自己可审计：硬编码权重手算 == combine_features（Y5：不拿被测函数自证）
        features = cand["score_breakdown"]
        hand = (0.45 * features["relevance"] + 0.15 * features["freshness"]
                + 0.15 * features["project_match"] - 0.30 * features["dup_penalty"])
        self.assertAlmostEqual(rank_mod.combine_features(features), hand, delta=1e-9)
        print("[C7] score=%.4f（阶段一：benefit−cost×weight）/ 四项加权和=%.4f（阶段二口径）"
              % (cand["score"], hand))
        # 本判据**不**断言"分项和 == score"：那是两个阶段的数，34 §3.5 的 R3 只在
        # 重排之后成立（T03b 的重排必须同时换掉 score 与 breakdown）。

    def test_c7_stage_contract_is_declared_for_t03b(self):
        """阶段名只定义一份；T03a 的候选一律 `first_phase` + `rerank=None`。"""
        self.assertEqual(rank_mod.SCORE_STAGE_FIRST, "first_phase")
        self.assertEqual(rank_mod.SCORE_STAGE_RERANK, "rerank")
        self.assertEqual(rank_mod.SCORE_STAGES, ("first_phase", "rerank"))
        self.write_session()
        self.ingest()
        for item in self._items():
            self.assertEqual(item["score_stage"], rank_mod.SCORE_STAGE_FIRST)
            self.assertIn("rerank", item)
            self.assertIsNone(item["rerank"], "T03a 没有第二阶段，不许冒充已重排")

    def _items(self, **kwargs):
        return fish_corpus.conv_push_candidates(
            query=push_fixture.QUERY, project=push_fixture.PROJECT, root=self.root,
            **kwargs)["items"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
