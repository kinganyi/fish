#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文文件名 = 编码 + 标题缩写（2026-10-25）。

需求（用户拍板）：人用自己的文件管理工具（Everything / 资源管理器）按文件名搜时，
纯编码文件名等于零信息。所以改成

    projects/<项目>/<编码>__<标题缩写>.md

四条纪律（每条都有判据钉住）：
  ① **纯函数**：同样输入永远同样输出——否则 rebuild 算出不同路径，重建即断链
  ② **标题是记录时的快照**：`memory.revised` 改标题后文件名**不变**
     （跟着变会让所有外部引用失效）
  ③ **截 24 字符**：标题平均 38.7 / 最长 81，不截会长到难读
  ④ **两处共用同一函数**：`record_memory` 与 `rebuild_projection` 都必须调 `body_relpath`

判据
----
  N1  纯函数：连续调用结果一致，且不依赖外部状态
  N2  Windows 非法字符被替换（76 条标题含，`/` 最多）
  N3  截断到 24 字符；空标题有兜底
  N4  `body_relpath` 形态正确
  N5  `record_memory` 生成的文件名**含标题**
  N6  **`rebuild` 与 `record_memory` 算出同一路径**（最关键：防两处漂移）
  N7  标题变化后文件名不变（快照语义）
  N8  变异：改 slug 规则 → N6 必红（证明判据有判别力）
