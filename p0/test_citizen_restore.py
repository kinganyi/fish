#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-7 判据：归档公民必须能还原，且提示不许指向不存在的命令。

## 审查方实测的问题（已复核成立）

`resolve_owner_arg` 的报错原文是：

    指定的公民已归档：X（先 citizen-restore 再绑）

而 `fish` 从来没有 `citizen-restore` 这个命令——实测只有
`citizen-add / citizen-list / citizen-merge / citizen-retire`。

归档是**搬行**（INSERT 冷表 + DELETE 热表），没有还原入口就等于"不可逆"，
而错误提示还让人以为自己能做点什么。**指向不存在命令的提示比不给提示更坏**：
它把"我做不到"伪装成"你少做了一步"。

## 这批判据为什么能失败

第一条判据直接**解析 CLI 的报错文案**，把里面提到的命令拿去 `--help` 验一遍。
只要有人再写一句"先 xxx 再绑"而 xxx 不存在，它就红——这条不依赖具体实现，
只依赖"提示里的命令必须真的能用"。
"""
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_db       # noqa: E402


def _conn_with_archived(cid="C-ARCH1"):
    tmp = tempfile.mkdtemp(prefix="fish-restore-")
    data = Path(tmp) / "data"
    data.mkdir(parents=True)
    conn = fish.connect(data)
    conn.executescript(fish.SCHEMA)
    fish.migrate(conn)
    cols = ", ".join(fish._CITIZENS_ALL_COLUMNS)
    marks = ", ".join("?" for _ in fish._CITIZENS_ALL_COLUMNS)
    vals = {"citizen_id": cid, "display_name": "归档者", "kind": "visitor",
            "role": None, "host": "dsh", "status": "active",
            "created_at": fish.now_utc(), "is_owner": 0, "account_id": None,
            "device_id": None, "host_session_key": None,
            "last_seen_at": fish.now_utc(), "archived_at": fish.now_utc()}
    conn.execute(f"INSERT INTO citizens_archive ({cols}) VALUES ({marks})",
                 tuple(vals[c] for c in fish._CITIZENS_ALL_COLUMNS))
    conn.commit()
    return conn, tmp, data


class RestoreCitizenTest(unittest.TestCase):

    def setUp(self):
        self.conn, self._tmpdir, self.data = _conn_with_archived()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _counts(self):
        return (self.conn.execute("SELECT COUNT(*) FROM citizens_archive").fetchone()[0],
                self.conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0])

    # ------------------------------------------------------------- 主判据
    def test_restore_moves_row_back(self):
        """冷表 1/热表 0 → 还原后冷表 0/热表 1。"""
        self.assertEqual(self._counts(), (1, 0), "前置状态不对")
        fish.restore_citizen(self.conn, "C-ARCH1")
        self.assertEqual(self._counts(), (0, 1), "还原没有把行搬回来")

    def test_restored_row_has_no_archived_at(self):
        """**`archived_at` 必须清空**——留着会让下游误判它仍是已归档。"""
        fish.restore_citizen(self.conn, "C-ARCH1")
        v = self.conn.execute(
            "SELECT archived_at FROM citizens WHERE citizen_id='C-ARCH1'").fetchone()[0]
        self.assertIsNone(v, f"archived_at 应清空，实际 {v!r}")

    def test_restored_row_is_visible_through_the_view(self):
        """`citizens_all` 是下游的身份判定入口，还原后必须能看到它。"""
        fish.restore_citizen(self.conn, "C-ARCH1")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM citizens_all WHERE citizen_id='C-ARCH1'").fetchone()[0]
        self.assertEqual(n, 1, "还原后 citizens_all 视图看不到它")

    def test_restore_emits_an_event(self):
        """事件流是唯一事实源——还原必须留痕，且写明"凭证不复活"。"""
        fish.restore_citizen(self.conn, "C-ARCH1")
        row = self.conn.execute(
            "SELECT payload FROM events WHERE type='citizen.restored'").fetchone()
        self.assertIsNotNone(row, "还原没有写 citizen.restored 事件")
        self.assertIn("凭证", row[0], "事件里没写清「凭证不复活」——事后会被误读")

    def test_dry_run_changes_nothing(self):
        fish.restore_citizen(self.conn, "C-ARCH1", dry_run=True)
        self.assertEqual(self._counts(), (1, 0), "dry-run 改了库")

    def test_restoring_twice_is_a_clear_error(self):
        """重复还原要报"本来就在热表里"，而不是含糊的"找不到"。"""
        fish.restore_citizen(self.conn, "C-ARCH1")
        with self.assertRaises(ValueError) as ctx:
            fish.restore_citizen(self.conn, "C-ARCH1")
        self.assertIn("无需还原", str(ctx.exception))

    def test_unknown_citizen_is_a_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            fish.restore_citizen(self.conn, "C-NOPE")
        self.assertIn("找不到", str(ctx.exception))
        self.assertIn("citizen-list", str(ctx.exception), "报错要给出下一步")


class HintPointsAtRealCommandTest(unittest.TestCase):
    """**提示里提到的命令必须真实存在**——这条不依赖具体实现。"""

    def test_archived_error_mentions_an_existing_command(self):
        src = io.open(HERE / "fish.py", encoding="utf-8").read()
        # 找所有「先 xxx 再」形态的提示
        hints = re.findall(r"先\s*`?([a-z][a-z0-9-]{2,})`?\s*再", src)
        self.assertTrue(hints, "没找到任何「先 xxx 再」形态的提示（判据失去靶点）")
        # 收集 CLI 真实注册的子命令
        registered = set(re.findall(r'add_parser\("([a-z][a-z0-9-]*)"', src))
        missing = sorted(h for h in hints if h not in registered)
        self.assertEqual(missing, [],
                         f"这些提示指向不存在的命令：{missing}（注册的有 {sorted(registered)[:8]}…）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
