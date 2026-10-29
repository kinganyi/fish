#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 阶段判据：MMR 多样性 + 价值门。

M-1 两条几乎相同（同 kind/project + 标题高度重叠）时，MMR 只选一条，第二条让给异主题
M-2 MMR 默认关（`push.mmr_enabled` 未设 → False）
V-1 价值门开启时，低 `importance × relevance` 的候选被挡
V-2 价值门默认关（`push.value_floor` = 0）
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish          # noqa: E402
import fish_push     # noqa: E402


class MmrValueTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-mmr-"))
        self.conn = fish.connect(self.tmp)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _twin(self):
        # A/B 几乎一模一样；C 异主题
        return [
            {"code": "A", "kind": "prompt_related", "project": "P",
             "title": "项目全景 v2 收敛版", "score": 0.90},
            {"code": "B", "kind": "prompt_related", "project": "P",
             "title": "项目全景 v2 收敛版（复制）", "score": 0.80},
            {"code": "C", "kind": "kb", "project": "P",
             "title": "完全不同的知识库笔记", "score": 0.50},
        ]

    # M-1
    def test_M1_mmr_prefers_diversity(self):
        picked = fish_push._mmr_select(self.conn, self._twin(), 2, lam=0.7)
        codes = [c["code"] for c in picked]
        self.assertEqual(codes, ["A", "C"],
                         f"MMR 该选 A + 异主题 C，而不是两条近重复：{codes}")

    # M-2
    def test_M2_mmr_disabled_by_default(self):
        self.assertFalse(fish_push._mmr_enabled(self.conn))

    # V-1
    def test_V1_value_floor_blocks_low_value(self):
        """价值门的**正确形态**：新内容有先天价值（不能因为"还没被读过"就判零）。

        ★ 判别力由 `_audit/mutation_p2.py` 的 M4 钉住：把 importance 的 0.3 底去掉
        （恢复 `rel × imp`）→ 第一条断言必红。旧版判据只有"低价值被挡"，
        两个公式都满足，等于空转（M4 第一次跑就 MISSED 抓到了这一点）。
        """
        # 新记忆：importance=0 但 relevance 高 → 先天价值 0.9×0.3=0.27 ≥ 0.2 → 放
        fresh = {"code": "F", "score": 0.9,
                 "factors": {"importance": 0.0, "relevance": 0.9}}
        self.assertTrue(
            fish_push._value_ok(fresh, 0.2),
            "新内容不能被判成零价值（旧公式 imp×rel=0 会把整条推送通道清空）")
        # 真的低价值（rel 也低）→ 0.1×0.3=0.03 < 0.2 → 挡
        low = {"code": "L", "score": 0.1,
               "factors": {"importance": 0.0, "relevance": 0.1}}
        self.assertFalse(fish_push._value_ok(low, 0.2))
        # 高价值 → 放
        high = {"code": "H", "score": 0.9,
                "factors": {"importance": 1.0, "relevance": 0.9}}
        self.assertTrue(fish_push._value_ok(high, 0.5))

    # V-2
    def test_V2_value_floor_off_by_default(self):
        self.assertEqual(fish_push._value_floor(self.conn), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
