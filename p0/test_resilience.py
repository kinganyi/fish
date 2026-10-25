#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件系统韧性：人类随意改文件，系统不能崩（2026-10-25 · 用户明确要求）。

## 需求原文

> 人类会非常自由的修改项目文件夹内甚至包括项目文件夹本身，包括但不限于
> 随意删除任意文件、调整里面的任意内容、修改项目文件名、修改项目文件夹名、
> 改变文件顺序、改变文件位置……我们不接受系统因此而崩溃或无法运行。

## 必须先说清的一件事（诚实边界）

**"不崩溃"能做到；"不丢数据"目前做不到。**

取证结论：`memory.recorded` 事件的 payload 只有
`['body_ref','code','mem_type','tags','title','visibility']`——**没有 body**。
全库 227 条事件，带 body 的 **0 条**。所以：
  · 人类**改名/移动** → 系统按编码找回来（本文件判据 A）
  · 人类**删掉正文**   → 标题/类型/标签/事件还在，**只有正文内容永久丢失**

## 判据

  R1  删正文 → `read_body` **返回说明文字**，不抛异常
  R2  改文件名 → **自动修回** `file_path` 并照常返回正文
  R3  改项目文件夹名 → 同样自动修回
  R4  整个 `projects/` 被挪走 → `recall` / `inject` **照常工作**（它们不读文件）
  R5  编码本身不存在 → **仍然抛 ValueError**（那是真错误，不该被当成"文件问题"吞掉）
  R6  文件内容被人类改写 → 系统**读人类的新内容**（文件是权威，系统不覆盖它）
  R7  正文文件被改成乱码 → 返回可读提示，不崩
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import mcp_server  # noqa: E402

CID = "01RESILIENCETEST000000001"


class ResilienceTest(unittest.TestCase):

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (CID, "韧性测试", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('RES','RES','',0,'active')")
        self.conn.commit()
        self.srv = mcp_server.FishServer(self.root)
        self.anon = self.srv._resolve_identity(None)
        self.code, self.rel = fish.record_memory(
            self.conn, self.root, project="RES", title="韧性测试正文",
            body="# 韧性\n\n这段正文不该被系统碰。\n", mem_type="fact", owner=CID)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    def _fp(self):
        return self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (self.code,)).fetchone()[0]

    def _read(self):
        return self.srv.tool_read_body({"code": self.code}, self.anon)

    # ------------------------------------------------------------------ R1
    def test_R1_deleted_body_does_not_crash(self):
        """**删掉正文文件 → 返回说明，不抛异常**（这是用户的首要要求）。"""
        (self.root / self._fp()).unlink()
        out = self._read()                     # 不 raise 就是通过
        self.assertIn("正文缺失", out)
        # 说明要讲清"什么还在、什么没了"
        self.assertIn("标题", out)
        self.assertIn("事件", out)
        self.assertIn("只有正文内容不在库内", out, "必须如实说明丢了什么")

    # ------------------------------------------------------------------ R2
    def test_R2_renamed_file_self_heals(self):
        """人类改了文件名 → **系统自己找回来并修正记录**。"""
        old = self.root / self._fp()
        new = old.with_name(old.stem + "-副本.md")
        old.rename(new)
        out = self._read()
        self.assertIn("这段正文不该被系统碰", out, f"应读到正文：{out[:120]}")
        self.assertIn("已自动修回记录", out)
        # 记录真的被修好了
        self.assertEqual(self._fp(), str(new.relative_to(self.root)).replace("\\", "/"))
        self.assertTrue((self.root / self._fp()).exists())

    # ------------------------------------------------------------------ R3
    def test_R3_renamed_project_dir_self_heals(self):
        """人类改了**项目文件夹名** → 同样自己找回来。"""
        old_dir = self.root / "projects" / "RES"
        new_dir = self.root / "projects" / "RES-改名了"
        old_dir.rename(new_dir)
        out = self._read()
        self.assertIn("这段正文不该被系统碰", out)
        self.assertIn("已自动修回记录", out)
        self.assertIn("RES-改名了", self._fp())

    def test_R3_moved_into_archive_self_heals(self):
        """人类把文件挪进任意子目录（比如手动归档）→ 也能找回来。"""
        old = self.root / self._fp()
        sub = self.root / "projects" / "RES" / "archive" / "2026"
        sub.mkdir(parents=True)
        old.rename(sub / old.name)
        out = self._read()
        self.assertIn("这段正文不该被系统碰", out)
        self.assertIn("/archive/2026/", self._fp())

    # ------------------------------------------------------------------ R4
    def test_R4_projects_dir_gone_still_works(self):
        """整个 `projects/` 被挪走 → **核心机制照常**（它们不读文件）。"""
        shutil.move(str(self.root / "projects"), str(self.base / "projects-moved"))
        # 召回：走 FTS 索引，不碰文件
        rows, _ = fish.search_memories(self.conn, query="韧性", limit=5)
        self.assertTrue(rows, "召回不该因为目录没了就失效")
        # 注入包：走库，不碰文件
        pack, meta = fish.build_injection_pack(self.conn, self.root, None, project="RES")
        self.assertIsInstance(pack, str)
        self.assertIn("layers", meta)
        # 读正文：降级为说明，不崩
        self.assertIn("正文缺失", self._read())

    # ------------------------------------------------------------------ R5
    def test_R5_unknown_code_still_raises(self):
        """编码本身不存在 **仍然抛 ValueError**——那是真错误。

        这条是防"韧性做过头"：把"文件没了"和"这条记忆从来不存在"混为一谈，
        会让 AI 以为数据丢了、然后重记一条，制造重复。
        """
        with self.assertRaises(ValueError):
            self.srv.tool_read_body({"code": "RES-999999-99"}, self.anon)

    # ------------------------------------------------------------------ R6
    def test_R6_human_edited_content_wins(self):
        """人类改了正文内容 → **系统读人类的新内容**（文件是权威，系统不覆盖）。"""
        p = self.root / self._fp()
        p.write_text("# 我改过了\n\n人类说这段才算数。\n", encoding="utf-8")
        out = self._read()
        self.assertIn("人类说这段才算数", out)
        self.assertNotIn("这段正文不该被系统碰", out)

    # ------------------------------------------------------------------ R7
    def test_R7_garbled_encoding_does_not_crash(self):
        """正文被改成非 UTF-8 乱码 → 返回可读提示，不崩。"""
        p = self.root / self._fp()
        p.write_bytes(b"\xff\xfe\x00\x00 not utf-8 at all \x80\x81")
        out = self._read()                     # 不 raise 就是通过
        self.assertTrue(out, "至少要给一句话")
        self.assertTrue(("读不出来" in out) or ("正文缺失" in out), f"实际：{out[:120]}")

    # ------------------------------------------------------------------ 附加
    def test_locate_body_three_states(self):
        """`locate_body` 的三档返回值语义正确。"""
        fp = self._fp()
        state, p = fish.locate_body(self.root, self.code, fp)
        self.assertEqual(state, "exact")
        (self.root / fp).unlink()
        state, p = fish.locate_body(self.root, self.code, fp)
        self.assertEqual(state, "missing")
        self.assertIsNone(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
