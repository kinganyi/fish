#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-P0-2 的 WB 侧判据 + 流式内存红线（T02 · 设计 §10）。

WB 与 DSH 的失败模式不同：DSH 怕"帧坏了"，WB 怕"行坏了/行太大了"——
实测 4.1 GB、单会话 674 MB，一行超长就会把内存打爆（所以 `_read_line` 有上限）。
本文件把这些"坏输入"逐个造出来，确认：

| 判据 | 用例 | 红的样子（变异体） |
|---|---|---|
| C-P0-2 正常 | `BaselineTest`：只有 `message` 行产段（`session-meta`/`ai-title` 不产段） | 把 `session-meta` 当消息 → 段数多出来 |
| C-P0-2 红① | `BadLineTest`：一行坏 JSON → 跳行 + 计数，文件仍 `ok` | 整文件失败 / 静默丢 |
| C-P0-2 红② | `StreamingTest`：超长行有上限、内存峰值远低于 512 MB | 整行读进内存 |
| M19 | `SystemReminderTest`：`<system-reminder>` 块必须剥除 | 注入上下文当用户原话入库 |
| 噪音 | `NoiseRowsTest`：`reasoning`/`function_call*` 不入库但计数 | 照采 → 体量与隐私双爆 |
| C-P0-4 | `IdempotencyTest`：重跑不变、追加 +1 | 段数翻倍 |

内存口径说明（与设计的偏离，见交付报告）：设计写的是 `resource.getrusage`，但
`resource` 是 **Unix-only**，Windows 上没有。这里用 `tracemalloc`（stdlib、跨平台）
量 Python 堆峰值——它**不看 C 层缓冲**，作为"有没有整行读进内存"的判据更严格，
方向是对的（真爆内存时 Python 堆里一定先看到那一大块 bytes）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_corpus  # noqa: E402
import fish_corpus_store as store  # noqa: E402

MS = 1792162093239
MEM_RED_LINE = 512 * 1024 * 1024          # PRD C-P0-2 红②
MEM_TARGET = 64 * 1024 * 1024             # 设计 §4.2 的目标


def row_message(role, text, *, ts=None, content=None, mid="m1"):
    return {"id": mid, "timestamp": ts or MS, "type": "message", "role": role,
            "content": content if content is not None else
            [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            "sessionId": "wb-session", "cwd": "I:/app/anyi-studio"}


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-wb-"))
        self.root = self.tmp / "data"
        self.projects = self.tmp / "projects"
        self.projects.mkdir(parents=True)
        self.conn = store.open_db(self.root)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def write_session(self, sid="wb-session", rows=(), *, ws="i-app-anyi-studio-AI工作空间",
                      extra_lines=(), raw_prefix=b"", name=None):
        folder = self.projects / ws
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or ("%s.jsonl" % sid))
        with open(path, "wb") as fh:
            fh.write(raw_prefix)
            for row in rows:
                payload = row if isinstance(row, bytes) else \
                    json.dumps(row, ensure_ascii=False).encode("utf-8")
                fh.write(payload + b"\n")
            for line in extra_lines:
                fh.write(line + b"\n")
        return path

    def append_rows(self, path, rows):
        with open(path, "ab") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")

    def ingest(self, *, project_of=None, limit=None, dry_run=False, max_line_bytes=None,
               days=None):
        if max_line_bytes is None:
            return fish_corpus.ingest(
                ("workbuddy",), root=self.root, conn=self.conn,
                roots={"workbuddy": [self.projects]}, limit=limit, dry_run=dry_run,
                use_lock=False, project_of=project_of, days=days)
        # 行上限是 WB 适配器的参数（门面只透传采集级参数）：直接调适配器。
        adapter = fish_corpus.adapter_for("workbuddy")
        cursor = store.read_cursor(self.conn, "workbuddy", "wb-session")
        return adapter.ingest_file(
            self.conn, self.paths[0], cursor, max_line_bytes=max_line_bytes,
            tagger=fish_corpus.tag_mod.Tagger(project_of=project_of))

    def segments(self):
        return self.conn.execute(
            "SELECT * FROM corpus_segment WHERE host='workbuddy' ORDER BY id").fetchall()

    def session_row(self, sid="wb-session"):
        return store.get_session(self.conn, "workbuddy", sid)


