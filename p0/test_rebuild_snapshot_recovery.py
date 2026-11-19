#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F-P2-9：投影行没有 `memory.recorded` 事件时，rebuild 不许静默丢。

## 审查方说的是 4 条，实测是 131 条

外部审查（2026-11-18）写的是「4 条投影行无 memory.recorded 事件，codes
anyi-studio-261025-01 等」。**2026-11-19 实测生产库：131 条 / 556 条 active
（24%）**。所以"4 条"这个数字不对，但**问题本身是真的，而且大得多**。

这些行来自历史治理脚本直接写投影（不是走 `record_memory`），
所以事件流里没有它们的 `memory.recorded`。

## 为什么这不等于"数据会丢"

`fish_rebuild.py` 有一步 **4b「快照补回」**：重放事件之后，事件里找不到来源的条目
会用快照补回整行，并计入 `restored_from_snapshot`——**不静默丢**。

所以真正要守住的**不是**"每条都必须有事件"（那要回填 131 条历史），
而是**"重建时不许丢"**。这条判据守的是后者。

## 将来要更强的话

给那 131 条回填 `memory.recorded` 事件，并把 write 入口收成单一路径——
那是"事件流是唯一事实源"的强口径。**本轮不做**：回填历史事件会改事件流，
需要单独评估，不该塞进一次判据修复里。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fish  # noqa: E402
import fish_rebuild  # noqa: E402


class RebuildSnapshotRecoveryTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="fish-p29-")
        self.root = Path(self._tmp) / "data"
        self.root.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-19',0)")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass

    def test_projection_row_without_event_survives_rebuild(self):
        """**核心**：没有事件的投影行，重建后必须还在，且被计数。"""
        # 一条正常记忆（有事件）
        ok_code, _ = fish.record_memory(
            self.conn, self.root, project="P", title="有事件的行",
            body="正文\\n", mem_type="fact")
        self.conn.commit()
        # 一条**直接写投影**的记忆（造出"无事件"的现场，模拟历史治理脚本）
        ghost = "P-261119-99"
        self.conn.execute(
            "INSERT INTO code_index (code, project, title, mem_type, status,"
            " file_path, recorded_at) VALUES (?,?,?,?,?,?,?)",
            (ghost, "P", "无事件的行", "fact", "active",
             "projects/P/P-261119-99__无事件的行.md", fish.now_utc()))
        self.conn.commit()
        before = {r[0] for r in self.conn.execute(
            "SELECT code FROM code_index WHERE status='active'").fetchall()}
        self.assertIn(ghost, before, "夹具没生效——那条无事件的行没写进去")

        rep = fish_rebuild.rebuild_projection(self.conn, self.root)
        after = {r[0] for r in self.conn.execute(
            "SELECT code FROM code_index WHERE status='active'").fetchall()}
        self.assertIn(ghost, after,
                      f"重建把「无事件」的行丢了——{ghost} 不在重建结果里")
        self.assertIn(ok_code, after, "重建把有事件的行也弄丢了")
        # 它必须是**被计数**地补回，不是碰巧没删
        self.assertGreaterEqual(
            rep.get("restored_from_snapshot", 0), 1,
            f"补回没被计数——静默恢复和静默丢失一样难查：{rep}")

    def test_rebuild_reports_recovered_list(self):
        """补回的条目要**列出来**（`recovered`），否则人不知道发生过。"""
        ghost = "P-261119-98"
        self.conn.execute(
            "INSERT INTO code_index (code, project, title, mem_type, status,"
            " file_path, recorded_at) VALUES (?,?,?,?,?,?,?)",
            (ghost, "P", "无事件的另一行", "fact", "active",
             "projects/P/P-261119-98__x.md", fish.now_utc()))
        self.conn.commit()
        rep = fish_rebuild.rebuild_projection(self.conn, self.root)
        self.assertTrue(rep.get("recovered"),
                        f"补回清单是空的——那就查不到丢了什么：{rep}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
