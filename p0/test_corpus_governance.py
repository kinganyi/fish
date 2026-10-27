#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-P0-7 / C-P0-8 治理与体检判据（T05 · 设计 §8/§10）。

两条底线：

1. **脱敏三道都在**：落盘前替换（不丢段）、出口二次（入库漏了出口补）、
   体检硬门槛（`FISH_CORPUS_NO_REDACT=1` 时必须红）。关掉脱敏还能体检通过，
   那条判据就只是装饰品（C-P0-7 红①）。
2. **删了不复活**：`forget` 先写台账再删段；`rebuild` 必须尊重台账（C-P0-8 ①），
   审计里**永不出现 query 原文**（C-P0-8 ②）。

变异体：M13 关出口二次脱敏 / M14 脱敏改成丢整段 / M15 rebuild 忽略台账 /
M16 审计落 query 原文 / M17 源缺失回填摘要（在 `test_corpus_search` 里）。
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import zstandard

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_common  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_gov as gov  # noqa: E402
import fish_corpus_redact as redact_mod  # noqa: E402
import fish_corpus_store as store  # noqa: E402

MS = 1790918381967
CID = "01CITIZEN00000000000GOV1"
PROJECT = "PJ"
SECRET = "fsh_" + "G" * 24
DIRTY = ("先说结论：这条判据的真值是 overlap。顺手记一下凭证 %s 与密码：abcd1234，"
         "以后别再贴进对话里了（这段故意带秘密，用来验证三道脱敏）。" % SECRET)
LONG = ("撞车判据的唯一真值是 overlap：先比目录前缀、再比文件后缀，同一份声明窗口内"
        "出现两条就算撞车。判定不看谁先签到——这条结论在历史会话里反复确认过。")


def frame(payload) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return zstandard.ZstdCompressor().compress(blob + b"\n")


def ev_session(sid, cwd="I:/app/gov-proj"):
    return {"type": "session", "version": 4, "id": sid, "createdAt": MS, "cwd": cwd}


