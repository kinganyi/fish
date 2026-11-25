#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-2：重复编码 rebuild 保留最后一条 + projection-only 补事件工具。

## 判据

  D1 同一 code 有两条 `memory.recorded`（title=first / title=second）→ rebuild 后
     `code_index.title == "second"`，且报告 `duplicate_codes` 含该 code（不静默）
  D2 没有重复编码时 `duplicate_codes == []`（防止"每次都报重复"的假阳性）
  F1 工具 dry-run：列出 projection-only 行，**零写**
  F2 工具 --apply：追加 `memory.imported` 事件（cause=backfill:p2-2），投影行仍在
  F3 清空 code_index 后 rebuild：该行能**只靠事件**重建回来（不依赖快照）
  F4 第二次 --apply：delta=0（幂等）

变异自证见 `mutation_p2_2.py`（改回 `INSERT OR IGNORE` → D1 必红；把
`memory.imported` 改成 `memory.recorded` → F2 必红）。
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

import fish  # noqa: E402
import fish_rebuild  # noqa: E402
import backfill_projection_events as backfill  # noqa: E402


def _setup(testcase):
    testcase._tmp = tempfile.mkdtemp(prefix="fish-p22rebuild-")
    testcase.root = Path(testcase._tmp) / "data"
    testcase.conn = fish.connect(testcase.root)
    testcase.conn.executescript(fish.SCHEMA)
    fish.migrate(testcase.conn)
    testcase.conn.execute(
        "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
        "VALUES ('P','P',NULL,0,'active')")
    testcase.conn.commit()


class DuplicateCodesTest(unittest.TestCase):

    def setUp(self):
        _setup(self)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _event(self, code, title, seq, occurred_at=None):
        payload = {"code": code, "title": title, "body_ref": "projects/P/x.md"}
        if occurred_at:
            payload["occurred_at"] = occurred_at
        self.conn.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"EV-{seq}", seq, "2026-11-26T00:00:0%dZ" % seq, "memory.recorded",
             json.dumps({"citizen_id": "", "evidence_level": "machine"}),
             json.dumps({"kind": "memory", "id": code}),
             json.dumps(payload, ensure_ascii=False), "test", "evt@1"))

    def test_D1_last_event_wins_and_reported(self):
        self._event("P-261126-01", "first", 1)
        self._event("P-261126-01", "second", 2)
        self.conn.commit()
        rep = fish_rebuild.rebuild_projection(self.conn, self.root, apply=True)
        row = self.conn.execute(
            "SELECT title FROM code_index WHERE code='P-261126-01'").fetchone()
        self.assertIsNotNone(row, "rebuild 后该编码消失了")
        self.assertEqual(row[0], "second",
                         f"重复编码应保留最后一条事件，实际 title={row[0]!r}")
        self.assertIn("P-261126-01", rep.get("duplicate_codes", []),
                      f"duplicate_codes 没记录：{rep.get('duplicate_codes')}")
        self.assertGreaterEqual(rep.get("duplicate_events", 0), 1,
                                f"duplicate_events 没计数：{rep.get('duplicate_events')}")

    def test_D2_no_duplicates_not_reported(self):
        self._event("P-261126-02", "only", 1)
        self.conn.commit()
        rep = fish_rebuild.rebuild_projection(self.conn, self.root, apply=True)
        self.assertEqual(rep.get("duplicate_codes"), [],
                         f"没有重复编码却报了：{rep.get('duplicate_codes')}")

    def test_D3_duplicate_occurred_at_change_is_not_a_mismatch(self):
        """重复编码按最后一条事件 → occurred_at 变了是**预期**，不许判成对账不一致。"""
        self._event("P-261126-03", "first", 1, occurred_at="2026-11-26T00:00:00Z")
        self.conn.commit()
        fish_rebuild.rebuild_projection(self.conn, self.root, apply=True)   # 物化 first
        self._event("P-261126-03", "second", 2, occurred_at="2026-11-26T05:00:00Z")
        self.conn.commit()
        rep = fish_rebuild.rebuild_projection(self.conn, self.root, apply=True)
        self.assertEqual(rep.get("mismatch"), [],
                         f"重复编码的时间变化被误判为不一致：{rep.get('mismatch')}")
        row = self.conn.execute(
            "SELECT title,occurred_at FROM code_index WHERE code='P-261126-03'").fetchone()
        self.assertEqual(row[0], "second")
        self.assertEqual(row[1], "2026-11-26T05:00:00Z")


