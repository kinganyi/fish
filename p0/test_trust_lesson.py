#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P0-3 判据：**未归属的 lesson 不许自动进注入包**。

## 审查方实测的问题（已复核成立）

`_lesson_rows` 只按 `mem_type='lesson'` 选，**不校验 owner**。而按 2026-11-11 的决策
`lesson` **免凭证可写**（"记忆比归属重要"）。两条放一起：

    → 任何 MCP 调用方都能写一条 lesson，而它会进 **所有会话** 的错题本段
    → 写入成本为零，影响面是全库 —— 这就是"陌生人能改教训"

实测生产库：`rule` 71 条 0 未归属（凭证门有效）、`identity` 11 条 0 未归属、
**`lesson` 105 条里 3 条未归属** —— 缺口就在这一类。

## 为什么修法不是"lesson 也要求凭证"

用户的决策是"记忆比归属重要"：无凭证照写不误。所以修的是**进包侧**，不是写入侧：

  · 默认：未归属 lesson **不进包**（`inject.allow_untrusted_lessons` = 0）
  · 放行：置 1 则允许进包（运维显式选择承担风险）

这样"无凭证也能记教训"和"陌生人的教训不能自动约束所有 AI"两件事都成立。

## 这批判据为什么能失败

关键是**同时存在两条 lesson（有主 / 无主）**，然后断言只出现有主那条。
如果实现只看 `mem_type`，第一条断言立刻红。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


class UntrustedLessonTest(unittest.TestCase):

    def setUp(self):
        # 用 mkdtemp 而不是 TemporaryDirectory：Win 上 SQLite 的 -wal/-shm 句柄
        # 会让 GC 终结器的 rmtree 抛 PermissionError，那是**清理失败**不是判据失败，
        # 混在一起会让"判据红了"和"临时目录删不掉"看起来是同一件事。
        self._tmpdir = tempfile.mkdtemp(prefix="fish-trust-")
        self.data = Path(self._tmpdir) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()      # next_code 用 BEGIN IMMEDIATE，必须没有未结束的事务
        # 两条 lesson：一条有主、一条未归属
        fish.record_memory(self.conn, self.data, project="P", title="有主的教训",
                           body="# 有主\n", mem_type="lesson", owner="C-OWNED")
        fish.record_memory(self.conn, self.data, project="P", title="陌生人的教训",
                           body="# 无主\n", mem_type="lesson", owner="unattributed")
        self.conn.commit()

    def tearDown(self):
        # Windows：DB 文件句柄没完全释放时 rmtree 会 PermissionError，
        # 那是**清理失败**不是**判据失败**——用 ignore_errors 把它和断言分开。
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _titles(self):
        """走真实的段渲染路径，返回错题本段里出现的标题。"""
        blk = fish.build_lesson_block(self.conn, project="P")
        return blk.get("text", "")

    # ------------------------------------------------------------- 主判据
    def test_unattributed_lesson_is_excluded_by_default(self):
        """**F-P0-3 的直接探针**：未归属 lesson 默认不进错题本段。"""
        text = self._titles()
        self.assertIn("有主的教训", text, f"有主 lesson 没进包：{text!r}")
        self.assertNotIn("陌生人的教训", text,
                         f"**未归属 lesson 进了注入包** —— 这就是审查方说的投毒面：\n{text!r}")

    def test_attributed_lesson_still_enters(self):
        """对照：有主 lesson 必须照常进包（不能为了安全把功能砍掉）。"""
        self.assertIn("有主的教训", self._titles())

    def test_count_and_list_share_the_same_gate(self):
        """段头的 M 与列表必须同口径——否则"说 2 条只列 1 条"。"""
        blk = fish.build_lesson_block(self.conn, project="P")
        text = blk.get("text", "")
        # 段头形如「错题本 … N/M 条」；未归属被排掉后，M 应为 1
        self.assertIn("1/1", text,
                      f"段头计数没跟上过滤（应只有 1 条）：{text!r}")

    # ------------------------------------------------------------- 开关
    def test_explicit_opt_in_allows_them(self):
        """运维显式打开开关时允许进包——**默认关不等于永久禁**。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key,value,type,updated_at)"
            " VALUES ('inject.allow_untrusted_lessons','1','int','x')")
        self.conn.commit()
        self.assertIn("陌生人的教训", self._titles(),
                      "开关打开后未归属 lesson 仍未进包")

    # ------------------------------------------------------------- 写入侧不变
    def test_unattributed_lesson_can_still_be_written(self):
        """**写入侧不许被收紧**：用户决策是"记忆比归属重要"，无凭证照写不误。"""
        code, *_ = fish.record_memory(
            self.conn, self.data, project="P", title="第三条无主",
            body="# x\n", mem_type="lesson", owner="unattributed")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertEqual(row[0], "unattributed", "无凭证写入被拦了——违反了用户决策")
        self.assertNotIn("第三条无主", self._titles(), "写进去了却照样进包")


if __name__ == "__main__":
    unittest.main(verbosity=2)