def ev_user(seq, text):
    return {"type": "user/message", "seq": seq, "time": MS + seq * 10,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": "user"}, "role": "user", "id": "u%d" % seq}}


def ev_assistant(seq, text):
    return {"type": "assistant/message", "seq": seq, "time": MS + seq * 10,
            "data": {"turn": 1, "step": 1,
                     "message": {"role": "assistant", "content": [
                         {"type": "text", "text": text}], "id": "a%d" % seq}}}


class GovCase(unittest.TestCase):
    """临时根 + 两个会话（一个干净、一个带秘密）。"""

    SID = "3931d23f-17c9-4de8-87b0-d7b782e70cf0"
    SID2 = "7a1c0002-17c9-4de8-87b0-000000000002"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-gov-"))
        self.root = self.tmp / "data"
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir(parents=True)
        self.conn = store.open_db(self.root)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 造文件与采集
    def write_session(self, sid, events, ws="--I-app-gov-proj--"):
        folder = self.sessions / ws / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        path.write_bytes(b"".join(frame(ev) for ev in events))
        return path

    def ingest(self):
        return fish_corpus.ingest(("dsh",), root=self.root, conn=self.conn,
                                 roots={"dsh": [self.sessions]}, use_lock=False,
                                 project_of=lambda cwd: PROJECT,
                                 citizen_of=lambda key: CID)

    def setup_corpus(self, *, dirty=False):
        self.write_session(self.SID, [ev_session(self.SID), ev_user(9, "撞车判据怎么做"),
                                      ev_assistant(10, LONG)])
        self.write_session(self.SID2, [ev_session(self.SID2), ev_user(1, "撞车判据里的凭证"),
                                       ev_assistant(2, DIRTY if dirty else LONG)],
                           ws="--I-app-gov-proj2--")
        self.ingest()

    def seg_count(self, sid=None):
        if sid:
            return store.count_segments(self.conn, "dsh", sid)
        return int(self.conn.execute("SELECT COUNT(*) FROM corpus_segment").fetchone()[0])

    def rows(self, table):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM %s" % table).fetchall()]

    def rebuild(self, **kw):
        kw.setdefault("root", self.root)
        kw.setdefault("hosts", ("dsh",))
        kw.setdefault("roots", {"dsh": [self.sessions]})
        kw.setdefault("project_of", lambda cwd: PROJECT)
        kw.setdefault("citizen_of", lambda key: CID)
        return fish_corpus.rebuild(**kw)


class RedactTest(GovCase):
    """C-P0-7：落盘前脱敏 + 出口二次 + 体检门槛。"""

    def test_plaintext_secrets_never_reach_disk(self):
        path = self.write_session(self.SID2, [ev_session(self.SID2),
                                              ev_assistant(2, DIRTY)])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.ingest()
        stored = " ".join(row["text"] for row in self.rows("corpus_segment"))
        self.assertNotIn(SECRET, stored)
        self.assertNotIn("abcd1234", stored)
        self.assertIn("已脱敏", stored)
        self.assertEqual(redact_mod.scan_count(stored), 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before,
                         "采集不许改写源日志（O7：源只读，脱敏兜在采集侧）")

    def test_redaction_never_drops_a_segment(self):
        """M14：脱敏是"替换"不是"丢弃"——段数骤降比漏脱敏更难发现。"""
        self.setup_corpus(dirty=True)
        self.assertEqual(self.seg_count(self.SID2), 2)
        for row in self.rows("corpus_segment"):
            self.assertTrue(row["text"].strip())
        self.assertEqual(self.rows("corpus_segment")[0]["redacted_count"], 0)
        dirty_row = [row for row in self.rows("corpus_segment")
                     if row["session_id"] == self.SID2][-1]
        self.assertGreater(dirty_row["redacted_count"], 0)

    def test_outlet_redaction_catches_store_leak(self):
        """M13：假设入库那一道漏了（直接塞一段带秘密的正文）——出口必须补上。"""
        self.setup_corpus()
        store.upsert_segment(self.conn, {
            "host": "dsh", "session_id": self.SID, "loc": "seq=99", "part": 0,
            "role": "assistant", "occurred_at": "2026-10-27T00:00:00Z",
            "text": "入库漏了的火种：%s" % SECRET, "chars": 40, "redacted_count": 0,
            "raw_sha256": store.text_sha256(SECRET), "byte_from": 0, "byte_to": 1,
            "offset_space": "decompressed", "shadowed": 0})
        self.conn.commit()
        res = fish_corpus.search("火种", root=self.root)
        self.assertTrue(res["hits"])
        self.assertNotIn(SECRET, res["hits"][0]["snippet"])
        self.assertIn("已脱敏", res["hits"][0]["snippet"])
        read = fish_corpus.read("log:dsh:%s#seq=99" % self.SID, root=self.root)
        self.assertNotIn(SECRET, read["text"])
        self.assertGreaterEqual(read["redacted_count"], 1)

    def test_doctor_is_red_when_redaction_disabled(self):
        """C-P0-7 红①：关掉脱敏跑体检**必须红**（否则门槛是装饰品）。"""
        old = os.environ.get(redact_mod.ENV_DISABLE)
        try:
            os.environ[redact_mod.ENV_DISABLE] = "1"
            self.write_session(self.SID2, [ev_session(self.SID2),
                                           ev_assistant(2, DIRTY)])
            self.ingest()
            self.assertIn(SECRET, " ".join(row["text"] for row in self.rows("corpus_segment")))
            res = fish_corpus.doctor(root=self.root)
            self.assertFalse(res["ok"])
            self.assertIn("redact_disabled", res["reasons"])
            self.assertIn("plaintext_secrets", res["reasons"])
            rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
            self.assertNotEqual(rc, 0, lines)
        finally:
            if old is None:
                os.environ.pop(redact_mod.ENV_DISABLE, None)
            else:
                os.environ[redact_mod.ENV_DISABLE] = old

    def test_doctor_ok_after_clean_forget(self):
        self.setup_corpus(dirty=True)
        fish_corpus.forget({"type": "host", "host": "dsh"}, root=self.root)
        self.assertEqual(self.seg_count(), 0)
        res = fish_corpus.doctor(root=self.root)
        self.assertTrue(res["ok"], res["reasons"])
        rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
        self.assertEqual(rc, 0, lines)

    def test_doctor_reports_fts_gap(self):
        """"索引落后"必须报出来（那是"存进去搜不到"的根因，只有对账能发现）。"""
        self.setup_corpus()
        self.conn.execute("DELETE FROM corpus_segment WHERE loc='seq=99'")
        self.conn.execute(
            "DELETE FROM corpus_segment WHERE id=(SELECT MAX(id) FROM corpus_segment)")
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertNotEqual(res["fts_gap"], 0)
        self.assertIn("fts_gap", res["reasons"])

    def test_doctor_without_db_is_ok_and_declared(self):
        res = fish_corpus.doctor(root=self.tmp / "nothing-here")
        self.assertTrue(res["ok"])
        self.assertTrue(res["db_missing"])


class ForgetTest(GovCase):
    """C-P0-8：删段 + 台账 + 审计；rebuild 不复活。"""

    def test_forget_session_deletes_and_records(self):
        self.setup_corpus()
        self.assertEqual(self.seg_count(self.SID), 2)
        res = fish_corpus.forget({"type": "session", "host": "dsh",
                                  "session_id": self.SID}, reason="测试删除",
                                 root=self.root)
        self.assertEqual(res["segments_deleted"], 2)
        self.assertEqual(self.seg_count(self.SID), 0)
        tombstones = store.list_tombstones(self.conn)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0]["scope_type"], "session")
        self.assertEqual(tombstones[0]["segments_deleted"], 2)
        self.assertEqual(tombstones[0]["reason"], "测试删除")
        actions = [row["action"] for row in store.list_access_log(self.conn)]
        self.assertIn("forget", actions)
        # 会话行还在（"这段对话存在过"是事实），但派生段数必须归零
        self.assertEqual(store.get_session(self.conn, "dsh", self.SID)["segments"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM corpus_fts WHERE seg_id NOT IN "
            "(SELECT id FROM corpus_segment)").fetchone()[0], 0)

    def test_rebuild_does_not_resurrect_forgotten_session(self):
        """C-P0-8 ① / M15：删过的会话 rebuild 之后仍是 0 段。"""
        self.setup_corpus()
        fish_corpus.forget({"type": "session", "session_id": self.SID}, root=self.root)
        res = self.rebuild(dry_run=False)
        self.assertEqual(res["sessions_skipped_by_tombstone"], 1)
        self.assertEqual(self.seg_count(self.SID), 0, "rebuild 让删掉的会话复活了")
        self.assertEqual(self.seg_count(self.SID2), 2, "未被删的会话必须照常重放")
        self.assertEqual(len(store.list_tombstones(self.conn)), 1, "台账不许被 rebuild 清掉")

    def test_rebuild_dry_run_reports_plan_without_writing(self):
        self.setup_corpus()
        before = self.seg_count()
        res = self.rebuild(dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["planned"], before)
        self.assertEqual(res["written"], 0)
        self.assertEqual(self.seg_count(), before)

    def test_rebuild_skips_host_scope_tombstone(self):
        self.setup_corpus()
        fish_corpus.forget({"type": "host", "host": "dsh"}, root=self.root)
        self.assertEqual(self.seg_count(), 0)
        self.rebuild(dry_run=False)
        self.assertEqual(self.seg_count(), 0)

    def test_forget_pointer_keeps_neighbours(self):
        self.setup_corpus()
        res = fish_corpus.forget({"type": "pointer",
                                  "pointer": "conv:dsh:3931d23f#seq=10"}, root=self.root)
        self.assertEqual(res["segments_deleted"], 1)
        self.assertEqual(self.seg_count(self.SID), 1)
        self.assertEqual(self.seg_count(self.SID2), 2)

    def test_forget_dry_run_changes_nothing(self):
        self.setup_corpus()
        before = self.seg_count()
        res = fish_corpus.forget({"type": "host", "host": "dsh"}, dry_run=True,
                                 root=self.root)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["planned"], before)
        self.assertEqual(res["segments_deleted"], 0)
        self.assertEqual(self.seg_count(), before)
        self.assertEqual(store.list_tombstones(self.conn), [])

    def test_forget_range_validates_utc(self):
        self.setup_corpus()
        with self.assertRaises(gov.GovError):
            fish_corpus.forget({"type": "range", "from": "2026-10-01 00:00:00",
                                "to": "2026-10-27T00:00:00Z"}, root=self.root)
        res = fish_corpus.forget({"type": "range", "from": "2026-10-01T00:00:00Z",
                                  "to": "2026-10-27T00:00:00Z"}, root=self.root)
        self.assertEqual(res["segments_deleted"], 4)

    def test_unknown_scope_is_loud(self):
        self.setup_corpus()
        for bad in ({"type": "everything"}, {"type": "session"}, "nonsense", 42):
            with self.assertRaises((gov.GovError, fish_corpus.CorpusError)):
                fish_corpus.forget(bad, root=self.root)

    def test_audit_never_contains_query_text(self):
        """C-P0-8 ② / M16：审计只有 query_hash + qlen，永不落原文。"""
        self.setup_corpus()
        probe = "撞车判据-审计探针-8f3a"
        fish_corpus.search(probe, root=self.root)
        fish_corpus.forget({"type": "session", "session_id": self.SID}, root=self.root)
        blob = json.dumps(self.rows("corpus_access_log"), ensure_ascii=False)
        self.assertNotIn(probe, blob)
        search_rows = [row for row in self.rows("corpus_access_log")
                       if row["action"] == "search"]
        self.assertTrue(search_rows)
        self.assertEqual(search_rows[0]["qlen"], len(probe))
        self.assertEqual(search_rows[0]["query_hash"],
                         hashlib.sha256(probe.encode("utf-8")).hexdigest())

    def test_prune_access_log_keeps_recent(self):
        self.setup_corpus()
        fish_corpus.search("撞车", root=self.root)
        self.conn.execute(
            "INSERT INTO corpus_access_log (citizen_id,session_key,action,query_hash,"
            "qlen,result_count,pointers,via,ts) VALUES (NULL,NULL,'search',?,3,0,NULL,"
            "'cli:corpus','2020-01-01T00:00:00Z')", ("x" * 64,))
        self.conn.commit()
        res = fish_corpus.prune_access_log(root=self.root, keep_days=30)
        self.assertEqual(res["deleted"], 1)
        rows = self.rows("corpus_access_log")
        self.assertTrue(rows)
        self.assertTrue(all(row["ts"] > "2020-01-01" for row in rows))