class BackfillProjectionEventsTest(unittest.TestCase):

    CODE = "P-261126-90"
    TITLE = "只有投影的行"
    REL = "projects/P/P-261126-90__只有投影的行.md"

    def setUp(self):
        _setup(self)
        p = self.root / self.REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# 只有投影的行\n\n正文内容\n", encoding="utf-8")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.CODE, self.TITLE, "P", "OWNER1", "fact", "shared", "active", "[]",
             self.REL, "2026-11-26T00:00:00Z", "2026-11-26T00:00:00Z"))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = backfill.main(["--data", str(self.root), "--json"] + list(args))
        return rc, json.loads(buf.getvalue().strip())

    def _event_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def test_F1_dry_run_lists_and_writes_nothing(self):
        before = self._event_count()
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out["count"], 1, f"dry-run 没列出 projection-only：{out}")
        self.assertEqual(out["rows"][0]["code"], self.CODE)
        self.assertEqual(out["written"], 0)
        self.assertEqual(self._event_count(), before, "dry-run 写了事件——违反零写承诺")
        self.assertFalse((self.root / "_backups").exists(),
                         "dry-run 不该生成备份")

    def test_F2_apply_appends_imported_event(self):
        rc, out = self._run("--apply")
        self.assertEqual(rc, 0, f"apply 失败：{out}")
        self.assertEqual(out["written"], 1)
        row = self.conn.execute(
            "SELECT type,cause,payload FROM events WHERE type='memory.imported'").fetchone()
        self.assertIsNotNone(row, "apply 后没有 memory.imported 事件")
        self.assertEqual(row[1], "backfill:p2-2")
        payload = json.loads(row[2])
        for key in ("code", "title", "body_ref", "body_sha256", "owner", "status"):
            self.assertIn(key, payload, f"payload 缺 {key}：{payload}")
        self.assertEqual(payload["code"], self.CODE)
        self.assertEqual(payload["status"], "active")
        # 行本身必须还在
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM code_index WHERE code=?", (self.CODE,)).fetchone())

    def test_F3_rebuild_recreates_row_from_event_only(self):
        rc, out = self._run("--apply")
        self.assertEqual(rc, 0, f"apply 失败：{out}")
        self.conn.execute("DELETE FROM code_index")
        self.conn.commit()
        rep = fish_rebuild.rebuild_projection(self.conn, self.root, apply=True)
        row = self.conn.execute(
            "SELECT title,owner,status,file_path FROM code_index WHERE code=?",
            (self.CODE,)).fetchone()
        self.assertIsNotNone(row, f"只靠事件重建丢了该行（rep={rep}）")
        self.assertEqual(row[0], self.TITLE)
        self.assertEqual(row[1], "OWNER1")
        self.assertEqual(row[2], "active")
        self.assertEqual(row[3], self.REL)
        self.assertGreaterEqual(rep.get("projected", 0), 1)

    def test_F4_second_apply_delta_zero(self):
        rc1, out1 = self._run("--apply")
        self.assertEqual((rc1, out1["written"]), (0, 1))
        before = self._event_count()
        rc2, out2 = self._run("--apply")
        self.assertEqual(rc2, 0)
        self.assertEqual(out2["written"], 0, f"第二次 apply 不是 delta=0：{out2}")
        self.assertEqual(self._event_count(), before, "第二次 apply 又写了事件")

    def test_F5_apply_never_touches_history(self):
        """补齐只允许 INSERT：已有事件的 seq/type/payload 必须逐字节不变。"""
        fish.append_event(
            self.conn, "memory.recorded",
            {"citizen_id": "OWNER1", "evidence_level": "machine"},
            {"kind": "memory", "id": "P-261126-91"},
            {"code": "P-261126-91", "title": "老事件", "body_ref": "projects/P/x.md"},
            cause="old", commit=False)
        self.conn.commit()
        old = self.conn.execute(
            "SELECT seq,event_id,type,payload FROM events ORDER BY seq").fetchall()
        rc, _out = self._run("--apply")
        self.assertEqual(rc, 0)
        now = self.conn.execute(
            "SELECT seq,event_id,type,payload FROM events WHERE event_id IN (%s) ORDER BY seq"
            % ",".join("?" * len(old)), tuple(r[1] for r in old)).fetchall()
        self.assertEqual(now, old, "补齐工具改了历史事件——append-only 契约被破坏")


if __name__ == "__main__":
    unittest.main(verbosity=2)
