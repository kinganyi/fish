#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 判据：MMR 多样性 + 价值门（2026-10-29 按外部审查 🟠3/🟠5 重设计）。

**每条判据都有明确的变异体**（`_audit/mutation_p2.py` 的 M7/M4），设计目标是
"坏实现必红、好实现必绿"，而不是"看起来合理"。

M-1  近重复抑制：两条标题**完全相同**（Jaccard=1.0）→ 第二条让给异主题
M-1b 同项目惩罚**不得过重**：同项目高分（0.95）该胜过异项目低分（0.70）
     —— 算术边界：λ=0.7 时同项目需领先 `0.4286×sim` 才扳平；
     sim=0.4 → 0.171（0.25 > 0.171，同项目胜 ✅）；sim=0.8 → 0.343（0.25 < 0.343，异项目胜 ❌）
     **这条判据就是 🟠5 的守卫**：谁把 sim 调回 0.8，它立刻红。
M-2  MMR 默认关
V-1  价值门：新内容有先天价值（imp=0 + 高相关 → 放），真低价值被挡
V-2  价值门默认关（floor=0）
V-3  **不空转**：prompt 通道的最低相关（rel=0.6）+ imp=0 → 挡
     （0.6×0.15=0.09 < 0.1；旧 0.3 底会给 0.18 → 放行 = 空转，故这条钉住 0.15 底）
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

    # ---------------------------------------------------------------- MMR
    # M-1：近重复（标题字符集合完全相同 → Jaccard=1.0）必须让位
    def test_M1_near_duplicate_yields_slot(self):
        cands = [
            {"code": "A", "kind": "prompt_related", "project": "P",
             "title": "项目全景 v2 收敛版", "score": 0.90},
            {"code": "B", "kind": "prompt_related", "project": "P",
             "title": "项目全景 v2 收敛版", "score": 0.85},     # 标题完全相同
            {"code": "C", "kind": "kb", "project": "Q",
             "title": "gamma-kb", "score": 0.50},
        ]
        picked = [c["code"] for c in
                  fish_push._mmr_select(self.conn, cands, 2, lam=0.7)]
        self.assertEqual(picked, ["A", "C"],
                         f"近重复该让位给异主题（Jaccard=1.0 必须生效）：{picked}")

    # M-1b：同项目惩罚不得过重（🟠5 的守卫；算术边界见文件头）
    def test_M1b_same_project_high_score_still_wins(self):
        """**夹具必须让 C 的标题与 B 零字符重叠**（Jaccard=0）。

        否则 C 自己也被多样性惩罚拖低，判据就测不出"同项目惩罚多重"——
        这正是 M7 第一次跑成 MISSED 的原因：C 标题 "gamma-kb" 与
        "beta-setting" 的 Jaccard 是 0.36，把 C 从 0.49 拉到 0.381，
        于是 sim=0.8（A=0.39）仍然赢。换成"伽马知识库"（与拉丁字符集无交集）。
        """
        cands = [
            {"code": "A", "kind": "prompt_related", "project": "P",
             "title": "alpha-config", "score": 0.90},
            {"code": "B", "kind": "prompt_related", "project": "P",
             "title": "beta-setting", "score": 0.95},
            {"code": "C", "kind": "kb", "project": "Q",
             "title": "伽马知识库", "score": 0.70},   # 与 B 零字符重叠
        ]
        picked = [c["code"] for c in
                  fish_push._mmr_select(self.conn, cands, 2, lam=0.7)]
        # 第一格 B（0.95 最高）
        # 第二格：sim=0.4 → A = 0.63−0.12 = 0.51 > C = 0.49 → A
        #         sim=0.8 → A = 0.63−0.24 = 0.39 < C = 0.49 → C（变异必红）
        self.assertEqual(picked, ["B", "A"],
                         f"同项目高分被过度惩罚了（sim 调回 0.8 就会变成 B+C）：{picked}")

    # M-2
    def test_M2_mmr_disabled_by_default(self):
        self.assertFalse(fish_push._mmr_enabled(self.conn))

    # ---------------------------------------------------------------- 价值门
    # V-1
    def test_V1_value_floor_keeps_fresh_blocks_low(self):
        """新内容有先天价值；真低价值被挡。variation=M4（去掉 0.15 底）必红。"""
        fresh = {"code": "F", "score": 0.9,
                 "factors": {"importance": 0.0, "relevance": 0.9}}
        self.assertTrue(
            fish_push._value_ok(fresh, 0.1),
            "新内容不能被判成零价值（旧公式 imp×rel=0 会把整条推送通道清空）")
        low = {"code": "L", "score": 0.1,
               "factors": {"importance": 0.0, "relevance": 0.1}}
        self.assertFalse(fish_push._value_ok(low, 0.1), "真低价值必须被挡")
        high = {"code": "H", "score": 0.9,
                "factors": {"importance": 1.0, "relevance": 0.9}}
        self.assertTrue(fish_push._value_ok(high, 0.1))

    # V-2
    def test_V2_value_floor_off_by_default(self):
        self.assertEqual(fish_push._value_floor(self.conn), 0.0)

    # V-3（🟠3 空转守卫：prompt 通道的最低相关必须能被挡）
    def test_V3_floor_is_not_a_noop_on_prompt_channel(self):
        """`rel=0.6`（prompt 的 `min_relevance`）+ `importance=0` → 0.09 < 0.1 → 挡。

        旧实现 importance 底是 0.3：0.6×0.3=0.18 > 0.1 → 恒真（空转）。
        这条判据把"底"钉在 0.15，谁改回 0.3 谁红。
        """
        weakest_prompt = {"code": "W", "score": 0.6,
                          "factors": {"importance": 0.0, "relevance": 0.6}}
        self.assertFalse(
            fish_push._value_ok(weakest_prompt, 0.1),
            "prompt 通道的最低相关 + 零热度必须被挡，否则价值门是空转")


if __name__ == "__main__":
    unittest.main(verbosity=2)