class DoctorInventoryTest(GovCase):
    """体检回执要能解释"库现在是什么状态"（规则数/台账/降级/孤儿都要在）。"""

    def test_doctor_inventory_fields(self):
        self.setup_corpus(dirty=True)
        res = fish_corpus.doctor(root=self.root)
        self.assertTrue(res["ok"], res["reasons"])
        self.assertEqual(res["segments"], 4)
        self.assertEqual(res["sessions"], 2)
        self.assertEqual(res["scanned"], 4)
        self.assertEqual(tuple(res["rules"]), tuple(redact_mod.RULE_NAMES))
        self.assertEqual(res["secret_hit_count"], 0)
        self.assertEqual(res["tombstones"], 0)
        self.assertEqual(res["unmapped_sessions"], 0)
        self.assertTrue(res["db_exists"])
        self.assertEqual([row["action"] for row in store.list_access_log(self.conn)][0],
                         "doctor")

    def test_doctor_declares_degraded_and_orphan(self):
        self.setup_corpus()
        self.conn.execute("UPDATE corpus_source SET status='degraded',"
                          " degraded_reason='测试降级' WHERE session_id=?", (self.SID,))
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertEqual(res["orphans"], 0)
        store.record_degraded(self.conn, "dsh", "x.zstd", self.SID, "bad_json", "坏帧")
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertEqual(len(res["degraded"]), 1)
        self.assertEqual(res["degraded"][0]["reason_code"], "bad_json")

    def test_cli_doctor_lines_mention_counts(self):
        self.setup_corpus()
        rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
        self.assertEqual(rc, 0, lines)
        self.assertIn("段 4", lines[0])
        self.assertTrue(any("脱敏规则" in line for line in lines))


class ForgetErasureTest(GovCase):
    """M1 + M5（2026-10-27 复审 🔴）：forget 之后 ① 日采不复活 ② 标题/cwd 清掉
    ③ 统计不计已删 ④ 明文在 db/wal 的字节里查不到。"""

    MARKER = "PLAINTEXT-MARKER-7f3a9c"

    def _insert_plaintext_segment(self):
        store.upsert_segment(self.conn, {
            "host": "dsh", "session_id": self.SID, "loc": "seq=77", "part": 0,
            "role": "assistant", "occurred_at": "2026-10-27T00:00:00Z",
            "text": "忘了也还在字节里：%s" % self.MARKER, "chars": 40,
            "redacted_count": 0, "raw_sha256": store.text_sha256(self.MARKER),
            "byte_from": 0, "byte_to": 1, "offset_space": "decompressed", "shadowed": 0})
        self.conn.commit()

    def _db_bytes(self) -> bytes:
        """db + wal 的原始字节（探针：明文是不是真的不在磁盘上）。"""
        blob = b""
        for name in ("corpus.sqlite", "corpus.sqlite-wal"):
            path = Path(self.root) / name
            if path.exists():
                blob += path.read_bytes()
        return blob

    def test_ingest_does_not_recollect_after_forget(self):
        """M1：`forget` 之后日常 ingest **不许**把它采回来（否则删除只在当天成立）。"""
        self.setup_corpus()
        fish_corpus.forget({"type": "session", "host": "dsh", "session_id": self.SID},
                           root=self.root)
        result = self.ingest()
        self.assertGreaterEqual(result["skipped_by_tombstone"], 1,
                                "日采没看台账：forget 掉的原话会被采回来")
        self.assertEqual(self.seg_count(self.SID), 0)

    def test_forget_clears_title_and_cwd(self):
        self.setup_corpus()
        self.conn.execute(
            "UPDATE corpus_session SET title=?, cwd=?, workspace=? WHERE session_id=?",
            ("撞车判据怎么做（含凭证线索）", "I:/app/gov-proj", "i:/app/gov-proj", self.SID))
        self.conn.commit()
        fish_corpus.forget({"type": "session", "host": "dsh", "session_id": self.SID},
                           root=self.root)
        row = store.get_session(self.conn, "dsh", self.SID)
        self.assertIsNone(row["title"], "forget 之后标题还留着（标题本身就是内容）")
        self.assertIsNone(row["cwd"])
        self.assertEqual(row["workspace"], "")
        self.assertEqual(row["project_source"], store.PROJECT_FORGOTTEN)
        self.assertEqual(row["segments"], 0)

    def test_stats_does_not_count_forgotten_sessions(self):
        self.setup_corpus()
        before = fish_corpus.stats(conn=self.conn)
        fish_corpus.forget({"type": "session", "host": "dsh", "session_id": self.SID},
                           root=self.root)
        after = fish_corpus.stats(conn=self.conn)
        self.assertEqual(after["sessions"], before["sessions"] - 1, after)
        self.assertEqual(after["segments"], before["segments"] - 2)
        self.assertEqual(after["forgotten_sessions"], 1)

    def test_forget_erases_plaintext_from_db_and_wal_bytes(self):
        """M5：`DELETE` 只把页标成空闲，明文还在——擦除必须在**字节层**可验证。"""
        self.setup_corpus()
        self._insert_plaintext_segment()
        self.assertIn(self.MARKER.encode("utf-8"), self._db_bytes(),
                      "探针无效：明文本来就不在库里（判据会假绿）")
        fish_corpus.forget({"type": "session", "host": "dsh", "session_id": self.SID},
                           root=self.root)
        self.conn.close()                    # 关掉句柄：剩下的字节就是"盘上留了什么"
        self.assertNotIn(self.MARKER.encode("utf-8"), self._db_bytes(),
                         "forget 只删了行，明文仍留在 db/wal 的字节里（需要 scrub 前的擦除）")


