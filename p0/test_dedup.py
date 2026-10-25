#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 C 判据：写前查重 —— **只提示，不拦**。

背景
----
§5 的原话：「是否有设计某种足够普遍、及时且不让 AI 厌烦的方式，要求 AI 在进行
项目创建 / 自我登记 / 记忆存档等各种『写』操作的时候先查重？**现状：没有。**」

用户给了硬约束：「如果每次写入都弹确认，**AI 会绕过系统直接写文件** —— 那比不查重
糟糕得多。」所以本阶段的判据不只测"能不能发现相似"，更测"**绝不阻断写入**"。

判据
----
D1   `find_similar_memories` 能发现内容相似的已有记忆
D1b  判别力：把 `min_sim` 提到 0.99 → D1 必须返回空（证明相似度真的参与判定，
     而不是"只要 FTS 命中就报")
D2   无关内容 → 返回空（查重最怕的是"什么都说像"：那样 AI 会训练自己忽略它）
D3   `record_memory(hints=[])` 把相似项填进 hints
D4   `record_memory` **不传 hints** → 行为完全不变（零开销，返回值仍是 (code, rel)）
D5   **有相似项时写入仍然成功**（硬约束：不阻断）
D6   `format_dup_hint` 全空 → 空串（不写"暂无相似"这种噪音）
D7   `find_similar_projects`：大小写同形（`cdql` / `CDQL`）
D8   `find_similar_projects`：前缀互为包含
D9   `find_similar_projects`：namespace 描述高度重合
D10  `find_similar_projects`：**精确重名不报**（那是 add_project 自己的职责，
     同一条信息不该说两遍）

跑法
----
    python test_dedup.py -v
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class DedupMemoryTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES ('PROJA','甲项目',NULL,0,'active')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _mem(self, title, body=""):
        return fish.record_memory(self.conn, self.data, project="PROJA",
                                  title=title, body=body)

    # ---------------------------------------------------------------- D1
    def test_D1_finds_similar_memory(self):
        """D1：内容相似 → 能找出已有那条。"""
        code, _ = self._mem("备份策略：改上线文件前先复制一份 .bak",
                            "上线文件的价值不在改动量，在它是唯一一份正确版本。")
        hits = fish.find_similar_memories(
            self.conn, title="上线文件改动前必须备份",
            body="改之前先复制 .bak，因为它是唯一正确版本")
        self.assertTrue(hits, "相似内容没被找出来（查重形同虚设）")
        self.assertEqual(hits[0]["code"], code)
        self.assertGreaterEqual(hits[0]["similarity"], fish.SIM_MIN_SIM)

    # ---------------------------------------------------------------- D1b
    def test_D1b_judgement_has_discriminating_power(self):
        """D1b 判别力：把阈值提到 0.99，同一条查询必须返回空。

        若它仍然命中，说明 D1 的成功可能来自"只要 FTS 沾边就报"，那判据就没有
        判别力（而且实际用起来会变成噪音源）。
        """
        self._mem("备份策略：改上线文件前先复制一份 .bak", "唯一正确版本")
        hits = fish.find_similar_memories(
            self.conn, title="上线文件改动前必须备份", body="先复制 .bak",
            min_sim=0.99)
        self.assertEqual(hits, [], "阈值拉到 0.99 还命中——判据没有判别力")

    # ---------------------------------------------------------------- D2
    def test_D2_unrelated_returns_empty(self):
        """D2：完全无关的内容 → 不报（防"什么都说像"）。"""
        self._mem("备份策略：改上线文件前先复制一份 .bak", "唯一正确版本")
        hits = fish.find_similar_memories(
            self.conn, title="今晚吃火锅还是烧烤", body="排队两小时值得吗")
        self.assertEqual(hits, [], f"无关内容也被判为相似：{hits}")

    # ---------------------------------------------------------------- D3
    def test_D3_record_memory_fills_hints(self):
        """D3：`hints=[]` → 相似项被填进来。"""
        self._mem("备份策略：改上线文件前先复制一份 .bak", "唯一正确版本")
        hints = []
        fish.record_memory(self.conn, self.data, project="PROJA",
                           title="上线文件改动前必须备份",
                           body="先复制 .bak，因为它是唯一正确版本",
                           hints=hints)
        self.assertTrue(hints, "hints 没被填进来——MCP/CLI 的回执就无从提示")
        self.assertIn("code", hints[0])

    # ---------------------------------------------------------------- D4
    def test_D4_no_hints_means_zero_overhead(self):
        """D4：不传 hints → 返回类型与数量都不变（旧调用一个字都不改）。"""
        self._mem("备份策略：改上线文件前先复制一份 .bak", "唯一正确版本")
        res = fish.record_memory(self.conn, self.data, project="PROJA",
                                 title="另一个标题", body="另一段正文")
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2, "返回值形状变了——旧调用会当场断")
        self.assertTrue(res[0].startswith("PROJA-"))

    # ---------------------------------------------------------------- D5
    def test_D5_write_is_never_blocked(self):
        """D5（硬约束）：**完全相同**的标题也照样写进去，只是回执带提示。

        这条是整个机制的安全底线：一旦这里开始抛错或拒绝，AI 就会绕过系统直接写文件。
        """
        self._mem("同一句话写两遍", "内容一模一样")
        hints = []
        code2, _ = fish.record_memory(self.conn, self.data, project="PROJA",
                                      title="同一句话写两遍", body="内容一模一样",
                                      hints=hints)
        n = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(n, 2, "重复内容被拦下来了——违反'不阻断'的硬约束")
        self.assertTrue(code2)
        self.assertTrue(hints, "拦不拦是一回事，提示该给还是得给")

    # ---------------------------------------------------------------- D6
    def test_D6_empty_hint_is_empty_string(self):
        """D6：全空 → 空串（不写"暂无相似"这种噪音，噪音会训练 AI 跳过它）。"""
        self.assertEqual(fish.format_dup_hint(), "")
        self.assertEqual(fish.format_dup_hint([], []), "")
        text = fish.format_dup_hint(mem_hints=[{"code": "X-1", "title": "t",
                                                "similarity": 0.4}])
        self.assertIn("没有拦你", text, "提示里必须明说'没拦你'，否则 AI 会当成拒绝")


class DedupProjectTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = fish.connect(self.root / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for p, ns in (("CDQL", "cdql.cc"), ("TESTP", "测试项目"),
                      ("ANYI", "anyi-studio"), ("VIDEO", "视频营销")):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, ns))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- D7
    def test_D7_case_variant_reported(self):
        """D7：`cdql` 与 `CDQL` 是同一项目的两个入口（生产库真实存在）。"""
        hits = fish.find_similar_projects(self.conn, "cdql", namespace="cdql.cc 平台")
        names = [h["prefix"] for h in hits]
        self.assertIn("CDQL", names, f"大小写同形没报出来：{hits}")
        reasons = next(h["reasons"] for h in hits if h["prefix"] == "CDQL")
        self.assertIn("大小写不同但同形", reasons)

    # ---------------------------------------------------------------- D8
    def test_D8_prefix_containment_reported(self):
        """D8：前缀互为包含（手滑常见形态）。"""
        hits = fish.find_similar_projects(self.conn, "TEST", namespace="另一个")
        self.assertIn("TESTP", [h["prefix"] for h in hits],
                      f"包含关系没报出来：{hits}")

    # ---------------------------------------------------------------- D9
    def test_D9_namespace_overlap_reported(self):
        """D9：名字完全不同但 namespace 描述重合 → 提示（`测试项目` / `测试项目2`）。"""
        hits = fish.find_similar_projects(self.conn, "ZZZZ", namespace="测试项目")
        self.assertIn("TESTP", [h["prefix"] for h in hits],
                      f"描述重合没报出来：{hits}")

    # ---------------------------------------------------------------- D10
    def test_D10_exact_name_not_reported(self):
        """D10：精确重名**不在这里报**（add_project 会直接拒绝复用，同一条信息不说两遍）。"""
        hits = fish.find_similar_projects(self.conn, "CDQL", namespace="cdql.cc")
        self.assertNotIn("CDQL", [h["prefix"] for h in hits],
                         "精确重名被重复报了——应由 add_project 的精确查重负责")


if __name__ == "__main__":
    unittest.main(verbosity=2)
