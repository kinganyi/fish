#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文归档：把已失效的记忆移进 `archive/<年>/`（2026-10-25 · 用户要求）。

## 需求原文

> 在很久很久以后，这种平铺的形式应该不会影响 AI 的查询速度吧？
> 能不能在不影响我们目前的记忆、推送等机制的前提下，在项目文件夹内增加一个
> 归档文件夹，依照系统已有的规则，把相关文档移入到归档文件夹里面，
> 但是不影响全部工具和其他机制（目的是便于人类识别）

## 为什么"不影响机制"是可以证明的（不是拍胸脯）

1. **全仓零目录遍历**：`os.walk` / `listdir` / `glob(` 命中数 = 0。
   所有读路径都走 `code_index.file_path` 这个字段 —— **字段指到哪就读到哪**。
2. **推送/召回硬过滤 `status='active'`**：`superseded` / `withdrawn` 本来就不在
   它们的视野里，所以只要归档**不动 active**，那些机制的行为一个字节都不变。
3. **`rebuild_projection` 快照优先**：只要 `file_path` 指向的文件存在，
   重建就保留它，不会算回平铺位置（这条是今天下午修 rebuild 断链时加的）。

## 判据

  A1  `archive_relpath` **只换目录、不改文件名**（slug 重算会漂移）
  A2  归档后文件真的在新位置、旧位置真的没有了
  A3  **`read_body` 仍可读**（走 file_path）
  A4  **`rebuild` 不会把它移回平铺**（核心回归）
  A5  **status 一个字都不改**（归档是位置，不是语义）
  A6  **幂等**：重复归档 → `already`，不报错、不动文件
  A7  **只动 superseded/withdrawn，active 一条不动**
  A8  留痕：追加 `memory.archived` 事件（可追溯、可回滚）
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

CID = "01ARCHIVETEST000000000001"


class ArchiveTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (CID, "归档测试", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('ARCH','ARCH','',0,'active')")
        self.conn.commit()
        self.live = self._mk("活着的记忆", "active")
        self.dead = self._mk("已失效的记忆", "superseded")
        self.gone = self._mk("已撤回的记忆", "withdrawn")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def _mk(self, title, status):
        code, rel = fish.record_memory(
            self.conn, self.root, project="ARCH", title=title,
            body=f"# {title}\n\n正文内容。\n", mem_type="fact", owner=CID)
        if status != "active":
            self.conn.execute("UPDATE code_index SET status=? WHERE code=?", (status, code))
            self.conn.commit()
        return code

    def _fp(self, code):
        return self.conn.execute(
            "SELECT file_path FROM code_index WHERE code=?", (code,)).fetchone()[0]

    # ------------------------------------------------------------------ A1
    def test_A1_only_directory_changes(self):
        """只换目录，**文件名一个字不改**（slug 重算是断链 73 条的根因）。"""
        old = f"projects/ARCH/{self.dead}__已失效的记忆.md"
        new = fish.archive_relpath(old, "2026")
        self.assertEqual(new, f"projects/ARCH/archive/2026/{self.dead}__已失效的记忆.md")
        self.assertEqual(Path(new).name, Path(old).name, "文件名必须一字不改")

    def test_A1_rejects_unknown_shape(self):
        with self.assertRaises(ValueError):
            fish.archive_relpath("somewhere/else/file.md", "2026")

    # ------------------------------------------------------------------ A2
    def test_A2_file_really_moves(self):
        old_fp = self._fp(self.dead)
        r = fish.archive_body(self.conn, self.root, self.dead)
        self.assertTrue(r["moved"])
        self.assertIn("/archive/", r["to"])
        self.assertTrue((self.root / r["to"]).exists(), "新位置应有文件")
        self.assertFalse((self.root / old_fp).exists(), "旧位置不该再有文件")

    # ------------------------------------------------------------------ A3
    def test_A3_body_still_readable(self):
        """归档后正文**仍然可读**——读路径走 file_path，与位置无关。"""
        fish.archive_body(self.conn, self.root, self.dead)
        fp = self._fp(self.dead)
        p = self.root / fp
        self.assertTrue(p.exists())
        self.assertIn("正文内容", p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ A4
    def test_A4_rebuild_does_not_move_back(self):
        """**核心回归**：rebuild 不能把已归档的文件算回平铺位置。

        这条如果不成立，归档就是一次性的——下次 rebuild 全部搬回来。
        """
        fish.archive_body(self.conn, self.root, self.dead)
        archived_fp = self._fp(self.dead)
        self.assertIn("/archive/", archived_fp)
        fish.rebuild_projection(self.conn, self.root)
        after = self._fp(self.dead)
        self.assertEqual(after, archived_fp, f"rebuild 把路径改了：{archived_fp} → {after}")
        self.assertTrue((self.root / after).exists())

    # ------------------------------------------------------------------ A5
    def test_A5_status_unchanged(self):
        """归档是**位置**，不是语义——`status` 一个字都不改。"""
        before = self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (self.dead,)).fetchone()[0]
        fish.archive_body(self.conn, self.root, self.dead)
        after = self.conn.execute(
            "SELECT status FROM code_index WHERE code=?", (self.dead,)).fetchone()[0]
        self.assertEqual(before, after, "归档不该改 status")

    # ------------------------------------------------------------------ A6
    def test_A6_idempotent(self):
        """重复归档 → 返回 `already`，**不报错、不动文件**。"""
        r1 = fish.archive_body(self.conn, self.root, self.dead)
        self.assertTrue(r1["moved"])
        fp1 = self._fp(self.dead)
        r2 = fish.archive_body(self.conn, self.root, self.dead)
        self.assertFalse(r2["moved"])
        self.assertTrue(r2.get("already"), f"第二次应标 already：{r2}")
        self.assertEqual(self._fp(self.dead), fp1, "路径不该变")

    def test_A6_sweep_counts_already_separately(self):
        """sweep 第二次必须报 `moved=0` + `already=N`（否则是假读数）。"""
        r1 = fish.archive_sweep(self.conn, self.root)
        self.assertEqual(r1["moved"], 2, f"应移动 2 条（superseded+withdrawn），实际 {r1}")
        r2 = fish.archive_sweep(self.conn, self.root)
        self.assertEqual(r2["moved"], 0, "第二次不该再移动")
        self.assertEqual(r2["already"], 2, "第二次应报已在归档区")

    # ------------------------------------------------------------------ A7
    def test_A7_active_untouched(self):
        """**只动 superseded / withdrawn，active 一条不碰**——这是"不影响推送"的红线。"""
        live_fp = self._fp(self.live)
        fish.archive_sweep(self.conn, self.root)
        self.assertEqual(self._fp(self.live), live_fp, "active 的路径不该变")
        self.assertNotIn("/archive/", self._fp(self.live))
        self.assertTrue((self.root / live_fp).exists())

    def test_A7_active_invisible_to_archive(self):
        """`active` 不该出现在可归档清单里（`archive list` 的口径）。"""
        rows = self.conn.execute(
            "SELECT code FROM code_index WHERE status IN ('superseded','withdrawn')"
        ).fetchall()
        codes = {r[0] for r in rows}
        self.assertNotIn(self.live, codes)
        self.assertIn(self.dead, codes)

    def test_A7_no_withdrawn_option(self):
        """`include_withdrawn=False` 时只归档 superseded。"""
        fish.archive_sweep(self.conn, self.root, include_withdrawn=False)
        self.assertIn("/archive/", self._fp(self.dead))
        self.assertNotIn("/archive/", self._fp(self.gone), "withdrawn 不该被归档")

    # ------------------------------------------------------------------ A8
    def test_A8_event_recorded(self):
        """留痕：追加 `memory.archived`，里面记着 from/to（可追溯、可回滚）。"""
        fish.archive_body(self.conn, self.root, self.dead)
        row = self.conn.execute(
            "SELECT payload FROM events WHERE type='memory.archived'").fetchone()
        self.assertIsNotNone(row, "应有 memory.archived 事件")
        import json
        p = json.loads(row[0])
        self.assertEqual(p["code"], self.dead)
        self.assertIn("/archive/", p["to"])
        self.assertNotIn("/archive/", p["from"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