class EraseReceiptTest(GovCase):
    """安全 DEL-01（2026-10-27）：`busy` 不许当成功；擦除读数要能被核对。

    旧实现只看 `checkpoint` 有没有返回行，`busy=1`（有并发读者、WAL 没截断）也算擦过，
    回执 `error` 恒 None——"忘了"在字节层可能根本没发生，而回执说 OK。
    """

    def test_success_receipt_reports_vacuum_checkpoint_and_bytes(self):
        self.setup_corpus()
        info = store.secure_erase(self.conn)
        self.assertTrue(info["ok"], info)
        self.assertTrue(info["vacuumed"], "VACUUM 没跑：空闲页里的明文不会被挤掉")
        self.assertEqual(info["checkpoint_busy"], 0)
        self.assertEqual(info["wal_bytes"], 0, "WAL 没有截断到 0 字节")
        self.assertGreater(info["db_bytes"], 0)
        self.assertIsNone(info["error"])

    def test_busy_checkpoint_is_reported_as_failure(self):
        """另一个连接持着读快照 → checkpoint 只能是 busy=1 → 必须报失败。

        这里显式 `vacuum=False` 且用 `busy_timeout=250ms` 的短超时连接：VACUUM 在并发
        读者下会先撞 `busy_timeout`（那是另一条失败路径），而长 busy_timeout 会让
        SQLite 的 busy handler 等满 15 s —— 判据要快，生产口径不变。
        """
        self.setup_corpus()
        reader = store.open_db(self.root, readonly=True)
        eraser = store.connect(store.db_path(self.root), busy_timeout_ms=250)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM corpus_segment").fetchone()
            info = store.secure_erase(eraser, vacuum=False)
        finally:
            try:
                reader.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            reader.close()
            eraser.close()
        self.assertFalse(info["ok"], "有并发读者还报擦除成功（WAL 其实没截断）")
        self.assertTrue(info["error"], "失败必须带原因，否则人没法判断怎么补救")
        self.assertGreaterEqual(info["attempts"], 1)
        if info["checkpoint_busy"] is not None:
            self.assertEqual(info["checkpoint_busy"], 1)
            self.assertIn("busy", info["error"])

    def test_cli_forget_prints_erase_receipt(self):
        """CLI 必须把擦除结果打在回执上（旧版只在 JSON 里有，命令行一个字不印）。"""
        self.setup_corpus()
        rc, lines, _accept = fish_corpus.run_cli(
            "forget", root=self.root,
            target="session:%s" % self.SID)
        self.assertEqual(rc, 0, lines)
        erase_lines = [line for line in lines if line.startswith("擦除：")]
        self.assertTrue(erase_lines, lines)
        self.assertIn("VACUUM=已跑", erase_lines[0])
        self.assertIn("busy=0", erase_lines[0])
        self.assertIn("wal=0 B", erase_lines[0])


class PointerRangeScopeTest(GovCase):
    """复审 🟠-1：`pointer` / `range` 两种作用域必须**真的生效**（采集 + stats 同口径）。

    旧实现只在 `session`/`host` 上生效：pointer/range 删掉的原话会在"源被重写 → 游标归零
    → 重采"之后复活（台账只写不读）。这里逐条验"复活不了"。
    """

    SID2_POINTER = "log:dsh:%s#seq=1" % GovCase.SID2

    def _reset_cursors(self):
        """模拟"源被重写"：游标归零 → 下一次采集从头重读（幂等 upsert 会更新原段）。"""
        self.conn.execute("UPDATE corpus_source SET bytes_total=0, mtime_ns=0")
        self.conn.commit()

    def _texts(self):
        return [row["text"] for row in self.rows("corpus_segment")]

    def test_pointer_forget_does_not_resurrect_after_rewrite(self):
        self.setup_corpus()
        res = fish_corpus.forget({"type": "pointer", "pointer": self.SID2_POINTER},
                                 root=self.root)
        self.assertEqual(res["segments_deleted"], 1, res)
        self.assertNotIn("撞车判据里的凭证", " ".join(self._texts()))
        self._reset_cursors()
        again = self.ingest()
        self.assertGreaterEqual(again["purged_by_segment_tombstone"], 1,
                                "重采之后台账没有回收：被 forget 的原文复活了")
        self.assertNotIn("撞车判据里的凭证", " ".join(self._texts()),
                         "pointer 作用域在采集侧不生效（原文复活）")
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["segments"], 3, stats)   # SID 2 段 + SID2 剩 1 段
        self.assertEqual(stats["tombstones"]["pointer"], 1)

    def test_pointer_forget_keeps_other_segments_and_session_tags(self):
        """单段删除**不该**把整会话的标题/cwd 一起抹掉（那是 session 作用域的事）。"""
        self.setup_corpus()
        fish_corpus.forget({"type": "pointer", "pointer": self.SID2_POINTER},
                           root=self.root)
        row = store.get_session(self.conn, "dsh", self.SID2)
        self.assertIsNotNone(row["cwd"], "pointer 删除把整个会话的 cwd 抹了（越权）")
        self.assertNotEqual(row["project_source"], store.PROJECT_FORGOTTEN)

    def test_range_forget_does_not_resurrect_after_rewrite(self):
        self.setup_corpus()
        stamp = self.conn.execute(
            "SELECT occurred_at FROM corpus_segment WHERE session_id=? AND loc='seq=1'",
            (self.SID2,)).fetchone()[0]
        res = fish_corpus.forget({"type": "range", "host": "dsh",
                                  "from": stamp, "to": stamp}, root=self.root)
        self.assertGreaterEqual(res["segments_deleted"], 1, res)
        self._reset_cursors()
        again = self.ingest()
        self.assertGreaterEqual(again["purged_by_segment_tombstone"], 1)
        self.assertNotIn("撞车判据里的凭证", " ".join(self._texts()),
                         "range 作用域在采集侧不生效（原文复活）")
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["segments"], 2, stats)   # 只剩 SID 的 2 段
        self.assertEqual(stats["tombstones"]["range"], 1)

    def test_rebuild_also_reaps_pointer_tombstones(self):
        self.setup_corpus()
        fish_corpus.forget({"type": "pointer", "pointer": self.SID2_POINTER},
                           root=self.root)
        # 必须显式给 roots/hosts：缺省根是**真实** DSH 日志目录，重建会去扫全盘
        # （这正是"全量跑不完"的那条路；判据不许踩它）。
        res = fish_corpus.rebuild(root=self.root, apply_=True, hosts=("dsh",),
                                 roots={"dsh": [self.sessions]},
                                 project_of=lambda cwd: PROJECT,
                                 citizen_of=lambda key: CID)
        self.assertGreaterEqual(res["reaped_by_pointer_tombstone"], 0)
        self.assertNotIn("撞车判据里的凭证", " ".join(self._texts()),
                         "rebuild 之后 pointer 删除失效（原文复活）")


