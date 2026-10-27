#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-P0-1 / C-P0-3 / C-P0-4 / C-P0-9 的 DSH 侧判据（T02 · 设计 §10）。

每个用例都写成"**坏输入必须红**"的形式，因为语料层最危险的失败是**静默少采**：
安静地丢掉一半对话，界面和其它判据全都正常，只有人回头找原话时才发现查不到。

| 判据 | 用例 | 红的样子（变异体） |
|---|---|---|
| C-P0-1 正常 | `BaselineTest`：段数 == 按 kind 白名单算出来的样本事件数 | M02 把白名单放宽成全部 `user/message` → 段数变多 |
| C-P0-1 红① | `TruncatedFrameTest`：帧间截断 → `degraded{truncated_frame}` + 不写半条脏段 | M05 坏帧只 `pass` → 没有降级台账 |
| C-P0-1 红② | `UnknownEventTest`：未知类型 → `event_stat.skipped++` 且文件仍 `ok` | M06 未知事件让整文件失败 |
| C-P0-3 | `TaggingTest`：cwd 命中/未命中/子代理缺父 | M07 未命中回填最近项目 / M09 跳过 project_source |
| C-P0-4 | `IdempotencyTest`：跑两次段数不变、追加只 +1、删游标重建一致 | M20 去掉 UNIQUE → 段数翻倍 |
| C-P0-9 | `StatsOrphanTest`：计数与磁盘对账、删文件 → `orphan` | M22 degraded 只打印不落表 |
| D03 | `SplicedTest`：`agent/inbox/spliced` 不入库 | M03 spliced 入库 → 段数翻倍 |
| C-P0-7 | `RedactionTest`：落盘前脱敏、源文件 sha256 不变 | M14 脱敏丢整段 → 段数骤降 |