class BaselineTest(CorpusCase):
    """C-P0-2 绿：只有 `message` 行产段；meta/title 行不产段但补会话头。"""

    def test_only_message_rows_become_segments(self):
        self.paths = [self.write_session(rows=[
            {"type": "session-meta", "id": "x", "sessionId": "wb-session",
             "timestamp": MS, "meta": {"codebuddy.ai/hostKind": "unopted"}},
            row_message("user", "用户说的第一句"),
            row_message("assistant", "助手回答", mid="m2"),
            {"type": "ai-title", "aiTitle": "会话标题", "sessionId": "wb-session",
             "cwd": "I:/app/anyi-studio", "timestamp": MS},
        ])]
        result = self.ingest(project_of=lambda cwd: "anyi-studio")
        self.assertEqual(result["sessions_seen"], 1)
        rows = self.segments()
        self.assertEqual([(r["role"], r["loc"]) for r in rows],
                         [("user", "line=2"), ("assistant", "line=3")])
        session = self.session_row()
        self.assertEqual(session["title"], "会话标题")
        self.assertEqual((session["project"], session["project_source"]),
                         ("anyi-studio", "mapped"))
        self.assertEqual(rows[0]["offset_space"], "file")
        self.assertTrue(rows[0]["occurred_at"].endswith("Z"))

    def test_non_jsonl_files_are_ignored(self):
        """实测目录里混着渲染产物 txt：按扩展名白名单排除（PRD N3 的实现取舍）。"""
        self.paths = [self.write_session()]
        (self.projects / "i-app-anyi-studio-AI工作空间" / "render.txt").write_text(
            "这不是对话", encoding="utf-8")
        discovered = fish_corpus.wb_adapter.discover([self.projects])
        self.assertEqual([Path(item["path"]).name for item in discovered], ["wb-session.jsonl"])


class SystemReminderTest(CorpusCase):
    """M19：注入块必须剥除，否则"用户说的话"里混进我们自己的上下文。"""

    def test_system_reminder_block_is_stripped(self):
        content = [{"type": "input_text", "text":
                    "<system-reminder data-role=\"user-context\">\n"
                    "<user_info>Workspace Folder: I:/app/x</user_info>\n"
                    "</system-reminder>\n真正的用户提问是什么"}]
        self.paths = [self.write_session(rows=[
            row_message("user", "", content=content)])]
        self.ingest()
        rows = self.segments()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("system-reminder", rows[0]["text"])
        self.assertNotIn("Workspace Folder", rows[0]["text"])
        self.assertIn("真正的用户提问是什么", rows[0]["text"])

    def test_message_becomes_empty_after_strip_and_is_skipped(self):
        content = [{"type": "input_text",
                    "text": "<system-reminder>只有注入块</system-reminder>"}]
        self.paths = [self.write_session(rows=[row_message("user", "", content=content)])]
        result = self.ingest()
        self.assertEqual(self.segments(), [])
        self.assertEqual(result["segments_skipped"], 1)


class NoiseRowsTest(CorpusCase):
    """噪音行：不入库，但必须**计数**（否则"为什么这段对话没有过程"无法解释）。"""

    def test_reasoning_and_tool_rows_are_counted_not_stored(self):
        self.paths = [self.write_session(rows=[
            row_message("user", "问题"),
            {"type": "reasoning", "content": [], "rawContent":
                [{"type": "reasoning_text", "text": "不该入库的思考"}], "timestamp": MS},
            {"type": "function_call", "name": "Read", "arguments": "{}", "timestamp": MS},
            {"type": "function_call_result", "name": "Read", "output": {}, "timestamp": MS},
            {"type": "file-history-snapshot", "snapshot": {}, "timestamp": MS},
            row_message("assistant", "回答", mid="m9"),
        ])]
        self.ingest()
        texts = [r["text"] for r in self.segments()]
        self.assertEqual(texts, ["问题", "回答"])
        stats = {row["event_type"]: row for row in
                 store.list_event_stats(self.conn, "workbuddy")}
        for noisy in ("reasoning", "function_call", "function_call_result",
                      "file-history-snapshot"):
            self.assertIn(noisy, stats)
            self.assertEqual(stats[noisy]["skipped"], 1)