class CwdRedactionTest(GovCase):
    """安全新🟠：`cwd`/`workspace`/`project_note` 写入前脱敏，`stats` 回显不含真值。

    现场：cwd 里贴过 `token=…`，落库原样，然后 `stats.unmapped_cwds` 把它回了 AI。
    """

    SECRET = "fsh_" + "C" * 24

    def _ingest_with_dirty_cwd(self):
        sid = self.SID2
        cwd = "I:/app/token=" + self.SECRET
        self.write_session(sid, [ev_session(sid, cwd=cwd), ev_user(1, "正文")])
        return fish_corpus.ingest(
            ("dsh",), root=self.root, conn=self.conn,
            roots={"dsh": [self.sessions]}, use_lock=False,
            project_of=lambda _cwd: None,          # 未映射：才会进 unmapped_cwds
            candidates_of=lambda _cwd: [
                {"project": "候选项目", "path_prefix": "I:/app/token=" + self.SECRET}],
        ), sid

    def test_cwd_workspace_and_note_are_redacted_before_storage(self):
        _result, sid = self._ingest_with_dirty_cwd()
        row = store.get_session(self.conn, "dsh", sid)
        for column in ("cwd", "workspace", "project_note"):
            value = row[column] or ""
            self.assertNotIn(self.SECRET, value, "%s 落库了明文凭证" % column)
            self.assertNotIn(self.SECRET[:12], value, "%s 仍能拼出凭证片段" % column)
        self.assertIn("已脱敏", row["cwd"])

    def test_stats_echo_has_no_secret(self):
        self._ingest_with_dirty_cwd()
        payload = fish_corpus.stats(conn=self.conn)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.SECRET, blob, "stats 把 cwd 里的凭证回显给了 AI")
        self.assertTrue(payload["unmapped_cwds"], "这条用例要求它出现在 unmapped_cwds 里")
        self.assertIn("已脱敏", payload["unmapped_cwds"][0]["workspace"])

    def test_doctor_stays_green_after_write_side_redaction(self):
        self._ingest_with_dirty_cwd()
        res = fish_corpus.doctor(root=self.root)
        self.assertTrue(res["ok"], res["reasons"])
        self.assertEqual(res["secret_hit_count"], 0)


class DoctorStallTest(GovCase):
    """复审 🟡：报告说"doctor 打印 stall"，代码里一处都没读——现在必须真的读。"""

    def test_doctor_reports_stalled_and_cli_prints_it(self):
        self.setup_corpus()
        store.set_meta(self.conn, store.META_STALL_STREAK, 3)
        store.set_meta(self.conn, store.META_STALL_ALARM, "stalled")
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertFalse(res["ok"])
        self.assertIn("stalled", res["reasons"])
        self.assertEqual(res["alarm"], "stalled")
        self.assertEqual(res["stall"]["zero_streak"], 3)
        _rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
        self.assertTrue(any("停滞" in line for line in lines), lines)


class DoctorTextColumnTest(GovCase):
    """S2：体检扫描面覆盖**可回显文本列**（title/cwd/project_note/degraded.reason）。

    变异体：把 `DOCTOR_TEXT_COLUMNS` 摘掉（只扫正文）→ 本文件红。
    """

    def test_doctor_is_red_when_title_carries_a_secret(self):
        self.setup_corpus()
        secret = "fsh_" + "Q" * 24
        self.conn.execute("UPDATE corpus_session SET title=? WHERE session_id=?",
                          ("凭证 %s 的会话" % secret, self.SID))
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertFalse(res["ok"], "标题里带 token 体检还绿（可回显列没扫）")
        self.assertIn("plaintext_secrets", res["reasons"])
        self.assertIn("corpus_session.title", {hit["column"] for hit in res["secret_hits"]})
        self.assertGreaterEqual(res["scanned_extra"], 1)

    def test_doctor_is_red_when_degraded_reason_carries_a_secret(self):
        self.setup_corpus()
        store.record_degraded(self.conn, "dsh", "x.zstd", self.SID, "bad_json",
                              "坏帧里带 fsh_" + "R" * 24, 0)
        self.conn.commit()
        res = fish_corpus.doctor(root=self.root)
        self.assertFalse(res["ok"])
        self.assertIn("corpus_degraded.reason",
                      {hit["column"] for hit in res["secret_hits"]})

    def test_clean_corpus_stays_ok(self):
        self.setup_corpus(dirty=True)
        res = fish_corpus.doctor(root=self.root)
        self.assertTrue(res["ok"], res["reasons"])


class SecretFormsTest(GovCase):
    """S1（2026-10-27 复审 🔴）：六种凭证形态**逐条**验证"键+值整体消费"。

    旧规则只掩标签留值（`MY_TOKEN=`/`GITHUB_TOKEN=`）、`sk-proj-`/`github_pat_`/
    JSON `"password":` 整条漏。每条都验三处：落库后的正文、`read` 出口、`search` 摘录。
    """

    # (说明, 原文, 绝不能出现在库/出口里的值)
    FORMS = (
        ("generic_token", "部署时 export MY_TOKEN=abcdef1234567890 就完事了",
         "abcdef1234567890"),
        ("upper_token", "GITHUB_TOKEN=gh9Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Ji0Hg 已写入",
         "gh9Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Ji0Hg"),
        ("openai_project_key", "用的是 sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34 这个",
         "sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34"),
        ("github_pat", "细粒度令牌 github_pat_11ABCDEFG0123456789_abcdefghijklmnop 拉库",
         "github_pat_11ABCDEFG0123456789_abcdefghijklmnop"),
        ("json_password", '{"user": "root", "password": "hunter2hunter2"} 是样例',
         "hunter2hunter2"),
        ("bearer", "请求头 Authorization: Bearer abcdefgh12345678ijkl 别外传",
         "abcdefgh12345678ijkl"),
    )

    def seed(self):
        sid = self.SID2
        events = [ev_session(sid)]
        for idx, (_label, text, _secret) in enumerate(self.FORMS):
            events.append(ev_user(10 + idx, text))
        self.write_session(sid, events)
        self.ingest()
        return sid

    def test_each_form_is_gone_from_store_read_and_search(self):
        sid = self.seed()
        stored = " ".join(row["text"] for row in self.rows("corpus_segment"))
        for idx, (label, _text, secret) in enumerate(self.FORMS):
            self.assertNotIn(secret, stored, "%s：明文落库了" % label)
            read = fish_corpus.read("log:dsh:%s#seq=%d" % (sid, 10 + idx), root=self.root)
            self.assertNotIn(secret, read["text"], "%s：read 出口漏了" % label)
            self.assertGreaterEqual(read["redacted_count"], 1,
                                    "%s：read 回执说这一句没命中（脱敏没生效）" % label)
        hits = fish_corpus.search("样例", root=self.root)["hits"]
        self.assertTrue(hits)
        for hit in hits:
            for label, _text, secret in self.FORMS:
                self.assertNotIn(secret, hit["snippet"], "%s：search 摘录漏了" % label)
        self.assertEqual(redact_mod.scan_count(stored), 0, "库里仍能扫到可识别形态")

    def test_placeholders_are_never_hit_twice(self):
        self.seed()
        stored = " ".join(row["text"] for row in self.rows("corpus_segment"))
        again, count, _hits = redact_mod.redact(stored)
        self.assertEqual(count, 0, "占位符被规则再次命中（出口二次会重复计数）")
        self.assertEqual(again, stored)

    def test_doctor_goes_red_when_redaction_is_disabled(self):
        """六种形态的"doctor 造红"：关掉脱敏落库 → 体检必须列出明文。"""
        old = os.environ.get(redact_mod.ENV_DISABLE)
        sid = self.SID2
        try:
            os.environ[redact_mod.ENV_DISABLE] = "1"
            events = [ev_session(sid)] + [
                ev_user(10 + idx, text) for idx, (_l, text, _s) in enumerate(self.FORMS)]
            self.write_session(sid, events)
            self.ingest()
            res = fish_corpus.doctor(root=self.root)
        finally:
            if old is None:
                os.environ.pop(redact_mod.ENV_DISABLE, None)
            else:
                os.environ[redact_mod.ENV_DISABLE] = old
        self.assertFalse(res["ok"])
        self.assertIn("plaintext_secrets", res["reasons"])
        hit_types = {t for hit in res["secret_hits"] for t in hit["types"]}
        self.assertGreaterEqual(len(hit_types), 4,
                                "体检只认出 %s 族（规则漏了族前缀）" % sorted(hit_types))


