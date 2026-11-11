#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 🔴9 的判据：`rebuild_projection` 必须重放归属变更事件。

问题（报告 60 §3，07 D-10 记载已造成真实损伤）：
`citizen.merged` / `citizen.retired{--migrate-to}` 已经把归属搬走了，但 rebuild
原先只重放 `memory.owner_fixed`，**不重放这两类事件** ⇒ **一次 rebuild 就静默
撤销人工合并**：owner 回到原始 actor 的公民，而那个公民此时已是
`merged_into:<dst>` / 退役态，留下指向已退役公民的幽灵归属。

判据设计（成对，任一侧失效都会红）：
  R9-1  无合并事件时，rebuild 后 owner 保持原值（对照组，防"无条件改写"）
  R9-2  有 citizen.merged 事件时，rebuild 后 owner 解析到 dst（核心回归）
  R9-3  citizen.retired{--migrate-to} 同样重放
  R9-4  合并链 A→B→C 解析到 C（链式，与 citizen_merge 的 8 跳上限一致）
  R9-5  环 A→B→A 不死循环、返回链上某个值而不挂
  R9-6  映射层单测：load_ownership_merge_map 的两类事件来源都认
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402
import fish_trust  # noqa: E402


class RebuildMergeReplayTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-r9-"))
        self.root = self.tmp
        self.conn = fish.connect(self.tmp / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.src = fish.ulid()
        self.dst = fish.ulid()
        for cid, name in ((self.src, "源"), (self.dst, "目标")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
                (cid, name, "active"))
        self.code = "r9-261111-01"
        # 用最小构件造一条 memory.recorded：rebuild 只读 payload.code 与 actor.citizen_id
        fish.append_event(
            self.conn, "memory.recorded",
            {"citizen_id": self.src}, {"kind": "memory", "id": self.code},
            {"code": self.code, "title": "归属重放判据",
             "occurred_at": "2026-11-01T00:00:00Z"})
        # 先把投影物化出来（真实时序：投影存在 → 合并迁移它 → 再 rebuild 必须保住）
        fish.rebuild_projection(self.conn, self.root)
        assert self._owner() == self.src, "前置：初始投影的 owner 应为 src"

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _owner(self):
        row = self.conn.execute(
            "SELECT owner FROM code_index WHERE code=?", (self.code,)).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- R9-1
    def test_R9_1_no_merge_keeps_owner(self):
        """对照组：没有归属变更事件时，rebuild 不得改写 owner。

        这条防的是"为了让 R9-2 过而无条件改写"——那样本组会红。
        """
        fish.rebuild_projection(self.conn, self.root)
        self.assertEqual(self._owner(), self.src)

    # ---------------------------------------------------------------- R9-2
    def test_R9_2_merge_event_replayed(self):
        """核心回归：合并之后 rebuild，owner 必须仍是 dst（不得退回 src）。"""
        fish.citizen_merge(self.conn, src=self.src, dst=self.dst, apply=True)
        self.assertEqual(self._owner(), self.dst, "前置：合并应已把 owner 迁到 dst")
        rep = fish.rebuild_projection(self.conn, self.root)
        self.assertEqual(
            self._owner(), self.dst,
            "🔴9 回归：rebuild 把人工合并撤销了（owner 退回已退役的 src）")
        self.assertGreaterEqual(rep["merged_events"], 1,
                                "报告应记下重放的归属变更事件数")

    # ---------------------------------------------------------------- R9-3
    def test_R9_3_retired_migrated_to_replayed(self):
        """citizen.retired 带 --migrate-to 时同样要重放。"""
        fish.append_event(
            self.conn, "citizen.retired",
            {"citizen_id": "system", "evidence_level": "signed"},
            {"kind": "citizen", "id": self.src},
            {"display_name": "源", "kind": "visitor", "reason": "manual",
             "migrated_to": self.dst, "memories": 1}, commit=False)
        self.conn.commit()
        fish.rebuild_projection(self.conn, self.root)
        self.assertEqual(
            self._owner(), self.dst,
            "🔴9 回归：rebuild 把 retired{--migrate-to} 的迁移撤销了")

    # ---------------------------------------------------------------- R9-4
    def test_R9_4_chain_resolves_to_final(self):
        """合并链 A→B→C 必须解析到 C（不能只跳一跳）。"""
        mid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
            (mid, "中间", "active"))
        m, n = fish_trust.load_ownership_merge_map(self.conn)
        m = dict(m)
        m[self.src] = mid
        m[mid] = self.dst
        self.assertEqual(fish_trust.resolve_merged(m, self.src), self.dst)

    # ---------------------------------------------------------------- R9-5
    def test_R9_5_cycle_terminates(self):
        """环 A→B→A 不能死循环（返回链上某个值即可，但必须终止）。"""
        cyc = {self.src: self.dst, self.dst: self.src}
        got = fish_trust.resolve_merged(cyc, self.src)
        self.assertIn(got, (self.src, self.dst))

    # ---------------------------------------------------------------- R9-6
    def test_R9_6_map_reads_both_event_types(self):
        """映射层单测：merged 与 retired{migrated_to} 两类来源都要认。"""
        third = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, status) VALUES (?,?,?)",
            (third, "第三", "active"))
        fish.citizen_merge(self.conn, src=self.src, dst=self.dst, apply=True)
        fish.append_event(
            self.conn, "citizen.retired",
            {"citizen_id": "system"},
            {"kind": "citizen", "id": third},
            {"migrated_to": self.dst, "reason": "manual"}, commit=False)
        self.conn.commit()
        m, counted = fish_trust.load_ownership_merge_map(self.conn)
        self.assertEqual(m.get(self.src), self.dst, "citizen.merged 未被识别")
        self.assertEqual(m.get(third), self.dst,
                         "citizen.retired{migrated_to} 未被识别")
        self.assertGreaterEqual(counted, 2)


if __name__ == "__main__":
    unittest.main()