class BadLineTest(CorpusCase):
    """C-P0-2 红①：坏 JSON 行 → 跳行 + 计数，整文件仍 `ok`。"""

    def test_bad_json_line_does_not_fail_the_file(self):
        bad = b'{"type": "message", "role": "user", "content": [BROKEN'
        self.paths = [self.write_session(
            rows=[row_message("user", "第一行"), bad, row_message("assistant", "第三行")])]
        result = self.ingest()
        self.assertEqual(result["degraded"], [], "单行坏 JSON 不该让整个文件降级")
        self.assertEqual(store.read_cursor(self.conn, "workbuddy", "wb-session")["status"],
                         store.SOURCE_OK)
        degraded = store.list_degraded(self.conn, "workbuddy")
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["reason_code"], store.REASON_BAD_JSON)
        self.assertIn("第 2 行", degraded[0]["reason"])
        self.assertEqual([r["text"] for r in self.segments()], ["第一行", "第三行"])

    def test_oversize_line_is_skipped_and_counted(self):
        """超长行整行丢弃 + 降级（不整文件失败，也不把 8 MB 读进内存）。

        安全 🟡（2026-10-27）：整行丢弃同样是**声明过的损失**——文件状态必须降级、
        ingest 摘要必须计数、台账 `segments_kept` 不许写 0（段其实采到了）。
        """
        huge = b'{"type": "message", "role": "user", "content": "' + b"x" * 200000 + b'"}'
        self.paths = [self.write_session(
            rows=[row_message("user", "正常"), huge, row_message("assistant", "也正常")])]
        result = self.ingest(max_line_bytes=1024)
        degraded = store.list_degraded(self.conn, "workbuddy")
        codes = [d["reason_code"] for d in degraded]
        self.assertIn(store.REASON_OVERSIZE_LINE, codes)
        self.assertEqual(len(self.segments()), 2)
        self.assertGreaterEqual(degraded[0]["segments_kept"], 1,
                                "台账说 0 段，而这一行之前已经采到段了（与事实相反）")
        summary = {d["reason_code"] for d in result["degraded"]}
        self.assertIn(store.REASON_OVERSIZE_LINE, summary,
                      "适配器摘要说'降级 0'，与台账/doctor 不一致")
        self.assertEqual(result["status"], store.SOURCE_DEGRADED,
                         "整行丢弃之后适配器还报 ok（同一次采集两个相反读数）")
        cursor = store.read_cursor(self.conn, "workbuddy", "wb-session")
        self.assertEqual(cursor["status"], store.SOURCE_DEGRADED,
                         "游标行状态没落盘：doctor/stats 看不到这次损失")


class StreamingTest(CorpusCase):
    """C-P0-2 红②：大文件必须流式（内存峰值远低于红线）。"""

    def test_large_file_peak_memory_stays_small(self):
        rows = []
        for idx in range(400):
            rows.append(row_message("user", "第 %d 条消息 " % idx + "内容" * 200,
                                    mid="m%d" % idx))
        self.paths = [self.write_session(rows=rows)]
        size = self.paths[0].stat().st_size
        tracemalloc.start()
        try:
            result = self.ingest()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["segments_new"], 400)
        self.assertLess(peak, MEM_RED_LINE, "内存峰值超过 512 MB 红线（C-P0-2 红②）")
        self.assertLess(peak, MEM_TARGET,
                        "峰值 %d B 超过设计目标 64 MB（文件 %d B）" % (peak, size))