class StatsAlarmTest(GovCase):
    """`stats` 的两个新读数（M1/M4）：`forgotten_sessions` 与 `stall` 字段必须在。"""

    def test_stats_shape_has_stall_and_forgotten(self):
        self.setup_corpus()
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["forgotten_sessions"], 0)
        self.assertIn("stall", stats)
        self.assertIsNone(stats["alarm"])


class CrossHostSessionForgetTest(GovCase):
    """评审 🟠-1（终批）：CLI 唯一形态 `forget session:<sid>`（不带 host）必须**跨宿主**生效。

    现场（实测）：那条命令落台账键 `*:<sid>`，而采集侧只比 `(host, sid)` → 源被重写
    （游标归零）之后 `purged=0 / segments_new=3`、被删原话复活、`stats.sessions` 多算。
    三处（采集跳过 / stats / rebuild）现在共用 `store.is_session_forgotten`。
    """

    def test_cross_host_forget_blocks_ingest_and_stats(self):
        self.setup_corpus()
        res = fish_corpus.forget("session:%s" % self.SID2, root=self.root)
        self.assertEqual(res["tombstone_key"], "*:" + self.SID2,
                         "不带 host 的 session 作用域必须落 `*:<sid>`")
        self.assertEqual(store.count_segments(self.conn, "dsh", self.SID2), 0)
        after_forget = fish_corpus.stats(conn=self.conn)
        # 源被重写 → 游标归零（"被删原话复活"的现场就是这么来的）
        self.conn.execute("UPDATE corpus_source SET bytes_total=0, mtime_ns=0")
        self.conn.commit()
        again = self.ingest()
        self.assertEqual(again["skipped_by_tombstone"], 1,
                         "`*:<sid>` 台账没被采集侧认到（只比了 (host,sid)）")
        self.assertEqual(again["segments_new"], 0, "被 forget 的原话复活了")
        self.assertEqual(store.count_segments(self.conn, "dsh", self.SID2), 0)
        texts = " ".join(row["text"] for row in self.rows("corpus_segment"))
        self.assertNotIn("撞车判据里的凭证", texts, "重采之后被删的原话又进了库")
        stats_after = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats_after["sessions"], after_forget["sessions"],
                         "被 forget 的会话又被算进 sessions（跨宿主台账在统计侧没生效）")
        self.assertEqual(stats_after["by_host"]["dsh"]["sessions"], 1)
        self.assertEqual(stats_after["forgotten_sessions"], 1)

    def test_rebuild_respects_cross_host_forget(self):
        self.setup_corpus()
        fish_corpus.forget("session:%s" % self.SID2, root=self.root)
        res = self.rebuild(apply_=True)
        self.assertEqual(res["sessions_skipped_by_tombstone"], 1,
                         "rebuild 没认出 `*:<sid>`（重放会把被删的原话带回来）")
        self.assertEqual(store.count_segments(self.conn, "dsh", self.SID2), 0)
        self.assertEqual(store.count_segments(self.conn, "dsh", self.SID), 2,
                         "另一个会话被连坐（范围判错）")


class ExitRedactionTest(GovCase):
    """安全 🟠（终批）：出口两列必须过 `_safe_field`，且**不许把判据一起打码**。

    两个出口、两类脏数据（旧库直接带真值，写侧那道覆盖不到）：
      · `read` / `search` 的 `session_title`；
      · `read.source.path` 与 `stats.degraded/orphan[].path`（会话目录名本身带 token）。
    同时判"打码不影响功能"：`source.missing` 必须仍按**原始路径**判——源在 → False，
    源没了 → True；写侧也**不许**打码（排障与 missing 判定都要真路径）。
    """

    SECRET = "fsh_" + "S" * 24

    def _setup_with_dirty_path(self):
        ws = "--token=" + self.SECRET + "--"
        self.write_session(self.SID, [ev_session(self.SID), ev_user(9, "撞车判据怎么做"),
                                      ev_assistant(10, LONG)], ws=ws)
        self.write_session(self.SID2, [ev_session(self.SID2), ev_user(1, "另一句")])
        self.ingest()
        return self.SID

    def _dirty_title(self, sid):
        self.conn.execute("UPDATE corpus_session SET title=? WHERE session_id=?",
                          ("凭证 %s 的会话" % self.SECRET, sid))
        self.conn.commit()

    def test_read_masks_title_and_source_path_without_breaking_missing(self):
        sid = self._setup_with_dirty_path()
        self._dirty_title(sid)
        res = fish_corpus.read("log:dsh:%s#seq=9" % sid, root=self.root)
        blob = json.dumps(res, ensure_ascii=False)
        self.assertNotIn(self.SECRET, blob, "read 出口回显了标题/源路径里的凭证")
        self.assertIn("已脱敏", res["session_title"], "标题没走 _safe_field")
        self.assertIn("已脱敏", res["source"]["path"], "source.path 没走 _safe_field")
        self.assertFalse(res["source"]["missing"],
                         "源还在却被判 missing：判据吃到了脱敏后的路径")
        # 写侧不打码（`_source_state` 与排障都要真路径）
        self.assertIn(self.SECRET, store.read_cursor(self.conn, "dsh", sid)["path"],
                      "写侧把路径打码了：missing 判定会从此恒真")

    def test_search_hit_title_is_masked(self):
        sid = self._setup_with_dirty_path()
        self._dirty_title(sid)
        hits = fish_corpus.search("撞车判据", root=self.root)["hits"]
        self.assertTrue(hits, "夹具无效：没有命中，这条判据会假绿")
        self.assertNotIn(self.SECRET, json.dumps(hits, ensure_ascii=False),
                         "search 的 session_title 出口漏了")
        self.assertIn("已脱敏", hits[0]["session_title"])

    def test_stats_masks_degraded_and_orphan_paths(self):
        sid = self._setup_with_dirty_path()
        store.record_degraded(self.conn, "dsh", "/x/token=" + self.SECRET + "/a.zstd",
                              self.SID2, "bad_json", "坏帧", 0)
        self.conn.commit()
        for path in self.sessions.rglob("session.v4.jsonl.zstd"):
            if sid in str(path):
                path.unlink()                      # 源没了 → 下一轮标 orphan
        self.ingest()
        payload = fish_corpus.stats(conn=self.conn)
        self.assertNotIn(self.SECRET, json.dumps(payload, ensure_ascii=False),
                         "stats 把降级/孤儿路径的真值回显给了 AI")
        self.assertTrue(payload["degraded"], "夹具无效：没有降级行")
        self.assertTrue(payload["orphan"], "夹具无效：没有孤儿行")
        self.assertIn("已脱敏", payload["degraded"][0]["path"])
        self.assertIn("已脱敏", payload["orphan"][0]["path"])
        # 源确实没了 → missing 仍要按原始路径判成 True
        res = fish_corpus.read("log:dsh:%s#seq=9" % sid, root=self.root)
        self.assertTrue(res["source"]["missing"], "源删了还说在：missing 判定坏了")
        self.assertNotIn(self.SECRET, res["source"]["path"])