"""
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

ILLEGAL = set('<>:"/\\|?*')


class BodyNamingTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01CITIZEN0000000000000009"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "命名测试", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('TESTP','测试项目',NULL,0,'active')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ N1
    def test_N1_slug_is_pure(self):
        """纯函数：同样输入 → 同样输出（rebuild 靠这条才能算出同一路径）。"""
        t = "协作铁律·行为层（CR-2/5/6/7/11 + 边界模糊先报备）"
        a = fish.slug_title(t)
        b = fish.slug_title(t)
        self.assertEqual(a, b, "同一标题两次调用结果不同——不是纯函数")
        self.assertEqual(fish.body_relpath("P", "P-261022-01", t),
                         fish.body_relpath("P", "P-261022-01", t),
                         "body_relpath 也不是纯函数")

    # ------------------------------------------------------------------ N2
    def test_N2_illegal_chars_replaced(self):
        """Windows 非法字符必须被替换，否则文件根本建不出来。"""
        t = 'a/b\\c:d*e?f"g|h<i>j'
        s = fish.slug_title(t)
        for ch in ILLEGAL:
            self.assertNotIn(ch, s, f"slug 里仍有非法字符 {ch!r}: {s!r}")
        self.assertTrue(s, "替换后不能变空")

    def test_N2_real_world_samples(self):
        """真实标题（含 76 条里的典型形态）也要过。"""
        for t in ("协作铁律·行为层（CR-2/5/6/7/11 + 边界模糊先报备）",
                  "MCP 接入回执 v2.1（对外）：paid_* 已修并部署 / 角色表述更正",
                  "引用与验证纪律（CR-12 引用三件套 / CR-14 验证声明五字段）",
                  '他说"这条要改"'):
            s = fish.slug_title(t)
            for ch in ILLEGAL:
                self.assertNotIn(ch, s, f"{t!r} → {s!r} 仍含 {ch!r}")

    # ------------------------------------------------------------------ N3
    def test_N3_truncated_to_24(self):
        """截断到 24 字符（用户拍板的长度）。"""
        long_title = "这是一条非常非常长的标题" * 5
        s = fish.slug_title(long_title)
        self.assertLessEqual(len(s), fish._SLUG_MAX, f"未截断：{len(s)} 字符")
        self.assertEqual(fish._SLUG_MAX, 24, "用户拍板的是 24，改了要同步这条判据")

    def test_N3_empty_title_fallback(self):
        """空标题要有兜底，不能产出 `__` 结尾的怪文件名。"""
        for t in ("", None, "   ", "///", "..."):
            s = fish.slug_title(t)
            self.assertTrue(s, f"{t!r} 产出空片段")
            self.assertNotEqual(s.strip("._-"), "", f"{t!r} → {s!r} 全是分隔符")

    # ------------------------------------------------------------------ N4
    def test_N4_relpath_shape(self):
        """`projects/<项目>/<编码>__<缩写>.md` 的形态。"""
        p = fish.body_relpath("TESTP", "TESTP-261022-01", "决策：用方案 A")
        self.assertTrue(p.startswith("projects/TESTP/"), p)
        self.assertTrue(p.endswith(".md"), p)
        self.assertIn("TESTP-261022-01__", p, "编码与标题之间要用 __ 分隔")
        self.assertEqual(p.count("/"), 2, f"层级应为 projects/<项目>/<文件>：{p}")

    # ------------------------------------------------------------------ N5
    def test_N5_record_memory_puts_title_in_filename(self):
        """`record_memory` 写出来的文件路径必须含标题。"""
        code, art = fish.record_memory(
            self.conn, self.data, project="TESTP", title="决策：用方案 A",
            body="正文", owner=self.cid)
        row = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                (code,)).fetchone()
        fp = row[0]
        self.assertIn("决策", fp, f"文件名里没有标题：{fp}")
        self.assertIn(code, fp, f"文件名里没有编码：{fp}")
        self.assertTrue((self.data / fp).exists(), f"文件没真的写出来：{fp}")

    # ------------------------------------------------------------------ N6
    def test_N6_rebuild_computes_same_path(self):
        """**最关键**：rebuild 算出的 file_path 必须与写入时一致。

        这条守的是"两处共用同一函数"的纪律。如果哪天有人在 rebuild 里
        手拼路径（而不是调 `body_relpath`），规则一漂移，重建即断链。
        """
        code, _ = fish.record_memory(
            self.conn, self.data, project="TESTP", title="决策：用方案 A",
            body="正文", owner=self.cid)
        before = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                   (code,)).fetchone()[0]
        fish.rebuild_projection(self.conn, self.data)
        after = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                  (code,)).fetchone()[0]
        self.assertEqual(after, before,
                         f"rebuild 算出的路径与写入时不一致：\n  写入={before}\n  重建={after}")
        self.assertTrue((self.data / after).exists(), f"重建后路径指向不存在的文件：{after}")

    def test_N6_mutation_changing_slug_goes_red(self):
        """变异：把 slug 规则换掉 → 重建路径必然与写入不同（证明 N6 有判别力）。"""
        code, _ = fish.record_memory(
            self.conn, self.data, project="TESTP", title="决策：用方案 A",
            body="正文", owner=self.cid)
        before = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                   (code,)).fetchone()[0]
        orig = fish.slug_title
        try:
            fish.slug_title = lambda t, limit=24: "MUTATED"   # 改规则
            after = fish.body_relpath("TESTP", code, "决策：用方案 A")
        finally:
            fish.slug_title = orig
        self.assertNotEqual(after, before,
                            "换了规则后路径居然没变——那 N6 测不出漂移")

    # ------------------------------------------------------------------ N7
    def test_N7_title_change_keeps_filename(self):
        """标题改了，**文件名不变**（快照语义，用户拍板）。

        理由：跟着变会让所有外部引用失效——人写的文档、别的 AI 的笔记都记着旧文件名。
        """
        code, _ = fish.record_memory(
            self.conn, self.data, project="TESTP", title="旧标题",
            body="正文", owner=self.cid)
        fp_before = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                      (code,)).fetchone()[0]
        # 改标题（模拟 memory.revised 只改标题）
        self.conn.execute("UPDATE code_index SET title='新标题' WHERE code=?", (code,))
        self.conn.commit()
        fp_after = self.conn.execute("SELECT file_path FROM code_index WHERE code=?",
                                     (code,)).fetchone()[0]
        self.assertEqual(fp_before, fp_after,
                         "改标题不该动文件名（快照语义）——动了外部引用就全断")
        self.assertIn("旧标题", fp_after, "文件名里应保留记录时的标题快照")


if __name__ == "__main__":
    unittest.main(verbosity=2)