纪律：所有库都在 `tempfile` 根下，**绝不碰 `p0/data/`**。
"""
import base64
import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

import zstandard

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish_corpus  # noqa: E402
import fish_corpus_store as store  # noqa: E402

MS = 1790918381967            # 样本里的 createdAt（毫秒）


def frame(payload) -> bytes:
    """把一条事件压成**独占一帧**（DSH 是"很多帧串联"，判据必须按帧造）。"""
    blob = payload if isinstance(payload, bytes) else \
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return zstandard.ZstdCompressor().compress(blob + b"\n")


def ev_session(sid, *, cwd="I:/app/anyi-studio", origin=None, parent=None, depth=None,
               preset=None):
    data = {"type": "session", "version": 4, "id": sid, "createdAt": MS, "cwd": cwd}
    if origin:
        data["origin"] = origin
    if parent:
        data["parentSession"] = parent
    if depth is not None:
        data["delegationDepth"] = depth
    if preset:
        data["agentPreset"] = preset
    return data


def ev_user(seq, text, *, kind="user", mid=None):
    return {"type": "user/message", "seq": seq, "time": MS + seq * 10,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": kind}, "role": "user", "id": mid or "u%d" % seq}}


def ev_assistant(seq, texts, *, turn=1, step=1):
    content = [{"type": "reasoning", "text": "想法不该入库"}]
    content.extend({"type": "text", "text": t} for t in texts)
    content.append({"type": "tool-call", "name": "Read", "arguments": "{}"})
    return {"type": "assistant/message", "seq": seq, "time": MS + seq * 10,
            "data": {"turn": turn, "step": step,
                     "message": {"role": "assistant", "content": content,
                                 "id": "a%d" % seq}}}


def ev_prune(seq, seqs):
    data = {"shadowedSeqs": list(seqs), "shadowedTokenCount": 100}
    if seqs:
        data["shadowedRange"] = [seqs[0], seqs[-1]]
    return {"type": "compaction/prune", "seq": seq, "time": MS + seq * 10,
            "data": data}


def ev_spliced(seq, text, mid):
    return {"type": "agent/inbox/spliced", "seq": seq, "time": MS + seq * 10,
            "data": {"target": "next-turn", "start": 0,
                     "inserted": [{"content": [{"type": "text", "text": text}],
                                   "id": mid}]}}


def ev_unknown(seq, etype="future/unknown-event"):
    return {"type": etype, "seq": seq, "time": MS + seq * 10, "data": {"x": 1}}


class CorpusCase(unittest.TestCase):
    """公共夹具：临时根 + 会话目录 + 连接（**永不碰生产 data/**）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-dsh-"))
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

    # ---- 造文件
    def write_session(self, sid, events, *, ws="--I-app-anyi-studio--",
                      name="session.v4.jsonl.zstd", truncate_index=None):
        folder = self.sessions / ws / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        frames = [frame(ev) for ev in events]
        if truncate_index is not None:
            cut = max(1, len(frames[truncate_index]) // 2)
            blob = b"".join(frames[:truncate_index]) + frames[truncate_index][:cut] \
                + b"".join(frames[truncate_index + 1:])
        else:
            blob = b"".join(frames)
        path.write_bytes(blob)
        self.last_frames = frames
        return path

    # ---- 跑采集
    def ingest(self, *, project_of=None, candidates_of=None, limit=None,
               dry_run=False, batch_frames=None, days=None):
        return fish_corpus.ingest(
            ("dsh",), root=self.root, conn=self.conn,
            roots={"dsh": [self.sessions]}, limit=limit, dry_run=dry_run,
            use_lock=False, project_of=project_of, candidates_of=candidates_of,
            batch_frames=batch_frames, days=days)

    def segments(self, sid=None):
        # 按 id（= 入库顺序）排：`loc` 是字符串（`seq=9` vs `seq=11`），字符串序会把
        # 11 排到 9 前面——那不是"消息顺序"，是判据自己写错了。
        if sid:
            return self.conn.execute(
                "SELECT * FROM corpus_segment WHERE host='dsh' AND session_id=?"
                " ORDER BY id", (sid,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM corpus_segment WHERE host='dsh' ORDER BY host, session_id, id"
        ).fetchall()

    def session_row(self, sid):
        return store.get_session(self.conn, "dsh", sid)

    def event_stats(self, sid):
        return {row["event_type"]: row for row in store.list_event_stats(self.conn, "dsh", sid)}


class BaselineTest(CorpusCase):
    """C-P0-1 绿：段数 == 按 kind 白名单算出来的样本事件数。"""

    def test_only_kind_user_and_assistant_text_become_segments(self):
        sid = "b1b0097b-447b-417a-9a6d-744298c62fdb"
        events = [
            ev_session(sid),
            ev_user(9, "第一条真人消息"),
            ev_user(10, "<system-reminder>注入块</system-reminder>", kind="runtime-context"),
            ev_user(11, "第二条真人消息"),
            ev_assistant(12, ["助手回答一"]),
            ev_assistant(13, ["助手回答二", "助手回答二·第二段"]),
            ev_prune(14, []),
        ]
        self.write_session(sid, events)
        result = self.ingest()
        self.assertEqual(result["sessions_seen"], 1)
        self.assertEqual(result["sessions_ok"], 1)
        rows = self.segments(sid)
        # 3 条 user 里只有 2 条 kind='user'；assistant 侧 text part 共 3 段
        self.assertEqual([(r["role"], r["loc"], r["part"]) for r in rows], [
            ("user", "seq=9", 0),
            ("user", "seq=11", 0),
            ("assistant", "seq=12", 0),
            ("assistant", "seq=13", 0),
            ("assistant", "seq=13", 1),
        ])
        self.assertEqual(result["segments_new"], 5)

    def test_injection_blocks_and_reasoning_never_stored(self):
        """M02 / PRD §3.1：注入块与 reasoning 一句都不许进库。"""
        sid = "aaaa1111-2222-3333-4444-555566667777"
        self.write_session(sid, [
            ev_session(sid),
            ev_user(1, "skill-catalog 注入", kind="skill-catalog"),
            ev_user(2, "time-context 注入", kind="time-context"),
            ev_user(3, "goal 注入", kind="goal"),
            ev_assistant(4, ["真的回答"]),
        ])
        self.ingest()
        texts = [r["text"] for r in self.segments(sid)]
        self.assertEqual(texts, ["真的回答"])
        self.assertNotIn("想法不该入库", " ".join(texts))

    def test_assistant_metadata_is_recorded(self):
        sid = "cccc1111-2222-3333-4444-555566667777"
        self.write_session(sid, [ev_session(sid), ev_assistant(7, ["带 turn/step"])])
        self.ingest()
        row = self.segments(sid)[0]
        self.assertEqual((row["turn"], row["step"]), (1, 1))
        self.assertEqual(row["offset_space"], "decompressed")
        self.assertGreater(row["byte_to"], row["byte_from"])
        self.assertTrue(row["occurred_at"].endswith("Z"))


class TruncatedFrameTest(CorpusCase):
    """C-P0-1 红①：帧间截断必须列名降级，且不写半条脏段。"""

    def test_middle_truncated_frame_is_degraded_and_not_stored(self):
        sid = "dddd1111-2222-3333-4444-555566667777"
        path = self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "帧一里的消息"),
            ev_user(3, "这一条在坏帧里，绝不能入库"),
            ev_user(4, "帧三里的消息"),
        ], truncate_index=2)
        good_bytes = len(self.last_frames[0]) + len(self.last_frames[1])
        # batch_frames=1：让"已提交的帧保留、坏帧不写"这条语义可观测
        result = self.ingest(batch_frames=1)
        self.assertEqual(len(result["degraded"]), 1)
        entry = result["degraded"][0]
        self.assertEqual(entry["reasons"], [store.REASON_TRUNCATED_FRAME])
        degraded = store.list_degraded(self.conn, "dsh")
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["reason_code"], store.REASON_TRUNCATED_FRAME)
        self.assertEqual(degraded[0]["path"], str(path))
        texts = [r["text"] for r in self.segments(sid)]
        self.assertEqual(texts, ["帧一里的消息"], "坏帧之后的段不该写进来")
        self.assertEqual(degraded[0]["segments_kept"], 1)
        # 游标停在最后一个完整帧边界 → 下次还能从那里续（不会把坏帧跳过去）
        cursor = store.read_cursor(self.conn, "dsh", sid)
        self.assertEqual(cursor["status"], store.SOURCE_DEGRADED)
        self.assertEqual(cursor["bytes_total"], good_bytes)

    def test_second_run_keeps_reporting_degraded_not_dropping_it(self):
        """M05：坏帧只 `pass` → 第二次跑就没有台账了（判据必须能抓住）。"""
        sid = "eeee1111-2222-3333-4444-555566667777"
        self.write_session(sid, [ev_session(sid), ev_user(2, "好的"),
                                 ev_user(3, "坏的"), ev_user(4, "好的二")],
                           truncate_index=2)
        self.ingest(batch_frames=1)
        self.ingest(batch_frames=1)
        degraded = store.list_degraded(self.conn, "dsh")
        self.assertEqual(len(degraded), 1)
        self.assertGreaterEqual(degraded[0]["hits"], 2, "反复失败只增 hits，不重复建行")

    def test_default_batch_does_not_lose_the_frames_before_the_bad_one(self):
        """R2（2026-10-27 复审 🔴）：**默认 `batch_frames`** 下坏帧不许连坐前面的好帧。

        旧实现的两个错叠成"永久少采"：坏帧只 ROLLBACK（把还没到提交阈值的整批一起
        丢掉）+ `committed_offset` 已前进（游标写成越过坏帧的值）→ 下一次从那之后
        开始读，丢掉的那批**永远不会再采**。判据同时锁三件事：
        ① 段不丢（好帧照样落盘）；② 游标停在最后一个成功边界；③ 坏帧仍在台账里。
        """
        sid = "0a0b0c0d-1111-2222-3333-444455556666"
        self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "好帧一"),
            ev_user(3, "好帧二"),
            ev_user(4, "坏帧里的这句绝不能入库"),
            ev_user(5, "坏帧之后的也不行"),
        ], truncate_index=3)
        good_bytes = (len(self.last_frames[0]) + len(self.last_frames[1])
                      + len(self.last_frames[2]))
        result = self.ingest()                      # 默认 batch_frames=64
        self.assertEqual([r["text"] for r in self.segments(sid)], ["好帧一", "好帧二"],
                         "坏帧把前面的好帧连坐丢掉了（默认批量下必现）")
        cursor = store.read_cursor(self.conn, "dsh", sid)
        self.assertEqual(cursor["status"], store.SOURCE_DEGRADED)
        self.assertEqual(cursor["bytes_total"], good_bytes,
                         "游标越过了坏帧：下一轮从坏帧之后开始读 = 永久少采")
        self.assertEqual(result["degraded"][0]["reasons"], [store.REASON_TRUNCATED_FRAME])
        second = self.ingest()
        self.assertEqual(second["segments_new"], 0, "重跑重复产段（幂等键失效）")
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 2,
                         "重跑把好帧丢了")
        self.assertGreaterEqual(store.list_degraded(self.conn, "dsh")[0]["hits"], 2)


class UnknownEventTest(CorpusCase):
    """C-P0-1 红②：未知事件类型 → 计数跳过，**不许整文件失败**。"""
    def test_unknown_event_is_counted_and_file_stays_ok(self):
        sid = "ffff1111-2222-3333-4444-555566667777"
        self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "正常消息"),
            ev_unknown(3),
            ev_unknown(4, "another/future-type"),
        ])
        result = self.ingest()
        self.assertEqual(result["degraded"], [])
        self.assertEqual(store.list_degraded(self.conn, "dsh"), [])
        stats = self.event_stats(sid)
        self.assertIn("future/unknown-event", stats)
        self.assertIn("another/future-type", stats)
        self.assertEqual(stats["future/unknown-event"]["skipped"], 1)
        self.assertEqual(stats["another/future-type"]["skipped"], 1)
        self.assertEqual(store.read_cursor(self.conn, "dsh", sid)["status"], store.SOURCE_OK)
        self.assertEqual([r["text"] for r in self.segments(sid)], ["正常消息"])


class SplicedTest(CorpusCase):
    """D03 / M03：`agent/inbox/spliced` 是既有消息的转投，入库会让段数翻倍。"""

    def test_spliced_does_not_become_a_segment(self):
        sid = "11223344-5566-7788-99aa-bbccddeeff00"
        same_text = "同一句话"
        self.write_session(sid, [
            ev_session(sid),
            ev_user(18, same_text, mid="dup-id"),
            ev_spliced(10, same_text, "dup-id"),
        ])
        self.ingest()
        rows = self.segments(sid)
        self.assertEqual(len(rows), 1, "spliced 入库了：同一句话有了两个门牌")
        self.assertEqual(rows[0]["loc"], "seq=18")


class PruneShadowTest(CorpusCase):
    """`compaction/prune` 只打 `shadowed`，**不删**（PRD §3.1 铁律）。"""

    def test_pruned_segments_are_marked_not_deleted(self):
        sid = "22334455-6677-8899-aabb-ccddeeff0011"
        self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "将被压缩出去的话"),
            ev_user(4, "还在上下文里的话"),
            ev_prune(6, [2]),
        ])
        self.ingest()
        rows = {r["loc"]: r for r in self.segments(sid)}
        self.assertEqual(len(rows), 2, "被压缩的消息必须仍在库里")
        self.assertEqual(rows["seq=2"]["shadowed"], 1)
        self.assertEqual(rows["seq=4"]["shadowed"], 0)
        self.assertEqual(self.session_row(sid)["shadowed"], 1)


class IdempotencyTest(CorpusCase):
    """C-P0-4：重跑不变、追加只增、删游标重建一致。"""

    def setUp(self):
        super().setUp()
        self.sid = "33445566-7788-99aa-bbcc-ddeeff001122"
        self.path = self.write_session(self.sid, [ev_session(self.sid),
                                                  ev_user(2, "第一句")])

    def test_second_run_is_a_no_op(self):
        first = self.ingest()
        self.assertEqual(first["segments_new"], 1)
        second = self.ingest()
        self.assertEqual(second["segments_new"], 0)
        self.assertEqual(second["unchanged"], 1, "size+mtime 没变就必须跳过（M21）")
        self.assertEqual(store.count_segments(self.conn, "dsh", self.sid), 1)

    def test_append_one_message_adds_exactly_one(self):
        self.ingest()
        with open(self.path, "ab") as fh:
            fh.write(frame(ev_user(3, "第二句")))
        result = self.ingest()
        self.assertEqual(result["segments_new"], 1)
        self.assertEqual(store.count_segments(self.conn, "dsh", self.sid), 2)

    def test_deleting_cursor_rebuilds_the_same_count(self):
        self.ingest()
        before = store.count_segments(self.conn, "dsh", self.sid)
        self.conn.execute("DELETE FROM corpus_source")
        self.conn.commit()
        self.ingest()
        after = store.count_segments(self.conn, "dsh", self.sid)
        self.assertEqual(before, after, "重建段数与首次不一致（幂等键失效）")

    def test_rewritten_text_updates_instead_of_adding(self):
        """幂等键是 `loc`，不掺文本哈希：源被改写要"更新段"，不是"多一段"。"""
        self.ingest()
        self.write_session(self.sid, [ev_session(self.sid), ev_user(2, "第一句·改")])
        self.ingest()
        rows = self.segments(self.sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "第一句·改")


class StatsOrphanTest(CorpusCase):
    """C-P0-9：计数对账 + 删文件 → orphan。"""

    def test_counts_add_up_and_orphan_is_marked(self):
        sid_a, sid_b = "44556677-8899-aabb-ccdd-eeff00112233", \
            "55667788-99aa-bbcc-ddee-ff0011223344"
        path_a = self.write_session(sid_a, [ev_session(sid_a), ev_user(2, "A")])
        self.write_session(sid_b, [ev_session(sid_b), ev_user(2, "B")])
        first = self.ingest()
        self.assertEqual(first["sessions_seen"], 2)
        self.assertEqual(first["sessions_ok"] + first["sessions_skipped"]
                         + len(first["degraded"]), 2)
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["segments"], 2)
        self.assertEqual(stats["by_host"]["dsh"]["sources"], 2)

        path_a.unlink()
        second = self.ingest()
        self.assertEqual(len(second["orphan"]), 1)
        self.assertEqual(second["orphan"][0]["session_id"], sid_a)
        stats2 = fish_corpus.stats(conn=self.conn)
        self.assertEqual([o["session_id"] for o in stats2["orphan"]], [sid_a])

    def test_limit_selects_newest_files_first(self):
        for idx, sid in enumerate(["aa" * 16, "bb" * 16, "cc" * 16]):
            path = self.write_session(sid, [ev_session(sid), ev_user(2, "x")])
            os.utime(path, (1e9 + idx, 1e9 + idx))
        result = self.ingest(limit=2)
        self.assertEqual(result["sessions_seen"], 3)
        self.assertEqual(result["sessions_skipped"], 1)
        self.assertEqual(result["sessions_ok"], 2)


class TaggingTest(CorpusCase):
    """C-P0-3：cwd 命中/未命中/子代理缺父（打标在采集链路里的实际效果）。"""

    def test_mapped_project_comes_from_cwd(self):
        sid = "66778899-aabb-ccdd-eeff-001122334455"
        self.write_session(sid, [ev_session(sid, cwd="I:/app/anyi-studio"),
                                 ev_user(2, "在 ani-studio 里说的")])
        self.ingest(project_of=lambda cwd: "anyi-studio" if "anyi-studio" in cwd else None)
        row = self.session_row(sid)
        self.assertEqual((row["project"], row["project_source"]), ("anyi-studio", "mapped"))
        self.assertEqual(row["workspace"], "i:/app/anyi-studio")
        self.assertEqual(row["sid8"], "66778899")

    def test_unmapped_cwd_never_borrows_another_project(self):
        """C-P0-3 红①：`workspace\\learn` 未映射时必须留空，硬塞项目即红。"""
        sid = "778899aa-bbcc-ddee-ff00-112233445566"
        self.write_session(sid, [ev_session(sid, cwd="I:/app/DSH-Home/workspace/learn"),
                                 ev_user(2, "在 learn 根目录说的")])
        self.ingest(project_of=lambda cwd: None,
                    candidates_of=None)
        row = self.session_row(sid)
        self.assertEqual((row["project"], row["project_source"]), ("", "unmapped"))

    def test_directory_name_never_decides_project(self):
        """C-P0-3 红②：目录名是编码串，用它判项目即红（R1）。"""
        sid = "8899aabb-ccdd-eeff-0011-223344556677"
        self.write_session(sid, [ev_session(sid, cwd=None), ev_user(2, "没有 cwd")],
                           ws="--I-app-anyi-studio--")
        self.ingest()
        row = self.session_row(sid)
        self.assertEqual((row["project"], row["project_source"]), ("", "no_cwd"))

    def test_subagent_head_is_tagged_with_parent_and_depth(self):
        sid = "99aabbcc-ddee-ff00-1122-334455667788"
        self.write_session(sid, [ev_session(sid, origin="subagent", parent="parent-sid",
                                            depth=2, preset="software-team"),
                                 ev_user(2, "子代理任务正文")])
        self.ingest()
        row = self.session_row(sid)
        self.assertEqual((row["origin"], row["origin_basis"]), ("subagent", "header"))
        self.assertEqual((row["parent_session"], row["delegation_depth"]), ("parent-sid", 2))
        self.assertEqual(row["agent_preset"], "software-team")

    def test_subagent_without_parent_is_red(self):
        """C-P0-3 红③：子代理缺 parent/depth → 降级 `bad_header`，不当主会话放过去。"""
        sid = "aabbccdd-eeff-0011-2233-445566778899"
        path = self.write_session(sid, [ev_session(sid, origin="subagent", depth=1),
                                        ev_user(2, "不该采成主会话")])
        result = self.ingest()
        self.assertEqual(len(result["degraded"]), 1)
        self.assertEqual(result["degraded"][0]["reasons"], [store.REASON_BAD_HEADER])
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 0)
        self.assertEqual(store.read_cursor(self.conn, "dsh", sid)["status"],
                         store.SOURCE_DEGRADED)

    def test_unsupported_format_is_registered_not_skipped(self):
        """v3/legacy 未取证：登记 `unsupported_format`，不许静默跳过（C-P0-9）。"""
        sid = "bbccddee-ff00-1122-3344-556677889900"
        self.write_session(sid, [ev_session(sid), ev_user(2, "x")],
                           name="session.v3.jsonl.zstd")
        result = self.ingest()
        self.assertEqual(result["sessions_seen"], 1)
        self.assertEqual(result["degraded"][0]["reasons"], [store.REASON_UNSUPPORTED_FORMAT])
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 0)


class RedactionTest(CorpusCase):
    """C-P0-7：落盘前脱敏；源日志一个字都不许改。"""

    def test_token_is_redacted_before_storage(self):
        sid = "ccddeeff-0011-2233-4455-66778899aabb"
        secret = "fsh_" + "Z" * 24
        path = self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "这是凭证 %s 别外传" % secret),
        ])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.ingest()
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after, "源日志被改写了（PRD O7：永不写回）")
        row = self.segments(sid)[0]
        self.assertNotIn(secret, row["text"])
        self.assertNotIn("fsh_", row["text"])
        self.assertIn("已脱敏", row["text"])
        self.assertEqual(row["redacted_count"], 1)
        # 段不许被整段丢掉（M14）：正文其余部分必须还在
        self.assertIn("这是凭证", row["text"])
        self.assertEqual(len(row["raw_sha256"]), 64)


class DryRunTest(CorpusCase):
    """`--dry-run` 不写库（验收命令要求"只读真目录，不建真库"）。"""

    def test_dry_run_does_not_touch_the_database(self):
        sid = "ddeeff00-1122-3344-5566-778899aabbcc"
        self.write_session(sid, [ev_session(sid), ev_user(2, "演练而已")])
        result = self.ingest(dry_run=True)
        self.assertEqual(result["segments_new"], 1, "演练也要能报出「会写多少」")
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 0)
        self.assertIsNone(store.read_cursor(self.conn, "dsh", sid))


class PathIdMismatchTest(CorpusCase):
    """R1（2026-10-27 复审 🔴）：目录名 ≠ 头部 id → **不递归**、映射落盘、第二轮幂等。

    变异体：把"只允许重启一次"改回无条件重入 → 本文件直接 `RecursionError`
    （实测整轮采集中断），第一、第三条用例都会红。
    """

    DIR_SID = "aaaa0000-1111-2222-3333-444455556666"
    TRUE_SID = "bbbb0000-1111-2222-3333-444455556666"

    def write_mismatch(self, events=None):
        return self.write_session(self.DIR_SID, events or [
            ev_session(self.TRUE_SID), ev_user(2, "目录名和头部 id 不一样")])

    def test_segments_land_on_the_header_id_without_recursion(self):
        self.write_mismatch()
        result = self.ingest()            # 能返回就说明重入有界（不会栈溢出）
        self.assertEqual(result["sessions_seen"], 1)
        self.assertEqual([r["text"] for r in self.segments(self.TRUE_SID)],
                         ["目录名和头部 id 不一样"])
        # 复审 🟡：旧实现把这条写进 `counters["warning"]`——没有任何调用方读它，
        # 于是"目录名≠头部 id 发生过"在返回/CLI 上完全看不见（死键）。
        self.assertEqual([w["warning"] for w in result["warnings"]], ["path_id_mismatch"])
        self.assertEqual(result["warnings"][0]["session_id"], self.TRUE_SID)
        self.assertEqual(
            store.get_meta(self.conn, store.path_id_key("dsh", self.DIR_SID)),
            self.TRUE_SID, "没有落目录名→头部 id 的映射（下一轮还得再发现一次）")
        self.assertIsNone(store.get_session(self.conn, "dsh", self.DIR_SID),
                          "目录名那一侧留下了幽灵会话行")

    def test_second_ingest_is_idempotent(self):
        self.write_mismatch()
        self.ingest()
        second = self.ingest()
        self.assertEqual(second["segments_new"], 0)
        self.assertEqual(second["unchanged"], 1, "第二轮应直接命中映射，不该重采")
        self.assertEqual(store.count_segments(self.conn, "dsh", self.TRUE_SID), 1)
        self.assertEqual(len(store.list_sources(self.conn, "dsh")), 1,
                         "游标行重复（一文件两条游标）")

    def test_second_header_with_another_id_converges_to_degraded(self):
        """已经按真 id 采过一轮又读到第三个 id：**降级收敛**，绝不递归。"""
        second_sid = "cccc0000-1111-2222-3333-444455556666"
        self.write_session(self.DIR_SID, [
            ev_session(self.TRUE_SID),
            ev_user(2, "第一个头下的好帧"),
            ev_session(second_sid),
            ev_user(3, "第二个头之后的话"),
        ])
        result = self.ingest()
        self.assertEqual([d["reasons"] for d in result["degraded"]],
                         [[store.REASON_BAD_HEADER]])
        self.assertEqual([r["text"] for r in self.segments(self.TRUE_SID)],
                         ["第一个头下的好帧"], "收敛前解析到的段不该被丢掉")


class ZeroSegmentSourceTest(CorpusCase):
    """P2-1：0 段文件也必须留游标行（sessions 与 sources 必须对得上，也不许每轮重扫）。"""

    def test_header_only_file_still_gets_a_cursor_row(self):
        sid = "11223344-0000-0000-0000-000000009901"
        self.write_session(sid, [
            ev_session(sid),
            {"type": "system/message", "seq": 2, "time": MS,
             "data": {"source": {"kind": "system"}, "content": []}},
        ])
        result = self.ingest()
        self.assertEqual(result["segments_new"], 0)
        cursor = store.read_cursor(self.conn, "dsh", sid)
        self.assertIsNotNone(cursor, "0 段文件没有游标行：sessions 与 sources 差 1（P2-1）")
        self.assertEqual(cursor["status"], store.SOURCE_OK)
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["sessions"], stats["by_host"]["dsh"]["sources"],
                         "会话数与游标行数对不上账")


class OversizeGuardTest(CorpusCase):
    """M6：解压输出与段正文都有上限，超限**降级**（不静默截断、不 OOM）。"""

    def test_high_ratio_frame_is_degraded_without_segments(self):
        """高压缩比帧：帧头声明的大小就超上限 → 连解都不解，直接降级。"""
        sid = "22334455-0000-0000-0000-000000009902"
        path = self.write_session(sid, [
            ev_session(sid),
            ev_user(2, "长" * 200000),      # 压完只剩几百字节，解出来 ~600 KB
            ev_user(3, "正常一句"),
        ])
        result = fish_corpus.adapter_for("dsh").ingest_file(
            self.conn, path, None, max_frame_bytes=4096,
            tagger=fish_corpus.tag_mod.Tagger())
        self.assertEqual(result["status"], store.SOURCE_DEGRADED)
        self.assertEqual([d["reason_code"] for d in result["degraded"]],
                         [store.REASON_OVERSIZE_FRAME])
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 0,
                         "超限帧仍然产出了段（上限没生效）")
        degraded = store.list_degraded(self.conn, "dsh")
        self.assertEqual(degraded[0]["reason_code"], store.REASON_OVERSIZE_FRAME)

    def test_oversize_text_part_is_truncated_and_declared(self):
        """单条正文超过 `MAX_PART_CHARS`：截断入库 + `oversize_text` 台账（不静默）。

        安全 🟡（2026-10-27）：截断是**声明过的损失**，所以三处读数必须一致——
        状态 `degraded`、ingest 摘要里的 `degraded` 非空、台账 `segments_kept` 不是 0
        （旧实现只写台账：摘要说"降级 0"、段数写 0，而段其实入库了）。
        """
        sid = "33445566-0000-0000-0000-000000009903"
        self.write_session(sid, [ev_session(sid),
                                 ev_user(2, "长" * (store.MAX_PART_CHARS + 500))])
        result = self.ingest()
        rows = self.segments(sid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["text"]), store.MAX_PART_CHARS)
        degraded = store.list_degraded(self.conn, "dsh")
        codes = [d["reason_code"] for d in degraded]
        self.assertIn(store.REASON_OVERSIZE_TEXT, codes,
                      "截断没有登记：损失发生了但没人知道")
        self.assertEqual(codes[0], store.REASON_OVERSIZE_TEXT)
        self.assertGreaterEqual(degraded[0]["segments_kept"], 1,
                                "台账说这个文件 0 段，而段其实入库了（计数与事实相反）")
        self.assertEqual(store.read_cursor(self.conn, "dsh", sid)["status"],
                         store.SOURCE_DEGRADED, "声明过的损失必须让文件状态降级")
        summary_codes = {code for item in result["degraded"] for code in item["reasons"]}
        self.assertIn(store.REASON_OVERSIZE_TEXT, summary_codes,
                      "ingest 摘要说'降级 0'，与台账/doctor 不一致")

    def test_streamed_frame_without_declared_size_is_capped_by_output(self):
        """流式写的帧**不声明**解压后大小（DSH 真实日志就是这样）：只能靠产出侧上限。

        这条与上一条互补：上一条走帧头预检，这一条必须走累计输出的硬检查。
        变异体：把 `if cap and produced > cap` 改成恒假 → 这里红（上一片仍绿）。
        """
        import zstandard as zstd
        sid = "22334455-0000-0000-0000-000000009912"
        folder = self.sessions / "--I-app-oversize--" / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        compressor = zstd.ZstdCompressor().compressobj()     # 流式：帧头无 content size
        lines = [json.dumps(ev_session(sid), ensure_ascii=False).encode("utf-8") + b"\n",
                 json.dumps(ev_user(2, "长" * 200000), ensure_ascii=False).encode("utf-8")
                 + b"\n"]
        blob = b""
        for line in lines:
            blob += compressor.compress(line)
        blob += compressor.flush()
        path.write_bytes(blob)
        self.assertEqual(zstd.frame_content_size(path.read_bytes()[:18]), -1,
                         "样本帧声明了内容大小：这条判据就测不到产出侧上限了")
        result = fish_corpus.adapter_for("dsh").ingest_file(
            self.conn, path, None, max_frame_bytes=4096,
            tagger=fish_corpus.tag_mod.Tagger())
        self.assertEqual(result["status"], store.SOURCE_DEGRADED)
        self.assertIn(store.REASON_OVERSIZE_FRAME,
                      [d["reason_code"] for d in result["degraded"]])
        self.assertEqual(store.count_segments(self.conn, "dsh", sid), 0)

    def test_default_frame_cap_is_far_above_real_frames(self):
        """上限不能把正常会话卡住：实测最大帧 152 KB，缺省上限必须是量级更高的数。"""
        self.assertGreaterEqual(fish_corpus.dsh_adapter.DEFAULT_MAX_FRAME_BYTES, 1 << 20)


class TitleRedactionTest(CorpusCase):
    """S3：标题写入前必须脱敏（标题会进 search/read 回执与推送行）。"""

    def test_title_with_token_is_redacted_before_storage(self):
        sid = "44556677-0000-0000-0000-000000009904"
        secret = "fsh_" + "T" * 24
        self.write_session(sid, [
            ev_session(sid),
            {"type": "session/title", "seq": 3, "time": MS,
             "data": {"title": "凭证 %s 的会话" % secret}},
            ev_user(2, "正文"),
        ])
        self.ingest()
        title = self.session_row(sid)["title"]
        self.assertNotIn(secret, title)
        self.assertNotIn("fsh_", title)
        self.assertIn("已脱敏", title)


class SkipEventTypesTest(CorpusCase):
    """🟡-14：流式碎片/回执进 skip 表——不然 unknown 占比 >95%，漂移信号被自己淹没。"""

    NEW_TYPES = ("reasoning-chunks", "assistant/chunk", "tool-call-chunks", "text-chunks",
                 "session-log-deepseek/delivery-accepted")

    def test_streaming_fragments_are_skipped_not_unknown(self):
        sid = "55667788-0000-0000-0000-000000009905"
        events = [ev_session(sid), ev_user(2, "正常一句")]
        events.extend(ev_unknown(10 + idx, etype)
                      for idx, etype in enumerate(self.NEW_TYPES))
        self.write_session(sid, events)
        result = self.ingest()
        self.assertEqual(result["unknown_events"], 0,
                         "流式碎片还算 unknown：unknown 计数失去漂移含义")
        stats = self.event_stats(sid)
        for etype in self.NEW_TYPES:
            self.assertIn(etype, stats, "%s 没进 event_stat" % etype)
            self.assertEqual(stats[etype]["skipped"], 1)


class StallAlarmTest(CorpusCase):
    """C-P0-9 红②：连续 3 轮"新段与新事件**双 0** 但源在变" → `stats.alarm='stalled'`。

    2026-10-27 复审 🟠-2 把口径与造红手法一起换了：
      · 旧写法用 `os.utime` 触碰 mtime —— 一个字节都没多，那是**假**的"源在变"；
        现在它必须既不重读也不报警（见 `MtimeTouchTest`）；
      · 新写法追加"只有空白行的帧"：游标真的前进（`bytes_total` 变大）、事件数与段数
        都是 0 —— 这才是"源在长、我却一段都没采到"的空转形态。

    变异体：把 `_update_stall` 摘掉（或把 alarm 恒置 None）→ 本文件红。
    """

    @staticmethod
    def blank_frame() -> bytes:
        """一帧只有空白行：能消费字节，但不产生任何事件/段（合法的空转样本）。"""
        return frame(b"\n\n\n")

    def test_three_zero_new_rounds_with_moving_source_raise_the_alarm(self):
        sid = "66778899-0000-0000-0000-000000009906"
        path = self.write_session(sid, [ev_session(sid), ev_user(2, "只有这一句")])
        self.ingest()                       # 第一轮有新增：计数归零
        self.assertIsNone(fish_corpus.stats(conn=self.conn)["alarm"])
        for _round in range(fish_corpus.STALL_STREAK_ALARM):
            with open(path, "ab") as fh:     # 源真的在长，但没有新事件/新段
                fh.write(self.blank_frame())
            result = self.ingest()
            self.assertEqual(result["segments_new"], 0)
            self.assertEqual(result["events_seen"], 0,
                             "空白行也算事件了：这条判据测不到'双 0'")
            self.assertTrue(result["stall"]["source_moved"],
                            "源明明长了字节，哨兵却说没动（指纹漏了游标位置）")
        stats = fish_corpus.stats(conn=self.conn)
        self.assertEqual(stats["alarm"], "stalled", stats["stall"])
        self.assertGreaterEqual(stats["stall"]["zero_streak"],
                                fish_corpus.STALL_STREAK_ALARM)
        self.assertEqual(store.get_meta(self.conn, fish_corpus.META_STALL_ALARM), "stalled")
        # 医生也要看得见（复审 🟡：报告说"doctor 打印停滞"，代码里其实没读）
        doc = fish_corpus.doctor(root=self.root)
        self.assertIn("stalled", doc["reasons"])
        _rc, lines, _accept = fish_corpus.run_cli("doctor", root=self.root)
        self.assertTrue(any("停滞" in line for line in lines), lines)

    def test_events_without_segments_do_not_alarm(self):
        """事件采到了、只是都不成段（全是 skip 类）→ **不是**停滞（复审 🟠-2 的"双 0"）。

        判据要点：只看"新段为 0"会把这批也算成空转，而它其实是"采到了、按规矩没入库"
        （spliced 是既有消息的转投）。这条用例专门让 `events_seen>0`。
        """
        sid = "99aabbcc-0000-0000-0000-000000009909"
        path = self.write_session(sid, [ev_session(sid), ev_user(2, "第一句")])
        self.ingest()
        for round_no in range(fish_corpus.STALL_STREAK_ALARM):
            with open(path, "ab") as fh:
                fh.write(frame(ev_spliced(100 + round_no, "转投的旧消息", "dup-%d" % round_no)))
            result = self.ingest()
            self.assertEqual(result["segments_new"], 0)
            self.assertEqual(result["events_seen"], 1, "这条用例要求事件被采到（不是双 0）")
        stats = fish_corpus.stats(conn=self.conn)
        self.assertIsNone(stats["alarm"],
                          "事件采到了、只是没成段，被误报成停滞（判据只看段数）")
        self.assertEqual(stats["stall"]["zero_streak"], 0)

    def test_alarm_clears_when_new_segments_arrive(self):
        sid = "778899aa-0000-0000-0000-000000009907"
        path = self.write_session(sid, [ev_session(sid), ev_user(2, "第一句")])
        self.ingest()
        for _round in range(fish_corpus.STALL_STREAK_ALARM):
            with open(path, "ab") as fh:
                fh.write(self.blank_frame())
            self.ingest()
        self.assertEqual(fish_corpus.stats(conn=self.conn)["alarm"], "stalled")
        with open(path, "ab") as fh:         # 真的有新内容了
            fh.write(frame(ev_user(3, "新增的一句")))
        self.ingest()
        stats = fish_corpus.stats(conn=self.conn)
        self.assertIsNone(stats["alarm"], "有新段了还报警（哨兵不会收）")
        self.assertEqual(stats["stall"]["zero_streak"], 0)


class MtimeTouchTest(CorpusCase):
    """复审 🟠-2：**纯 mtime 触碰**不许被当成"源在变"。

    两条一起判：
      ① `is_frame_boundary(offset == size)` 必须是 True —— 否则游标一见到新 mtime 就
         归零重读整份文件（旧实现恒 False，实测把每个日报用例拖成分钟级）；
      ② 哨兵归零 —— 触碰 4 次（> 阈值 3）也必须 `alarm is None`，且不产生新段/新事件。
    """

    SID = "8899aabb-0000-0000-0000-000000009908"

    def test_offset_at_eof_is_a_frame_boundary(self):
        path = self.write_session(self.SID, [ev_session(self.SID), ev_user(2, "一句")])
        size = path.stat().st_size
        is_boundary = fish_corpus.dsh_adapter.is_frame_boundary
        self.assertTrue(is_boundary(path, size), "文件尾必须算帧边界（否则整份重读）")
        self.assertTrue(is_boundary(path, size, size))
        # `offset > size` 与 `offset == size` **方向相反**（本次回归 🟠-2）：越过文件尾
        # 意味着"源被修剪得更短、游标悬空"，那必须归零重采，绝不能认成文件尾。
        self.assertFalse(is_boundary(path, size + 1024, size),
                         "越过文件尾被认成边界：源变短后游标不归零，新内容永远读不到")
        self.assertFalse(is_boundary(path, 1, size), "帧中间不该算边界（这条是原有的红判据）")

    def test_pure_mtime_touch_neither_rereads_nor_alarms(self):
        path = self.write_session(self.SID, [ev_session(self.SID), ev_user(2, "只有这一句")])
        self.ingest()
        size = path.stat().st_size
        for round_no in range(fish_corpus.STALL_STREAK_ALARM + 1):
            os.utime(path, (1.6e9 + round_no, 1.6e9 + round_no))
            result = self.ingest()
            self.assertEqual(result["segments_new"], 0)
            self.assertEqual(result["segments_updated"], 0,
                             "纯 mtime 触碰就重读了（is_frame_boundary 没认文件尾）")
            self.assertEqual(result["events_seen"], 0, "重读把事件也重新数了一遍")
            cursor = store.read_cursor(self.conn, "dsh", self.SID)
            self.assertEqual(int(cursor["bytes_total"]), int(size),
                             "游标被归零了：触碰一下就把整份文件重读一遍")
        stats = fish_corpus.stats(conn=self.conn)
        self.assertIsNone(stats["alarm"], "纯触碰被当成'源在变'（假停滞）")
        self.assertEqual(stats["stall"]["zero_streak"], 0)


class SourceShrunkTest(CorpusCase):
    """本次回归 🟠-2：**源被修剪得更短**时游标必须归零重采。

    现场（实测）：源从 668 B 被修剪到 281 B，`bytes_total=668 > size=281`——旧判据
    `offset >= size → 帧边界` 把悬空游标认成"文件尾"，于是 `iter_frames` 从越界位置读不到
    任何帧：`segments_new=0`、游标一动不动，**新内容永远读不到**（而且每轮都静默）。

    判据三件事一起看：① 重采发生（新段进来了）；② 段数正确、无重复行；③ 游标收敛到
    新长度（否则下一轮还是"少采"）。
    """

    SID = "aabbccdd-0000-0000-0000-000000009910"

    def test_shrunk_source_resets_cursor_and_reingests(self):
        long_a = "第一句：" + "甲" * 120
        long_b = "第二句：" + "乙" * 120
        path = self.write_session(self.SID, [ev_session(self.SID), ev_user(2, long_a),
                                            ev_user(3, long_b)])
        self.ingest()
        self.assertEqual(len(self.segments(self.SID)), 2)
        old_size = path.stat().st_size
        cursor = store.read_cursor(self.conn, "dsh", self.SID)
        self.assertEqual(int(cursor["bytes_total"]), old_size)
        # 源被修剪/改写：只剩头 + 一条**没见过**的新消息，整体比旧文件短
        is_boundary = fish_corpus.dsh_adapter.is_frame_boundary
        new_path = self.write_session(self.SID, [ev_session(self.SID), ev_user(5, "修剪后新内容")])
        new_size = new_path.stat().st_size
        self.assertLess(new_size, old_size, "夹具无效：新文件没有变短（这条判据要求变短）")
        self.assertFalse(is_boundary(new_path, old_size, new_size),
                         "源变短时 is_frame_boundary 说'还是边界'：游标不会归零")
        result = self.ingest()
        self.assertGreaterEqual(result["segments_new"], 1,
                                "源变短后没重采：新内容被永久少采")
        # 新内容真的进来了（门牌能读），且只在库里出现一份
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM corpus_segment WHERE host='dsh' AND session_id=?"
            " AND loc='seq=5'", (self.SID,)).fetchone()[0]
        self.assertEqual(int(rows), 1, "重采产生了重复段（段不幂等）")
        got = fish_corpus.read("log:dsh:%s#seq=5" % self.SID, root=self.root)
        self.assertIn("修剪后新内容", got["text"])
        # 游标收敛到新长度；旧段按"只更新不删"保留（投影是追加式事实）
        cursor = store.read_cursor(self.conn, "dsh", self.SID)
        self.assertEqual(int(cursor["bytes_total"]), new_size,
                         "游标没收敛到新长度：下一轮还是从悬空位置读")
        self.assertEqual(len(self.segments(self.SID)), 3, "段数应 = 旧 2 + 新 1（无重复）")
        again = self.ingest()
        self.assertEqual(again["segments_new"], 0, "同一份源被反复重采（游标没生效）")
        self.assertEqual(again["segments_updated"], 0)


class EventsSeenSemanticsTest(CorpusCase):
    """评审（终验黄项）：`corpus_source.events_seen` 是**文件当前实有事件数**，不是跨轮累计。

    现场（实测）：源从 3 个事件被修剪成 2 个，归零重采写回 `3 + 2 = 5`——读数比文件多出
    一倍。停滞哨兵/对账都拿它当"文件里有多少事件"，虚增等于把"少了什么"藏起来。
    修法：从头扫（首采或归零重采）时以**本次**为准；增量续传才叠加旧游标。

    两个方向都判：① 截短重采后 == 文件实有；② 正常追加后 == 文件实有（别把口径修成
    "永远只报本次"，那样增量续传会少报）。
    """

    SID = "aabbccdd-0000-0000-0000-00000000ee01"

    def events_seen(self):
        return int(store.read_cursor(self.conn, "dsh", self.SID)["events_seen"])

    def test_reset_recount_reports_the_file_not_the_cumulative_sum(self):
        self.write_session(self.SID, [ev_session(self.SID), ev_user(2, "甲" * 100),
                                      ev_user(3, "乙" * 100)])
        self.ingest()
        self.assertEqual(self.events_seen(), 3, "首采的事件数就不是文件实有数")
        # 源被修剪：只剩头 + 一条新消息（整体比旧文件短 → 游标悬空 → 归零重采）
        self.write_session(self.SID, [ev_session(self.SID), ev_user(5, "修剪后")])
        self.ingest()
        self.assertEqual(self.events_seen(), 2,
                         "归零重采把旧游标的事件数又加了一遍（3+2=5），而文件只有 2 个事件")

    def test_incremental_append_keeps_the_scan_count(self):
        self.write_session(self.SID, [ev_session(self.SID), ev_user(2, "第一句")])
        self.ingest()
        self.assertEqual(self.events_seen(), 2)
        self.write_session(self.SID, [ev_session(self.SID), ev_user(2, "第一句"),
                                      ev_user(3, "追加的一句")])
        self.ingest()
        self.assertEqual(self.events_seen(), 3,
                         "增量追加后事件数不是文件实有数（口径修成了「只报本次」）")


def streamed_frame(lines) -> bytes:
    """把若干事件压成**一个流式帧**（帧头不声明解压后大小）——DSH 真实日志就是这样写的。

    为什么必须用流式压缩造样本：单发 `compress()` 会把真实大小写进帧头，于是
    `_declared_frame_size` 预检先命中，永远测不到"产出侧累计上限"和"已解出的段"。
    """
    compressor = zstandard.ZstdCompressor().compressobj()
    blob = b""
    for line in lines:
        blob += compressor.compress(json.dumps(line, ensure_ascii=False).encode("utf-8")
                                    + b"\n")
    return blob + compressor.flush()


def incompressible(seq, size):
    """一段**不可压**的正文（伪随机字节的 base64：压缩比 ≈ 1）。

    为什么要不可压：用重复字符造 65 MiB 帧时压缩比能到 1000:1，一次解压调用就能产出
    GB 级内存——那测的是"压缩炸弹"，不是"大帧"；本文件的判据要的是后者。
    """
    raw = random.Random(seq).randbytes(max(1, size * 3 // 4))
    return base64.b64encode(raw).decode("ascii")


def ev_filler(seq, size):
    """一条不进库的填充事件（`tool/call` 在 skip 表里），用来把帧撑到目标大小。"""
    return {"type": "tool/call", "seq": seq, "time": MS + seq,
            "data": {"name": "Read", "arguments": incompressible(seq, size)}}


def env_var(name, value):
    """临时设/删环境变量（判据跑完必须还原：上限是全局可见的）。"""
    old = os.environ.get(name)

    def restore():
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)
    return restore


class FrameCapConfigTest(CorpusCase):
    """A①（2026-10-27 采集完整性）：单帧上限**可配**（默认 64 MiB），三级覆盖。

    背景（真实数据）：首采 538 会话里 10+ 个帧超过旧的 8 MiB 上限，被整帧降级成
    `oversize_frame`，对话原话永久缺失。上限本身是对的（防 OOM），错的是"不可配 +
    默认太低 + 超限整帧丢"。

    判据：① 9 MiB 流式帧在**默认上限**下能采出段（旧默认 8 MiB 必红）；② 环境变量
    覆盖生效（同一份帧在上限 1 MiB 时降级，且降级前解出的段保留）；③ 取值顺序与坏值
    兜底（显式 > env > 配置键 > 缺省；`"64MB"`/0/读不到 一律退回缺省）。
    """

    def _write_big_frame(self, sid, target_bytes, filler_size=64 << 10, ws="--I-app-big--"):
        """第一帧是会话头（给游标一个干净边界），第二帧是**流式**大帧。"""
        folder = self.sessions / ws / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        lines = [ev_user(2 + idx, "第%d条真人消息" % idx) for idx in range(3)]
        total, seq = 0, 10
        while total < target_bytes:
            lines.append(ev_filler(seq, filler_size))
            total += filler_size
            seq += 1
        path.write_bytes(frame(ev_session(sid)) + streamed_frame(lines))
        return path

    def test_default_cap_collects_a_9mib_frame(self):
        """默认上限（64 MiB）下，9 MiB 的帧必须采出来——旧默认 8 MiB 时这一条必红。"""
        sid = "b1b1b1b1-0000-0000-0000-00000000a101"
        path = self._write_big_frame(sid, 9 << 20)
        self.assertEqual(fish_corpus.dsh_adapter.DEFAULT_MAX_FRAME_BYTES, 64 << 20,
                         "默认上限不是 64 MiB（旧值 8 MiB 会把这批帧整帧丢掉）")
        result = self.ingest()
        self.assertEqual(result["degraded"], [], "9 MiB 的帧被判成超限了（上限没生效）")
        self.assertEqual(result["sessions_ok"], 1)
        self.assertEqual([r["text"] for r in self.segments(sid)],
                         ["第0条真人消息", "第1条真人消息", "第2条真人消息"])
        cursor = store.read_cursor(self.conn, "dsh", sid)
        self.assertEqual(cursor["status"], store.SOURCE_OK)
        self.assertEqual(int(cursor["bytes_total"]), path.stat().st_size,
                         "游标没走到文件尾：大帧没被完整采下")

    def test_env_override_lowers_the_cap_and_keeps_decoded_segments(self):
        """`FISH_CORPUS_MAX_FRAME_BYTES` 覆盖生效：同一份帧在上限 1 MiB 时降级。"""
        sid = "b1b1b1b1-0000-0000-0000-00000000a102"
        self._write_big_frame(sid, 9 << 20)
        restore = env_var(fish_corpus.dsh_adapter.ENV_MAX_FRAME_BYTES, 1 << 20)
        self.addCleanup(restore)
        result = self.ingest()
        codes = [code for item in result["degraded"] for code in item["reasons"]]
        self.assertEqual(codes, [store.REASON_OVERSIZE_FRAME],
                         "环境变量没覆盖上限（9 MiB 帧在 1 MiB 上限下仍算正常）")
        self.assertEqual([r["text"] for r in self.segments(sid)],
                         ["第0条真人消息", "第1条真人消息", "第2条真人消息"],
                         "上限内已解出的段被丢掉了（A③）")

    def test_cap_resolution_order_and_bad_values(self):
        """取值顺序：显式 > 环境变量 > 配置键 > 缺省；坏值一律退回缺省、绝不抛。"""
        resolve = fish_corpus.dsh_adapter.resolve_max_frame_bytes
        config = {"corpus.max_frame_bytes": 3 << 20}
        restore = env_var(fish_corpus.dsh_adapter.ENV_MAX_FRAME_BYTES, 2 << 20)
        try:
            self.assertEqual(resolve(lambda key: config.get(key), 5 << 20), 5 << 20,
                             "显式参数没压过环境变量")
            self.assertEqual(resolve(lambda key: config.get(key)), 2 << 20,
                             "环境变量没压过配置键")
        finally:
            restore()
        self.assertEqual(resolve(lambda key: config.get(key)), 3 << 20, "配置键没被读到")
        self.assertEqual(resolve(lambda key: None), 64 << 20, "没有配置时的缺省不对")

        def boom(key):
            raise KeyError(key)

        for bad in ("64MB", 0, -1, None):
            self.assertEqual(resolve(lambda key, _bad=bad: _bad), 64 << 20,
                             "坏值 %r 没退回缺省（0 会被下游当成'不限'）" % (bad,))
        self.assertEqual(resolve(boom), 64 << 20, "读配置抛异常时没退回缺省（采集会整轮失败）")


class OversizePartialKeepTest(CorpusCase):
    """A③（2026-10-27）：超过上限的帧**仍降级，但已解出的段必须落库**，且内存有界。

    旧实现是"整帧丢"：上限在解压产出侧检查，超限直接抛 `FrameError`，那一帧里已经
    解出来的段落库之前就被扔掉了。真实数据里 10+ 个这样的帧 = 多年对话的一部分永久缺失。

    判据三件事：① 段真的入库了（不是只在内存里）；② 台账 `segments_kept` 与段表一致
    （先写台账后 flush 会把真值写成 0）；③ 游标**不越过**超限帧（停在它之前，那帧剩余
    内容下次还能再试），④ tracemalloc 峰值 < 512 MiB（不随帧大小爆）。
    """

    def test_65mib_frame_keeps_decoded_segments(self):
        sid = "c2c2c2c2-0000-0000-0000-00000000a201"
        folder = self.sessions / "--I-app-oversize--" / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        lines = [ev_user(2 + idx, "超限帧里的第%d条" % idx) for idx in range(3)]
        total, seq = 0, 10
        while total < (65 << 20):
            lines.append(ev_filler(seq, 256 << 10))
            total += 256 << 10
            seq += 1
        header = frame(ev_session(sid))
        path.write_bytes(header + streamed_frame(lines))
        self.assertEqual(zstandard.frame_content_size(
            path.read_bytes()[len(header):len(header) + 18]), -1,
            "样本帧声明了内容大小：这条判据就测不到产出侧上限了")

        tracemalloc.start()
        try:
            result = fish_corpus.adapter_for("dsh").ingest_file(
                self.conn, path, None, tagger=fish_corpus.tag_mod.Tagger())
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        self.assertEqual(result["status"], store.SOURCE_DEGRADED)
        self.assertEqual([d["reason_code"] for d in result["degraded"]],
                         [store.REASON_OVERSIZE_FRAME])
        self.assertEqual([r["text"] for r in self.segments(sid)],
                         ["超限帧里的第0条", "超限帧里的第1条", "超限帧里的第2条"],
                         "超限帧整帧丢掉了：已解出的段没落库（A③）")
        degraded = store.list_degraded(self.conn, "dsh")
        self.assertEqual(len(degraded), 1)
        self.assertEqual(degraded[0]["reason_code"], store.REASON_OVERSIZE_FRAME)
        self.assertGreaterEqual(degraded[0]["segments_kept"], 3,
                                "台账说这个文件 0 段，而段其实入库了（先写台账后 flush）")
        self.assertGreaterEqual(result["degraded"][0]["segments_kept"], 3,
                                "返回里的 segments_kept 与台账/段表不一致")
        cursor = store.read_cursor(self.conn, "dsh", sid)
        self.assertEqual(int(cursor["bytes_total"]), len(header),
                         "游标越过了超限帧：那帧剩下的内容永远读不到")
        self.assertLess(peak, 512 << 20,
                        "解压峰值 %.1f MiB：内存随帧大小爆了（要按小块喂解压器）"
                        % (peak / 1048576.0))


class LegacyV0FormatTest(CorpusCase):
    """B（2026-10-27）：早期 `session.jsonl.zstd`（v0，文件名里没有 `vN`）也要采。

    只读取证（2026-10-27，17 个真实旧样本）：头部有 `"version": 0`，事件形状与 v4
    **同构**——`{"type","seq","time","data"}`、`user/message.data.source.kind` 白名单语义
    一致、`session/title` / `compact...` 一样。所以按同一套解析器处理，只在 `format`
    上记 `dsh.legacy`（对账时能区分"早期会话"与"当前格式"）。

    变异体：把 `dsh.legacy` 从 `SUPPORTED_FORMATS` 摘掉（回到"unsupported_format"）→ 本类必红。
    """

    SID = "session-2a66f559-4bb1-4139-b7b2-a23c7b6836ca"      # 形如真实目录名
    TITLE = "早期会话的标题"

    def _write_legacy(self):
        events = [
            {"type": "session", "version": 0, "id": self.SID, "createdAt": MS,
             "cwd": "I:/app/anyi-studio", "delegationDepth": 0, "agentPreset": "cordis"},
            # v0 多出来的会话配置事件：不认识就计数（unknown），绝不入库
            {"type": "permission/preset", "seq": 2, "time": MS, "data": {"preset": "x"}},
            ev_user(3, "早期会话里的真话", kind="user"),
            ev_user(4, "<skills>技能目录注入</skills>", kind="skill-catalog"),
            ev_user(5, "agent 指令注入", kind="agent-instructions"),
            ev_user(6, "插件注入", kind="plugin"),
            ev_assistant(7, ["早期回答"]),
            {"type": "session/title", "seq": 8, "time": MS + 8,
             "data": {"title": self.TITLE, "messageSeqs": [3],
                      "source": {"kind": "fallback"}}},
            # v0 的流式碎片（字段名是 seq0/time0）：skip 表里，不许掉进 unknown
            {"type": "reasoning-chunks", "seq0": 9, "time0": MS + 9,
             "data": {"texts": ["碎片"]}},
        ]
        return self.write_session(self.SID, events, name="session.jsonl.zstd")

    def test_legacy_v0_segments_title_and_format(self):
        self._write_legacy()
        self.assertIn(store.FORMAT_DSH_LEGACY, fish_corpus.dsh_adapter.SUPPORTED_FORMATS,
                      "dsh.legacy 不在支持列表里（早期会话会被登记成 unsupported_format）")
        result = self.ingest()
        self.assertEqual(result["degraded"], [],
                         "早期格式被降级了：%s" % (result["degraded"],))
        self.assertEqual(result["sessions_ok"], 1)
        self.assertEqual([(r["role"], r["loc"], r["text"]) for r in self.segments(self.SID)],
                         [("user", "seq=3", "早期会话里的真话"),
                          ("assistant", "seq=7", "早期回答")],
                         "注入块/碎片被采进来了，或真话没采到")
        self.assertEqual(self.session_row(self.SID)["title"], self.TITLE)
        cursor = store.read_cursor(self.conn, "dsh", self.SID)
        self.assertEqual(cursor["format"], store.FORMAT_DSH_LEGACY,
                         "format 没记成 dsh.legacy（对账分不出早期会话）")
        self.assertEqual(cursor["status"], store.SOURCE_OK)
        # v0 多出来的会话配置事件：只计数、不入库
        self.assertGreaterEqual(result["unknown_events"], 1)
        stats = self.event_stats(self.SID)
        self.assertEqual(stats["permission/preset"]["skipped"], 1)
        self.assertEqual(stats["reasoning-chunks"]["skipped"], 1,
                         "v0 的流式碎片掉进 unknown 了（skip 表没认 seq0 形态）")

    def test_legacy_injection_blocks_are_never_stored(self):
        self._write_legacy()
        self.ingest()
        joined = " ".join(r["text"] for r in self.segments(self.SID))
        for token in ("技能目录注入", "agent 指令注入", "插件注入", "碎片"):
            self.assertNotIn(token, joined, "早期格式的注入块被采进来了：%s" % token)


class WindowTest(CorpusCase):
    """采集时间窗口（2026-10-27 用户决策：**只投影最近 3 天**，更早留待后续回填）。

    为什么要有这一层过滤：全量投影等于把几个月的历史会话一次性压进检索面，
    而"最近 3 天"才是日常真正会用到的语料；更早的留待专门的回填动作（不是丢掉）。

    判据（都是"坏输入必须红"）：
      ① `days=3` → 只采 2 天前的文件；4 天前的、以及**恰好 3 天前再加 2 分钟**的
         （略早于 cutoff）都跳过；
      ② `days=0` / `days=None` → 全采（显式全量）；
      ③ 被窗口跳过的**不建 `corpus_source` 行、不记 orphan**，只在 `skipped_by_window` 计数；
      ④ `stats` 交叉读数：`window_days` 与 `skipped_by_window` 与 ingest 回执一致。

    变异体（`mutation_corpus_3gate.W1/W2`）：mtime 比较改成恒 True（全采）→ ①③ 红；
    恒 False（全丢）→ ① 红。判据不钉在"数字好不好看"上，而是钉在"哪几个会话进了库"。
    """

    DAY = 86400

    def _fixture(self):
        """三个会话文件（mtime 用 `os.utime` 钉死）：4 天前 / 3 天前+2 分钟 / 2 天前。"""
        now = time.time()
        spec = (("win-old", 4 * self.DAY, "四天前的原话"),
                ("win-edge", 3 * self.DAY + 120, "刚过窗口的原话"),
                ("win-fresh", 2 * self.DAY, "两天前的原话"))
        for sid, age, text in spec:
            path = self.write_session(sid, [ev_session(sid), ev_user(9, text)])
            ts = now - age
            os.utime(path, (ts, ts))          # ← 唯一的窗口输入：文件 mtime
        return [sid for sid, _age, _text in spec]

    def _source_ids(self):
        rows = self.conn.execute(
            "SELECT session_id FROM corpus_source WHERE host='dsh'"
            " ORDER BY session_id").fetchall()
        return [row["session_id"] for row in rows]

    def test_only_files_inside_the_window_are_ingested(self):
        self._fixture()
        result = self.ingest(days=3)
        self.assertEqual(result["window_days"], 3)
        self.assertEqual(result["skipped_by_window"], 2,
                         "4 天前与刚过窗的文件没被计数（跳过是静默的吗？）")
        self.assertEqual(result["sessions_seen"], 1,
                         "sessions_seen 应是**进入管线**的文件数，窗口外的不算")
        self.assertEqual(self._source_ids(), ["win-fresh"],
                         "窗口外的文件建了 corpus_source 行（等于假装采过）")
        self.assertEqual(result["orphan"], [],
                         "窗口跳过的文件被当成孤儿了（其中道理：它从没进过管线）")
        self.assertEqual([r["text"] for r in self.segments("win-fresh")], ["两天前的原话"])
        for sid in ("win-old", "win-edge"):
            self.assertEqual(self.segments(sid), [], "%s 的原话被投影进来了" % sid)
            self.assertIsNone(self.session_row(sid), "%s 建了会话行" % sid)

    def test_days_zero_collects_everything(self):
        self._fixture()
        result = self.ingest(days=0)          # 显式 0 = 全量（回填时就是这么用的）
        self.assertEqual(result["window_days"], 0, "0 应当读成「不限」")
        self.assertEqual(result["skipped_by_window"], 0)
        self.assertEqual(result["sessions_seen"], 3)
        self.assertEqual(self._source_ids(), ["win-edge", "win-fresh", "win-old"])

    def test_days_none_collects_everything(self):
        self._fixture()
        result = self.ingest(days=None)       # 函数默认值：既有调用方的行为不变
        self.assertEqual(result["window_days"], 0)
        self.assertEqual(result["skipped_by_window"], 0)
        self.assertEqual(result["sessions_seen"], 3)

    def test_stats_surfaces_the_window_readings(self):
        self._fixture()
        result = self.ingest(days=3)
        payload = fish_corpus.stats(root=self.root, conn=self.conn)
        self.assertEqual(payload["window_days"], 3, "stats 看不到采集窗口（读数断链）")
        self.assertEqual(payload["skipped_by_window"], result["skipped_by_window"])
        self.assertEqual(payload["skipped_by_window"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