class SourceSegmentsSyncTest(GovCase):
    """评审 🟡-1（终批）：`pointer`/`range` forget 之后两个段数读数必须**当场**一致。"""

    POINTER = "log:dsh:%s#seq=1" % GovCase.SID2

    def test_pointer_forget_syncs_source_segments_kept(self):
        self.setup_corpus()
        before = fish_corpus.stats(conn=self.conn)
        self.assertEqual(before["segments"], 4)
        self.assertEqual(before["by_host"]["dsh"]["segments_kept"], 4)
        fish_corpus.forget({"type": "pointer", "pointer": self.POINTER}, root=self.root)
        after = fish_corpus.stats(conn=self.conn)
        self.assertEqual(after["segments"], 3)
        self.assertEqual(after["by_host"]["dsh"]["segments_kept"], after["segments"],
                         "游标里的 segments_kept 没跟着改：要等下一次 ingest 才收敛")


class RetagMappingTest(GovCase):
    """安全 🟡（终批）：`retag` 拿**已脱敏**的 cwd 重算 → mapped 被悄悄降成 unmapped。

    现场（实测）：`retag` 之后 `changed=1 / mapped=0`——映射覆盖率越跑越差，却一句警告
    都没有。修法：重算不出映射时保留写时的归属并**响亮**告警；真要降级得显式要求。
    """

    def test_retag_keeps_mapping_and_warns_instead_of_downgrading(self):
        self.setup_corpus()
        for sid in (self.SID, self.SID2):
            row = store.get_session(self.conn, "dsh", sid)
            self.assertEqual((row["project"], row["project_source"]), (PROJECT, "mapped"))
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: None)
        self.assertEqual(counters["unmapped"], 0, "已映射的会话被重算成未映射了")
        self.assertEqual(counters["mapped"], 2)
        self.assertEqual(counters["kept_mapped"], 2)
        self.assertTrue(counters["warnings"], "降级被拦下了却没人被通知（必须响亮）")
        self.assertIn("保留写时的项目", counters["warnings"][0])
        for sid in (self.SID, self.SID2):
            row = store.get_session(self.conn, "dsh", sid)
            self.assertEqual((row["project"], row["project_source"]), (PROJECT, "mapped"))

    def test_cli_prints_the_kept_mapping_warning(self):
        self.setup_corpus()
        args = argparse.Namespace(root=str(self.root), action="retag", host="all",
                                  session=None, all=True, dry_run=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fish_corpus.run_command(args, {"project_of": lambda _cwd: None})
        lines = buf.getvalue().splitlines()
        self.assertEqual(rc, 0, lines)
        self.assertTrue(any("保留原映射 2" in line for line in lines), lines)
        self.assertTrue(any(line.strip().startswith("⚠") for line in lines),
                        "命令行一个字都没提降级被拦下：这条判据要的是「响亮」")

    def test_explicit_downgrade_is_still_possible(self):
        self.setup_corpus()
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: None,
                                      allow_downgrade=True)
        self.assertEqual(counters["kept_mapped"], 0, "显式降级还被拦着（能力被堵死）")
        self.assertEqual(counters["unmapped"], 2)
        row = store.get_session(self.conn, "dsh", self.SID)
        self.assertEqual(row["project_source"], "unmapped")


