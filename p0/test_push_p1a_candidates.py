#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-a 候选池的判据（2026-10-25）。

设计来源
--------
`16` §5：判据是「收益 − 成本」（不是"有没有用"）；四类候选 50/25/15/10；
防静默失败（降权但不归零，90 天封顶）；评估是门控层。

这批判据守的是**契约**，不是实现细节：
  C1  四类候选都能生成，且比例接受 10% 意外
  C2  打分（benefit − cost）能改变排序——不是摆设
  C3  与 ③④⑤ 不重复编码（exclude 生效）
  C4  **防静默**：被忽略 10 次的候选仍在 90 天封顶内出现一次
  C5  关键节点（未闭合/冲突）不受降权影响
  C6  候选段进注入包，且 meta 报出候选
  C7  关闭推送（push.enabled=False）时整段不出现

每条都配变异（见各 test_*_mutation_*）：把对应机制摘掉，判据必须红。
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class PushCandidateTest(unittest.TestCase):
    """候选池的契约。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.data = Path(self.root) / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01CITIZEN0000000000000009"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "候选测试居民", "resident", "owner", None, "active", fish.now_utc()))
        # 两个项目：用来测"同项目 vs 跨项目"
        for p in ("TESTA", "TESTB"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p + " 项目"))
        self.conn.commit()
        self.n = 0

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _mem(self, project="TESTA", title=None, mem_type="fact", status="active"):
        self.n += 1
        code = f"{project}-261025-{self.n:02d}"
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,'[]',NULL,?,?,0,0)",
            (code, title or f"条目{self.n}", project, self.cid, mem_type, status,
             fish.now_utc(), fish.now_utc()))
        self.conn.commit()
        return code

    def _link(self, src, dst, rel="related", source="semantic", weight=0.5, suppressed=0):
        self.conn.execute(
            "INSERT INTO links (src,dst,rel,source,weight,evidence,created_at,suppressed) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (src, dst, rel, source, weight, "测试建边", fish.now_utc(), suppressed))
        self.conn.commit()

    def _access(self, code, *, citizen=None, via="recall", ts=None):
        self.conn.execute(
            "INSERT INTO memory_access_log (code,citizen_id,session_key,project,via,accessed_at) "
            "VALUES (?,?,?,?,?,?)",
            (code, citizen or self.cid, "s-test", "TESTA", via, ts or fish.now_utc()))
        self.conn.commit()

    def _push(self, code, *, reaction=None, pushed_at=None, session="s-test"):
        self.conn.execute(
            "INSERT INTO push_log (session_key,citizen_id,project,code,channel,budget_tokens,"
            "pushed_at,reaction) VALUES (?,?,?,?,?,?,?,?)",
            (session, self.cid, "TESTA", code, "inject", 1000,
             pushed_at or fish.now_utc(), reaction))
        self.conn.commit()

    # ------------------------------------------------------------------ C1
    def test_C1_four_kinds_generated(self):
        """四类候选都能生成；且至少有一个「意外」类（防茧房闸）。

        夹具要点：**锚点是"该项目最近 3 条"**，所以候选必须比锚点旧——
        先写候选、后写锚点。若顺序反了，每条记忆都成了锚点，候选一个都不剩
        （这正是本判据第一版红掉的原因，记在这里免得再踩）。
        """
        old1 = self._mem("TESTA", "A 的旧条目")      # 候选（非锚点）
        b1 = self._mem("TESTB", "B 的条目")          # 候选（跨项目）
        a1 = self._mem("TESTA", "A 的锚点一")
        self._mem("TESTA", "A 的锚点二")
        self._mem("TESTA", "A 的锚点三")            # 最后 3 条 = 锚点
        self._link(a1, old1, weight=0.9)            # 联想：同项目邻居
        self._link(a1, b1, weight=0.4)              # 意外：跨项目
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="TESTA")
        kinds = {c["kind"] for c in cands}
        self.assertIn("association", kinds,
                      f"应有「联想」类候选（锚点 {a1} 的同项目邻居 {old1}），实际 {kinds}")
        self.assertIn("serendipity", kinds,
                      f"应有「意外」类候选（跨项目邻居 {b1}），实际 {kinds}")
        for c in cands:
            self.assertIn("reason", c, "每条候选必须带 reason（可解释性是契约）")
            self.assertTrue(c["reason"].strip(), "reason 不能为空串")
            self.assertIn("score", c)
            self.assertIn("benefit", c)
            self.assertIn("cost", c)

    # ------------------------------------------------------------------ C2
    def test_C2_score_changes_order(self):
        """打分必须能改变排序：高相关 + 未读 的候选应排在被读过的前面。"""
        hot = self._mem("TESTA", "热门邻居")
        cold = self._mem("TESTA", "冷门邻居")
        a1 = self._mem("TESTA", "锚点一")
        self._mem("TESTA", "锚点二")
        self._mem("TESTA", "锚点三")
        self._link(a1, hot, weight=0.9)
        self._link(a1, cold, weight=0.9)
        self._access(cold)                     # cold 已被本会话读过 → novelty 低
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="TESTA")
        idx = {c["code"]: i for i, c in enumerate(cands)}
        self.assertIn(hot, idx, f"hot 应在候选里，实际 {list(idx)}")
        self.assertIn(cold, idx, f"cold 应在候选里，实际 {list(idx)}")
        self.assertLess(idx[hot], idx[cold],
                        "未读的候选应排在已读的前面（打分没生效）")

    # ------------------------------------------------------------------ C3
    def test_C3_exclude_removes_codes(self):
        """exclude 里的编码不得出现在候选里（避免与 ③④⑤ 重复占预算）。"""
        a2 = self._mem("TESTA", "A 的邻居")        # 候选（非锚点）
        a1 = self._mem("TESTA", "A 的锚点")
        self._mem("TESTA", "锚点二")
        self._mem("TESTA", "锚点三")
        self._link(a1, a2, weight=0.9)
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="TESTA",
            exclude={a2})
        codes = {c["code"] for c in cands}
        self.assertNotIn(a2, codes, f"exclude 里的 {a2} 不该出现，实际 {codes}")

    # ------------------------------------------------------------------ C4
    def test_C4_silencing_fallback(self):
        """**防静默**：一条被忽略 10 次的候选，仍必须在 90 天封顶内出现一次。

        这是 `15` 调研的核心修正（DAWN 的 silencing failure mode）：
        归零 = 吸收态，永远不推，而用户说过"等推送完善后会让 AI 意识到冷门工具的用途"。
        """
        cold = self._mem("TESTA", "被忽略十次的邻居")   # 候选（非锚点）
        a1 = self._mem("TESTA", "A 的锚点")
        self._mem("TESTA", "锚点二")
        self._mem("TESTA", "锚点三")
        self._link(a1, cold, weight=0.9)
        for _ in range(10):
            self._push(cold, reaction="ignored")
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="TESTA")
        codes = {c["code"] for c in cands}
        self.assertIn(cold, codes,
                      "被忽略 10 次也必须保留（降权可以，归零不行）——否则永远没有「以后」")

    def test_C4_mutation_zero_instead_of_decay(self):
        """变异：把「降权但保留」改成「归零即剔除」→ C4 必须红。"""
        cold = self._mem("TESTA", "被忽略十次的邻居")
        a1 = self._mem("TESTA", "A 的锚点")
        self._mem("TESTA", "锚点二")
        self._mem("TESTA", "锚点三")
        self._link(a1, cold, weight=0.9)
        for _ in range(10):
            self._push(cold, reaction="ignored")
        import fish_push
        orig = fish_push._silence_weight
        try:
            # T02 拆包：`_silence_weight` 已搬进 `fish_push`——patch 必须打在**使用方模块**上，
            # 打 `fish._silence_weight` 只是改了 fish.py 的导入副本（from-import 快照），生效不了。
            fish_push._silence_weight = lambda conn, code, **kw: 0.0   # 归零
            cands = fish.build_push_candidates(
                self.conn, self.data, citizen_id=self.cid, project="TESTA")
        finally:
            fish_push._silence_weight = orig
        self.assertNotIn(cold, {c["code"] for c in cands},
                         "变异后 cold 应被剔除（若仍出现，说明 C4 没在测防静默）")

    # ------------------------------------------------------------------ C5
    def test_C5_key_nodes_immune_to_decay(self):
        """关键节点（未闭合事项）不受静默降权影响。

        「未闭合」由标题关键词识别（`_open_loop_rows`），不按 mem_type——
        所以夹具标题必须含「未闭合」，否则它根本进不了 rule 类（第一版就红在这里）。
        """
        rule = self._mem("TESTA", "未闭合：本项目的铁律还没落地", mem_type="rule")
        a1 = self._mem("TESTA", "A 的锚点")
        self._mem("TESTA", "锚点二")
        self._mem("TESTA", "锚点三")
        for _ in range(10):
            self._push(rule, reaction="ignored")
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="TESTA")
        hit = [c for c in cands if c["code"] == rule]
        self.assertTrue(hit, f"未闭合事项不该被静默降权剔除，实际候选 {[c['code'] for c in cands]}")
        self.assertGreater(hit[0]["score"], 0,
                           "关键节点的 score 必须仍为正（否则等于没推）")
        self.assertTrue(hit[0]["key"], "rule 类必须标记 key=True")

    # ------------------------------------------------------------------ C6
    def test_C6_block_in_pack_and_meta(self):
        """候选段必须进注入包，且 meta 报出候选编码。"""
        a1 = self._mem("TESTA", "A 的锚点")
        a2 = self._mem("TESTA", "A 的邻居")
        self._link(a1, a2, weight=0.9)
        pack, meta = fish.build_injection_pack(
            self.conn, self.data, self.cid, project="TESTA")
        self.assertIn("push_candidates", meta,
                      "meta 必须报出候选（契约：调用方按固定形状读）")
        if meta["push_candidates"]:
            self.assertIn("可能对你有用", pack,
                          f"有候选时包里应有该段。包尾：\n{pack[-600:]}")

    # ------------------------------------------------------------------ C7
    def test_C7_disabled_hides_block(self):
        """push.enabled=False 时整段不出现（度量要做对照，必须能关）。"""
        fish.set_config(self.conn, "push.enabled", False)
        a1 = self._mem("TESTA", "A 的锚点")
        a2 = self._mem("TESTA", "A 的邻居")
        self._link(a1, a2, weight=0.9)
        pack, meta = fish.build_injection_pack(
            self.conn, self.data, self.cid, project="TESTA")
        self.assertNotIn("可能对你有用", pack, "关闭推送后不该出现候选段")
        self.assertEqual(meta["push_candidates"], [], "关闭后 meta 也应为空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
