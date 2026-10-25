#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`rebuild_projection` 必须应用编码改名映射（2026-10-25）。

背景
----
数据治理把 153 条编码从 `ANYI-*` 改成了 `anyi-studio-*` / `大肥鱼生命系统-*`，
但 `events` 是 append-only（ADR-001），历史 `memory.recorded` 里存的是**旧编码**。

于是 `rebuild_projection` 一重放就会把编码**退回旧值**——迁移结果被一键撤销。
这是一个**定时炸弹**：任何人跑一次 `fish rebuild` 就炸。

修法：重放前先读 `code.renamed.batch` 事件的完整映射，重放时应用。

判据
----
  R1  rebuild 后编码**不退回去**（仍是映射后的新编码）
  R2  rebuild 后行数不变
  R3  映射应用计数 > 0（证明真的用上了，不是碰巧没重放）
  R4  变异：去掉映射应用 → R1 必须红（证明判据有判别力）
  R5  没有映射事件时行为不变（老库不受影响）
"""
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

OLD = "ANYI-261022-01"
NEW = "anyi-studio-261022-01"


class RebuildRenameTest(unittest.TestCase):

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
            (self.cid, "重建测试", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('anyi-studio','anyi-studio',NULL,0,'active')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------ 工具
    def _record_old(self):
        """造一条**旧编码**的 memory.recorded 事件（模拟治理前的历史）。"""
        body_dir = self.data / "projects" / "anyi-studio"
        body_dir.mkdir(parents=True, exist_ok=True)
        body = body_dir / (NEW + ".md")
        body.write_text("# 一条测试记忆\n", encoding="utf-8")
        payload = {
            "code": OLD, "title": "一条测试记忆",
            "body_ref": f"projects/anyi-studio/{NEW}.md",
            "tags": [], "visibility": "shared",
            "occurred_at": "2026-10-22T00:00:00Z",
        }
        seq = 1
        self.conn.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("EVT-1", seq, "2026-10-22T00:00:01Z", "memory.recorded",
             json.dumps({"citizen_id": self.cid, "evidence_level": "machine"}),
             json.dumps({"kind": "memory", "id": OLD}),
             json.dumps(payload, ensure_ascii=False), "", "evt@1"))
        self.conn.commit()

    def _write_mapping_event(self):
        seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        self.conn.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("EVT-RENAME", seq, "2026-10-25T00:00:00Z", "code.renamed.batch",
             json.dumps({"citizen_id": "fish-主AI-2", "evidence_level": "signed"}),
             json.dumps({"kind": "project", "id": "ANYI"}),
             json.dumps({"mapping": {OLD: NEW}, "count": 1}, ensure_ascii=False),
             "", "evt@1"))
        self.conn.commit()

    # ------------------------------------------------------------------ R1/R2/R3
    def test_R1_rebuild_keeps_new_code(self):
        """rebuild 之后编码必须仍是新编码——不能退回 ANYI-*。"""
        self._record_old()
        self._write_mapping_event()
        rep = fish.rebuild_projection(self.conn, self.data)
        codes = {r[0] for r in self.conn.execute("SELECT code FROM code_index")}
        self.assertIn(NEW, codes, f"重建后应保留新编码 {NEW}，实际 {sorted(codes)}")
        self.assertNotIn(OLD, codes, f"重建后不该退回旧编码 {OLD}，实际 {sorted(codes)}")
        self.assertGreater(rep.get("renamed_applied", 0), 0,
                           "renamed_applied 应 > 0（证明映射真的用上了）")

    def test_R2_row_count_unchanged(self):
        """rebuild 不改行数（**先跑一次建好投影，再测重建不改变它**）。

        第一版把 `before` 读在"投影还不存在"的时候（events 有了但没 rebuild 过），
        于是 before=0、after=1，判据红——是测试写错了，不是实现错。
        """
        self._record_old()
        self._write_mapping_event()
        fish.rebuild_projection(self.conn, self.data)     # 先建好投影
        before = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(before, 1, "夹具应产生 1 条投影")
        fish.rebuild_projection(self.conn, self.data)     # 再重建一次
        after = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(after, before, f"行数变了：{before} → {after}")

    def test_R3_mapping_size_reported(self):
        """报告里要能看见映射条目数——看不见就等于没法判断映射有没有生效。"""
        self._record_old()
        self._write_mapping_event()
        rep = fish.rebuild_projection(self.conn, self.data)
        self.assertEqual(rep.get("renamed_map_size"), 1,
                         f"映射条目数应为 1，实际 {rep.get('renamed_map_size')}")
        self.assertEqual(rep.get("renamed_events"), 1,
                         f"应读到 1 条 code.renamed.batch 事件，实际 {rep.get('renamed_events')}")

    # ------------------------------------------------------------------ R4 变异
    def test_R4_mutation_without_mapping_goes_red(self):
        """变异：**不写映射事件** → 重建必然退回旧编码（证明 R1 有判别力）。

        这条不是"测实现"，是把"为什么需要映射"钉死在判据里：
        如果哪天有人删掉映射应用逻辑，R1 会红；而这里证明"没有映射确实会退回去"。
        """
        self._record_old()          # 只造历史事件，不写映射
        fish.rebuild_projection(self.conn, self.data)
        codes = {r[0] for r in self.conn.execute("SELECT code FROM code_index")}
        self.assertIn(OLD, codes,
                      f"没有映射时重建应退回旧编码 {OLD}（这正是不修会炸的原因），实际 {sorted(codes)}")
        self.assertNotIn(NEW, codes, "没有映射时不该凭空出现新编码")

    # ------------------------------------------------------------------ R5
    def test_R5_no_mapping_event_is_noop(self):
        """没有映射事件的老库：行为不变（不会因为新代码而崩）。"""
        self._record_old()
        rep = fish.rebuild_projection(self.conn, self.data)
        self.assertEqual(rep.get("renamed_map_size"), 0)
        self.assertEqual(rep.get("renamed_applied"), 0)
        codes = {r[0] for r in self.conn.execute("SELECT code FROM code_index")}
        self.assertIn(OLD, codes, "老库没有映射，重建就该按原样还原")


if __name__ == "__main__":
    unittest.main(verbosity=2)