class RetagIdentityTest(GovCase):
    """安全官（终验黄项）：`retag` 抹身份列。

    现场（实测）：`public:selftest/session_tokens` 只跑一次 `retag` 就变成
    `citizen_id=None / identity_source=None`——`Tagger` 构造时没传 `citizen_of`，
    `_identity_of` 返回 `(None, None)`，而旧的 UPDATE 把它当成"新值"写回去；顺带
    `changed` 首次虚高（每行都"变了"）。

    五条判据（都是"坏输入必须红"）：
      ① 注入了同一个解析器 → 身份两列**一列都不许变**、且 `changed=0`；
      ② 没注入解析器（直跑 API）→ **保留原列**，计入 `kept_identity`（不许清空）；
      ③ 解析器在场但查不到会话键（令牌记录被清）→ 同样保留，不许"清空历史归属"；
      ④ CLI（`run_command`）必须把注入的 `citizen_of` 传到 `retag`——用"库里本来没身份"
         的现场判：补齐是能力，漏传就是永久补不上；
      ⑤ 映射真变了才 `changed`（身份仍不动）。
    """

    def _identity(self, sid):
        row = store.get_session(self.conn, "dsh", sid)
        return row["citizen_id"], row["identity_source"]

    def test_retag_with_same_resolvers_changes_nothing(self):
        self.setup_corpus()
        for sid in (self.SID, self.SID2):
            self.assertEqual(self._identity(sid), (CID, "session_tokens"),
                             "夹具无效：身份没在采集时落库")
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: PROJECT,
                                     citizen_of=lambda _key: CID)
        self.assertEqual(counters["changed"], 0,
                         "项目与身份都没变，retag 却说变了（changed 虚高）")
        self.assertEqual(counters["kept_identity"], 0, "解析器在，却走了「保留原身份」分支")
        for sid in (self.SID, self.SID2):
            self.assertEqual(self._identity(sid), (CID, "session_tokens"),
                             "retag 把身份列改了（安全官实测的 None/None 现场）")

    def test_retag_without_citizen_resolver_keeps_stored_identity(self):
        self.setup_corpus()
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: PROJECT)
        self.assertEqual(counters["kept_identity"], 2,
                         "没注入解析器时既没保留也没报告（静默抹身份的入口）")
        for sid in (self.SID, self.SID2):
            self.assertEqual(self._identity(sid), (CID, "session_tokens"),
                             "没注入 citizen_of 时身份被写成了 NULL")

    def test_resolver_misses_keep_stored_identity(self):
        """解析器在场但查不到这个会话键（令牌记录被清）→ 不许把历史归属清空。"""
        self.setup_corpus()
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: PROJECT,
                                     citizen_of=lambda _key: None)
        self.assertEqual(counters["kept_identity"], 2,
                         "查不到就把身份清空了（一次令牌清理能抹掉全部历史归属）")
        self.assertEqual(counters["changed"], 0, "身份没变，changed 却动了")
        for sid in (self.SID, self.SID2):
            self.assertEqual(self._identity(sid), (CID, "session_tokens"))

    def test_cli_retag_injects_citizen_resolver(self):
        # 库里本来没身份（采集时没注入解析器）→ CLI 的 retag 必须能补齐
        self.write_session(self.SID, [ev_session(self.SID), ev_user(9, "身份补齐")])
        fish_corpus.ingest(("dsh",), root=self.root, conn=self.conn,
                           roots={"dsh": [self.sessions]}, use_lock=False,
                           project_of=lambda _cwd: PROJECT)
        self.assertEqual(self._identity(self.SID), (None, None),
                         "夹具无效：身份应为空才测得出「CLI 有没有传解析器」")
        args = argparse.Namespace(root=str(self.root), action="retag", host="all",
                                  session=None, all=True, dry_run=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fish_corpus.run_command(args, {"project_of": lambda _cwd: PROJECT,
                                                "citizen_of": lambda _key: CID})
        lines = buf.getvalue().splitlines()
        self.assertEqual(rc, 0, lines)
        self.assertIn("保留原身份 0", " ".join(lines), lines)
        self.assertEqual(self._identity(self.SID), (CID, "session_tokens"),
                         "CLI 没把 citizen_of 传给 retag：身份永远补不上")

    def test_changed_counts_only_real_mapping_changes(self):
        self.setup_corpus()
        counters = fish_corpus.retag(root=self.root, project_of=lambda _cwd: "OTHER",
                                     citizen_of=lambda _key: CID)
        self.assertEqual(counters["changed"], 2, "映射真的变了，changed 却没数进去")
        for sid in (self.SID, self.SID2):
            row = store.get_session(self.conn, "dsh", sid)
            self.assertEqual(row["project"], "OTHER")
            self.assertEqual((row["citizen_id"], row["identity_source"]),
                             (CID, "session_tokens"), "改项目顺手把身份改了")


class CliIngestRedactionTest(GovCase):
    """安全官（终验黄项）：`corpus ingest` 的**降级行**把源路径原样打给终端。

    `7ec50fc` 修了渲染（`_safe_field(item['path'])`）但没有判据——这条补上，并且故意
    把凭证放进**会话目录名**：真实 DSH 里目录名是 cwd 的转义编码，cwd 里的
    `token=<值>` 会原样进路径，降级行就是那条"回显通道"。
    """

    SECRET_TOKEN = "fsh_" + "D" * 24

    def _write_degraded_source(self):
        # `.v3` → 事件模型未取证 → 登记降级（不读内容，路径仍会进降级行）
        folder = self.sessions / ("--token=" + self.SECRET_TOKEN + "--") / self.SID
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v3.jsonl.zstd"
        path.write_bytes(b"not-a-v4-frame")
        return path

    def _run_cli_ingest(self):
        args = argparse.Namespace(root=str(self.root), action="ingest", host="dsh",
                                  limit=None, dry_run=False)
        buf = io.StringIO()
        env = {"FISH_CORPUS_DSH_SESSIONS": str(self.sessions)}
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(buf):
                rc = fish_corpus.run_command(args)
        return rc, buf.getvalue()

    def test_degraded_line_masks_the_source_path(self):
        self._write_degraded_source()
        rc, out = self._run_cli_ingest()
        self.assertEqual(rc, 1, out)                       # 有降级 → 退出码 1
        self.assertIn("降级", out, "夹具无效：没有打出降级行")
        self.assertNotIn(self.SECRET_TOKEN, out,
                         "CLI 把降级路径里的凭证原样打给了终端")
        self.assertIn("已脱敏", out, "降级行没走 _safe_field")

    def test_ok_run_does_not_print_paths_at_all(self):
        # 反面控制：只有降级行回显 path；正常采集的行里不该出现任何源路径
        self.write_session(self.SID, [ev_session(self.SID), ev_user(1, "正常一句")])
        rc, out = self._run_cli_ingest()
        self.assertEqual(rc, 0, out)
        self.assertNotIn(str(self.sessions), out, "正常回执把源路径也打了出来")


class EraseLatencyTest(GovCase):
    """安全 🟡（终批）：擦除的等待上限。

    现场（实测）：并发读者下 `wal_checkpoint(TRUNCATE)` 撞 busy 等满 15 s × 2 轮 ≈ 34 s
    ——"删掉一条私密对话"变成挂住半分钟的猜测。修法：擦除期间 `busy_timeout` **只降不升**
    压到 `ERASE_BUSY_TIMEOUT_MS`（≤2 s），失败照旧如实报，结束后复原调用方口径。
    """

    def test_erase_caps_busy_timeout_and_stays_fast(self):
        self.setup_corpus()
        reader = store.open_db(self.root, readonly=True)
        eraser = store.connect(store.db_path(self.root))        # 默认 15 s 口径
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM corpus_segment").fetchone()
            started = time.time()
            info = store.secure_erase(eraser, vacuum=False)
            elapsed = time.time() - started
            restored = int(eraser.execute("PRAGMA busy_timeout").fetchone()[0])
        finally:
            try:
                reader.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            reader.close()
            eraser.close()
        self.assertFalse(info["ok"], "有并发读者还报擦除成功（WAL 其实没截断）")
        self.assertLessEqual(info["busy_timeout_ms"], store.ERASE_BUSY_TIMEOUT_MS,
                             "擦除没把 busy_timeout 压下来")
        self.assertLessEqual(elapsed, 5.0, "擦除等了 %.1fs（busy_timeout 没压到 ≤2 s）" % elapsed)
        self.assertEqual(restored, store.BUSY_TIMEOUT_MS, "擦除后没复原调用方的 busy_timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