class IdempotencyTest(CorpusCase):
    """C-P0-4：重跑不变、追加只增。"""

    def test_second_run_is_noop_and_append_adds_one(self):
        path = self.write_session(rows=[row_message("user", "第一句")])
        self.paths = [path]
        first = self.ingest()
        self.assertEqual(first["segments_new"], 1)
        second = self.ingest()
        self.assertEqual(second["segments_new"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.append_rows(path, [row_message("assistant", "第二句", mid="m2")])
        third = self.ingest()
        self.assertEqual(third["segments_new"], 1)
        self.assertEqual(len(self.segments()), 2)
        # 续传后 `line=` 仍是**文件内**行号（不是从 1 重数）
        self.assertEqual([r["loc"] for r in self.segments()], ["line=1", "line=2"])

    def test_cursor_on_mid_line_is_reset(self):
        """源被重写后旧游标落在行中：必须归零重采，不许把半行当整行（静默少采）。"""
        path = self.write_session(rows=[row_message("user", "第一句")])
        self.paths = [path]
        self.ingest()
        with open(path, "wb") as fh:
            fh.write(json.dumps(row_message("user", "重写后的第一句"),
                                ensure_ascii=False).encode("utf-8") + b"\n")
        self.ingest()
        rows = self.segments()
        self.assertEqual(len(rows), 1, "重采不该多出段（loc 是幂等键）")
        self.assertEqual(rows[0]["text"], "重写后的第一句")


class TaggingFallbackTest(CorpusCase):
    """WB 头只有 session-meta/ai-title：缺 cwd 时必须如实写 `no_cwd`（不猜项目）。"""

    def test_missing_cwd_is_no_cwd(self):
        self.paths = [self.write_session(rows=[
            {"type": "session-meta", "sessionId": "wb-session", "timestamp": MS},
            {"id": "m1", "timestamp": MS, "type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "没有 cwd 的消息"}]},
        ])]
        self.ingest()
        session = self.session_row()
        self.assertEqual((session["project"], session["project_source"]), ("", "no_cwd"))
        self.assertEqual(session["sid8"], "wb-sessi")   # 前 8 位（"wb-sessi"）


class ResumePrefixTest(CorpusCase):
    """M16（2026-10-27 复审 🟡）：续传不许重读已采前缀（674 MB 会话的续传代价）。

    变异体：把 `iter_rows` 的 `base_lines` 参数忽略（继续 `_count_lines_before` 数一遍）
    → `prefix_bytes_read` 变成整个前缀的大小，本用例红。
    """

    def test_resume_reads_no_prefix_bytes(self):
        rows = [row_message("user", "第 %d 条消息" % idx, mid="m%d" % idx)
                for idx in range(1, 601)]
        path = self.write_session(rows=rows)
        self.paths = [path]
        first = self.ingest()
        self.assertEqual(first["segments_new"], 600)
        self.append_rows(path, [row_message("assistant", "续传才有的那句", mid="m601")])
        second = self.ingest()
        self.assertEqual(second["segments_new"], 1)
        self.assertEqual(second["prefix_bytes_read"], 0,
                         "续传又把已采前缀读了一遍（M16：游标里已有行号）")
        # 行号仍是**文件内**行号：续传那条必须接在 600 之后
        self.assertIn("line=601", [row["loc"] for row in self.segments()])

    def test_missing_cursor_line_falls_back_to_counting(self):
        """游标缺 `last_line_seen`（旧库）时仍要能算出行号——自证口径。"""
        rows = [row_message("user", "第 %d 条" % idx, mid="m%d" % idx) for idx in range(1, 11)]
        path = self.write_session(rows=rows)
        self.paths = [path]
        self.ingest()
        self.conn.execute("UPDATE corpus_source SET last_line_seen=NULL"
                          " WHERE host='workbuddy'")
        self.conn.commit()
        self.append_rows(path, [row_message("assistant", "第十一条", mid="m11")])
        result = self.ingest()
        self.assertEqual(result["segments_new"], 1)
        self.assertGreater(result["prefix_bytes_read"], 0,
                           "缺行号时应当重数一遍（自证），这里却没读任何前缀")
        self.assertIn("line=11", [row["loc"] for row in self.segments()])


class ZeroSegmentSourceTest(CorpusCase):
    """P2-1 的 WB 侧：整份只有噪音/空消息也要留游标行。"""

    def test_file_without_messages_still_gets_a_cursor_row(self):
        self.paths = [self.write_session(rows=[
            {"type": "session-meta", "sessionId": "wb-session", "timestamp": MS},
            {"type": "reasoning", "content": [], "timestamp": MS},
        ])]
        result = self.ingest()
        self.assertEqual(result["segments_new"], 0)
        cursor = store.read_cursor(self.conn, "workbuddy", "wb-session")
        self.assertIsNotNone(cursor, "0 段文件没有游标行：sessions 与 sources 差 1")


class WindowTest(CorpusCase):
    """采集时间窗口的 WB 侧：`--days` 是**跨宿主的同一口径**（门面解析、发现阶段过滤）。

    WB 的文件是 `*.jsonl`（逐行文本），发现阶段同样有 mtime——所以窗口在那里生效，
    而不是等读完 674 MB 才发现"这个文件不该采"。判据同 DSH 侧：窗口外的文件
    不建 source 行、不记 orphan，只计数。
    """

    DAY = 86400

    def _write_aged(self, sid, age):
        path = self.write_session(sid=sid, rows=[row_message("user", "%s 的原话" % sid,
                                                             mid=sid)])
        ts = time.time() - age
        os.utime(path, (ts, ts))
        return path

    def test_window_filters_wb_files_at_discovery(self):
        self._write_aged("wb-old", 4 * self.DAY)
        self._write_aged("wb-fresh", 2 * self.DAY)
        result = self.ingest(days=3)
        self.assertEqual(result["window_days"], 3)
        self.assertEqual(result["skipped_by_window"], 1)
        self.assertEqual(result["sessions_seen"], 1)
        ids = [row["session_id"] for row in self.conn.execute(
            "SELECT session_id FROM corpus_source WHERE host='workbuddy'"
            " ORDER BY session_id").fetchall()]
        self.assertEqual(ids, ["wb-fresh"], "窗口外的 WB 文件建了 source 行")
        self.assertEqual(result["orphan"], [])
        self.assertEqual([row["text"] for row in self.segments()], ["wb-fresh 的原话"])

    def test_days_zero_collects_all_wb_files(self):
        self._write_aged("wb-old", 4 * self.DAY)
        self._write_aged("wb-fresh", 2 * self.DAY)
        result = self.ingest(days=0)
        self.assertEqual(result["window_days"], 0)
        self.assertEqual(result["skipped_by_window"], 0)
        self.assertEqual(result["sessions_seen"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
