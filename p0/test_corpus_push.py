#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C-P0-6 推送判据（T04 · 设计 §7）。

背景（为什么这批门槛这么硬）：`tool_after` 通道实测 **956 次推送 / 1 次采纳**——
不是"推得不够"，是"推得太松"。所以语料候选的契约是"少而准"：

| 判据 | 本文件 |
|---|---|
| 绿：造"历史讨论过 X" → ≥1 条候选、渲染行 ≤120 字、含门牌、`read` 后 accepted | `CandidateTest` / `AcceptanceTest` |
| 红①：24h 同 code 推两次 | `test_duplicate_within_24h_is_suppressed` |
| 红②：`score<=0` 仍推（M11） | `test_every_candidate_has_positive_score` |
| 红③：`citizen_id` 空仍推（M12） | `test_no_identity_no_push` |
| 前缀回填（M18） | `test_acceptance_is_exact_code_not_prefix` |

纪律：所有库都在 `tempfile` 根下（主库与语料库同级不同文件），**绝不碰 `p0/data/`**。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

import zstandard

P0 = Path(__file__).resolve().parent
if str(P0) not in sys.path:
    sys.path.insert(0, str(P0))
import fish  # noqa: E402
import fish_corpus  # noqa: E402
import fish_corpus_store as store  # noqa: E402

MS = 1790918381967
CID = "01CITIZEN00000000000PUSH"
PROJECT = "PJ"
QUERY = "撞车判据"
LONG = ("撞车判据的唯一真值是 overlap：先比目录前缀、再比文件后缀，同一份声明窗口内"
        "出现两条就算撞车。判定不看谁先签到，也不看谁嗓门大——这条结论在历史会话里"
        "已经反复确认过，长度足够过 min_chars。")
SHORT = "撞车判据就那样"


def frame(payload) -> bytes:
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return zstandard.ZstdCompressor().compress(blob + b"\n")


def ev_session(sid, cwd="I:/app/push-proj", **extra):
    data = {"type": "session", "version": 4, "id": sid, "createdAt": MS, "cwd": cwd}
    data.update(extra)
    return data


def ev_user(seq, text):
    return {"type": "user/message", "seq": seq, "time": MS + seq * 10,
            "data": {"content": [{"type": "text", "text": text}],
                     "source": {"kind": "user"}, "role": "user", "id": "u%d" % seq}}


def ev_assistant(seq, text):
    return {"type": "assistant/message", "seq": seq, "time": MS + seq * 10,
            "data": {"turn": 1, "step": 1,
                     "message": {"role": "assistant", "content": [
                         {"type": "reasoning", "text": "不该入库"},
                         {"type": "text", "text": text}], "id": "a%d" % seq}}}


class PushCase(unittest.TestCase):
    """主库 + 语料库都在临时根里；语料由真实 DSH 帧采集进来（不手插段表）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-push-"))
        self.root = self.tmp / "data"
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir(parents=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # 两个库两个连接：主库（push_log/记忆）与语料库（corpus.sqlite）**绝不混用**
        # ——把主库连接传给 ingest 就是"在错的库里找 corpus_source"。
        self.corpus_conn = store.open_db(self.root)
        self.sid = "3931d23f-17c9-4de8-87b0-d7b782e70cf0"
        self.session_key = "dsh:" + self.sid
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.corpus_conn.close()
            self.conn.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 采集
    def write_session(self, sid=None, events=None, ws="--I-app-push-proj--"):
        sid = sid or self.sid
        events = events or [ev_session(sid), ev_user(9, "撞车判据怎么做"),
                            ev_assistant(10, LONG)]
        folder = self.sessions / ws / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "session.v4.jsonl.zstd").write_bytes(
            b"".join(frame(ev) for ev in events))
        return folder / "session.v4.jsonl.zstd"

    def ingest(self, *, citizen_of=lambda key: CID, project_of=lambda cwd: PROJECT):
        return fish_corpus.ingest(("dsh",), root=self.root, conn=self.corpus_conn,
                                 roots={"dsh": [self.sessions]}, use_lock=False,
                                 project_of=project_of, citizen_of=citizen_of)

    # ---- 取候选
    def candidates(self, prompt=QUERY, project=PROJECT, session_key=None, timing="prompt"):
        return [c for c in fish.build_prompt_candidates(
            self.conn, self.root, prompt=prompt, citizen_id=CID,
            session_key=self.session_key if session_key is None else session_key,
            project=project, limit=3) if c["code"].startswith("conv:")]

    @staticmethod
    def render(cand) -> str:
        """与 `cmd_prompt_push` 同一渲染法（判据必须量到"真正被递出去的那一行"）。"""
        return "- %s %s（%s）" % (cand["code"], cand["title"], cand["reason"])

    def push_row(self, code):
        row = self.conn.execute(
            "SELECT reaction FROM push_log WHERE code=? ORDER BY id DESC LIMIT 1",
            (code,)).fetchone()
        return None if row is None else row[0]


class CandidateTest(PushCase):
    """绿：历史讨论过 X → 有且只有 1 条、行短、含门牌。"""

    def test_candidate_is_produced(self):
        self.write_session()
        self.ingest()
        cands = self.candidates()
        self.assertGreaterEqual(len(cands), 1)
        self.assertLessEqual(len(cands), fish_corpus.PUSH_MAX_ITEMS)
        cand = cands[0]
        self.assertTrue(cand["code"].startswith("conv:dsh:3931d23f#seq="))
        self.assertEqual(cand["kind"], "conv_related")
        self.assertEqual(cand["project"], PROJECT)

    def test_rendered_line_is_within_budget_and_has_pointer(self):
        self.write_session()
        self.ingest()
        line = self.render(self.candidates()[0])
        self.assertLessEqual(len(line), 120, "推送行超出 120 字预算：%r" % line)
        self.assertIn("conv:dsh:3931d23f#", line)      # 门牌必须在
        self.assertIn("（", line)                       # 理由必须在

    def test_every_candidate_has_positive_score(self):
        """M11：候选不许绕过 `score > 0`（负分候选等于"明知没用还推"）。"""
        self.write_session()
        self.ingest()
        cands = self.candidates()
        self.assertTrue(cands)
        for cand in cands:
            self.assertGreater(cand["score"], 0, cand)
        # 2026-10-28：G4 默认挡语料；本判据测的是"候选分数与选择"，需显式开通道。
        self.conn.execute("INSERT OR REPLACE INTO config (key,value) "
                          "VALUES ('push.corpus_enabled','true')")
        self.conn.commit()
        self.assertEqual(fish.select_push_candidates(self.conn, cands, limit=1), cands[:1])

    def test_only_one_item_per_batch(self):
        sid_two = "7a1c0002-aaaa-4de8-87b0-000000000002"
        self.write_session()
        self.write_session(sid_two, [ev_session(sid_two), ev_user(9, "撞车判据再问一次"),
                                     ev_assistant(10, LONG + "（第二条）")])
        self.ingest()
        self.assertEqual(len(self.candidates()), 1, "单批只推 1 条（O9 的安全侧）")

    def test_inject_timing_has_no_conv(self):
        """设计 §7：语料只挂 prompt 时机（开场/工具后没有 query，推了就是噪音）。"""
        self.write_session()
        self.ingest()
        cands = fish.build_push_candidates(self.conn, self.root, citizen_id=CID,
                                          project=PROJECT, session_key=self.session_key,
                                          timing="inject")
        self.assertEqual([c for c in cands if c["code"].startswith("conv:")], [])

    def test_prompt_timing_of_build_push_candidates_includes_conv(self):
        self.write_session()
        self.ingest()
        cands = fish.build_push_candidates(self.conn, self.root, citizen_id=CID,
                                          project=PROJECT, session_key=self.session_key,
                                          timing="prompt", prompt=QUERY)
        self.assertTrue([c for c in cands if c["code"].startswith("conv:")])

    def test_no_query_no_candidate(self):
        self.write_session()
        self.ingest()
        self.assertEqual(self.candidates(prompt=""), [])

    def test_no_project_no_candidate(self):
        self.write_session()
        self.ingest()
        self.assertEqual(self.candidates(project=None), [])

    def test_duplicate_within_24h_is_suppressed(self):
        """C-P0-6 红①：同一个 code 24h 内推过就不再产。"""
        self.write_session()
        self.ingest()
        code = self.candidates()[0]["code"]
        fish.append_push_log(self.conn, [code], channel="prompt",
                             session_key=self.session_key, citizen_id=CID)
        self.assertEqual(self.candidates(), [])

    def test_short_segment_is_skipped_with_reason(self):
        sid = "0b5d0003-bbbb-4de8-87b0-000000000003"
        self.write_session(sid, [ev_session(sid), ev_user(1, "撞车判据"),
                                 ev_assistant(2, SHORT)])
        self.ingest()
        res = fish_corpus.conv_push_candidates(query=QUERY, project=PROJECT,
                                              root=self.root)
        self.assertEqual(res["items"], [])
        self.assertIn("too_short", res["skipped"])

    def test_no_identity_no_push(self):
        """C-P0-6 红③ / M12：`citizen_id` 查不到就**不推**（并且记账，不静默）。"""
        self.write_session()
        self.ingest(citizen_of=lambda key: None)
        self.assertEqual(self.candidates(), [])
        res = fish_corpus.conv_push_candidates(query=QUERY, project=PROJECT,
                                              root=self.root)
        self.assertEqual(res["items"], [])
        self.assertIn("no_citizen", res["skipped"])

    def test_subagent_not_pushed_by_default(self):
        """D11：子代理语料**可检索、默认不推**（开关可开）。"""
        sid = "5abc0004-cccc-4de8-87b0-000000000004"
        self.write_session(sid, [ev_session(sid, origin="subagent", parentSession=self.sid,
                                            delegationDepth=1),
                                 ev_user(1, "撞车判据（子代理任务正文）"),
                                 ev_assistant(2, LONG)], ws="--I-app-push-proj--")
        self.write_session()
        self.ingest()
        # 主会话的候选照常；子代理那条必须**一条都不出现**（同 sid8 会让门牌撞车，
        # 所以夹具里每个会话的前 8 位都不同）。
        codes = [cand["code"] for cand in self.candidates()]
        self.assertTrue(codes)
        self.assertFalse(any(code.startswith("conv:dsh:5abc0004#") for code in codes),
                         codes)
        res = fish_corpus.conv_push_candidates(query=QUERY, project=PROJECT,
                                              root=self.root, include_subagent=True,
                                              limit=5)
        self.assertTrue(any(item["session_id"] == sid for item in res["items"]),
                        res)


class AcceptanceTest(PushCase):
    """`read` 成功后按**精确 code** 回填 accepted（M18：前缀回填会让采纳率虚高）。"""

    def test_read_then_accept_backfills_the_code(self):
        self.write_session()
        self.ingest()
        cand = self.candidates()[0]
        fish.append_push_log(self.conn, [cand["code"]], channel="prompt",
                             session_key=self.session_key, citizen_id=CID)
        read = fish_corpus.read(cand["code"], root=self.root)
        self.assertIn("撞车判据", read["text"])
        fish.mark_push_accepted(self.conn, [read["code"]], session_key=self.session_key)
        self.assertEqual(self.push_row(cand["code"]), "accepted")

    def test_acceptance_is_exact_code_not_prefix(self):
        """M18：同会话里另一条 conv 推送**不得**被一起置 accepted。"""
        sid_two = "6ef00005-dddd-4de8-87b0-000000000005"
        self.write_session()
        self.write_session(sid_two, [ev_session(sid_two), ev_user(1, "撞车判据第二会话"),
                                     ev_assistant(2, LONG)])
        self.ingest()
        code_a = "conv:dsh:3931d23f#seq=10"
        code_b = "conv:dsh:6ef00005#seq=2"
        fish.append_push_log(self.conn, [code_a, code_b], channel="prompt",
                             session_key=self.session_key, citizen_id=CID)
        read = fish_corpus.read(code_a, root=self.root)
        fish.mark_push_accepted(self.conn, [read["code"]], session_key=self.session_key)
        self.assertEqual(self.push_row(code_a), "accepted")
        self.assertIsNone(self.push_row(code_b),
                          "前缀回填把无关推送也置了 accepted（K7 会虚高）")

    def test_blank_pointer_line_is_readable(self):
        """推送里给的门牌必须能**原样**读回（否则 C-P0-6 的闭环断在第一步）。"""
        self.write_session()
        self.ingest()
        for cand in self.candidates():
            read = fish_corpus.read(cand["code"], root=self.root)
            self.assertEqual(read["code"], cand["code"])

    def test_corpus_audit_not_polluted_by_push_search(self):
        """推送取数 `record=False`：不许把每次推送都算成一次"人搜了一次"。"""
        self.write_session()
        self.ingest()
        before = self.conn.execute("SELECT COUNT(*) FROM push_log").fetchone()[0]
        self.candidates()
        corpus_conn = store.open_db(self.root)
        try:
            rows = store.list_access_log(corpus_conn, limit=10)
        finally:
            corpus_conn.close()
        self.assertEqual([row for row in rows if row["action"] == "search"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM push_log").fetchone()[0],
                         before)


class ConvFailureTest(PushCase):
    """M3（2026-10-27 复审 🟠）：语料候选失败**不许**拖垮 prompt-push。

    这条发生在用户消息边界上：采集中（写锁被占）时一次 `database is locked` 就会让
    整批候选抛出去，用户那边看到的不是"少推一条"，而是这条链路整个坏掉。
    """

    def test_conv_error_does_not_break_prompt_candidates(self):
        from unittest import mock
        self.write_session()
        self.ingest()
        with mock.patch.object(fish_corpus, "conv_push_for_session",
                               side_effect=sqlite3.OperationalError("database is locked")):
            cands = fish.build_prompt_candidates(
                self.conn, self.root, prompt=QUERY, citizen_id=CID,
                session_key=self.session_key, project=PROJECT, limit=3)
        self.assertIsInstance(cands, list)      # 没抛出去
        self.assertEqual([c for c in cands if c["code"].startswith("conv:")], [])

    def test_conv_error_does_not_break_tool_after_candidates(self):
        from unittest import mock
        self.write_session()
        self.ingest()
        with mock.patch.object(fish_corpus, "conv_push_for_session",
                               side_effect=sqlite3.OperationalError("database is locked")):
            cands = fish.build_push_candidates(
                self.conn, self.root, citizen_id=CID, project=PROJECT,
                session_key=self.session_key, timing="tool_after")
        self.assertIsInstance(cands, list)
        self.assertEqual([c for c in cands if c["code"].startswith("conv:")], [])

    def test_read_path_uses_a_readonly_connection(self):
        """M3：`search`/`read` 走只读连接（读动作不该抢写锁、也不该跑 DDL）。"""
        from unittest import mock
        self.write_session()
        self.ingest()
        seen = {}
        real_open = store.open_db

        def spy(root, *, readonly=False, **kwargs):
            seen["readonly"] = readonly
            return real_open(root, readonly=readonly, **kwargs)

        with mock.patch.object(store, "open_db", side_effect=spy):
            fish_corpus.search("撞车", root=self.root)
        self.assertTrue(seen.get("readonly"), "读路径开了写连接（会抢写锁跑 DDL）")


class DailyReportStepTest(unittest.TestCase):
    """日报一步（设计 §4.3）：失败只落一行、busy 视为正常跳过——**绝不让日报假红**。

    2026-10-27 复审 🟠-1：这一步**默认不跑**（闸门在 `corpus_daily_enabled`），所以
    这批判据显式 `enabled=True` 测"步本体"；闸门本身由
    `test_corpus_contract.DailyCorpusGateTest` 判（默认不许扫真实日志）。
    """

    RESULT = {"busy": False, "sessions_seen": 3, "segments_new": 7, "degraded": [],
              "orphan": [], "lag_sec": 60}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-daily-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        import daily_report
        self.daily = daily_report

    def test_success_line_mentions_counts(self):
        from unittest import mock
        with mock.patch.object(fish_corpus, "ingest", return_value=dict(self.RESULT)):
            line = self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertIn("新增 7 段", line)
        self.assertIn("会话 3", line)

    def test_busy_is_reported_as_skip(self):
        from unittest import mock
        busy = dict(self.RESULT, busy=True, skipped="locked")
        with mock.patch.object(fish_corpus, "ingest", return_value=busy):
            line = self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertIn("跳过", line)

    def test_failure_is_one_line_and_never_raises(self):
        from unittest import mock
        with mock.patch.object(fish_corpus, "ingest", side_effect=RuntimeError("盘满了")):
            line = self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertIn("语料采集失败", line)
        self.assertIn("盘满了", line)
        self.assertEqual(line.count("\n"), 0)

    def test_lag_is_flagged(self):
        from unittest import mock
        stale = dict(self.RESULT, lag_sec=30 * 3600)
        with mock.patch.object(fish_corpus, "ingest", return_value=stale):
            line = self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertIn("滞后", line)

    def test_stalled_alarm_is_printed(self):
        """M4：停滞哨兵必须在**日报那一行**上出现（stats 里没人看得到）。"""
        from unittest import mock
        alarmed = dict(self.RESULT, segments_new=0,
                       stall={"zero_streak": 3, "alarm": "stalled"})
        with mock.patch.object(fish_corpus, "ingest", return_value=alarmed):
            line = self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertIn("停滞", line)
        self.assertIn("3 轮", line)

    def test_ingest_is_called_with_the_three_day_window(self):
        """生产入口必须显式传窗口：`ingest()` 的函数默认是 `None` = **不限**。

        判据钉在**实参**上：调用点直接写死 `3` 时，常量一改就静默漂成两种口径；
        而漏传 `days` 会把几个月的历史重新全量投影回来（这条路径每天都会跑）。
        """
        from unittest import mock
        with mock.patch.object(fish_corpus, "ingest",
                               return_value=dict(self.RESULT)) as patched:
            self.daily.corpus_ingest_step(self.tmp / "data", enabled=True)
        self.assertEqual(patched.call_args.kwargs.get("days"),
                         fish_corpus.DEFAULT_WINDOW_DAYS,
                         "日报步没引用 DEFAULT_WINDOW_DAYS（窗口口径会各写一份）")


class DailyReportWindowTest(unittest.TestCase):
    """日报语料步的窗口口径（2026-10-27 用户决策：**只投影最近 3 天**）。

    为什么这条判据必须在**日报这一步**上测：`ingest(days=3)` 本身已有窗口判据
    （`test_corpus_ingest_dsh.WindowTest`），但"日报没传 days"照样会全量采集——
    函数默认 `None`（不限）。所以这里**不打桩**，从 `corpus_ingest_step(enabled=True)`
    一路跑到真采集，判据钉在"哪几个会话进了库"上。
    """

    DAY = 86400

    def setUp(self):
        import daily_report
        self.daily = daily_report
        self.tmp = Path(tempfile.mkdtemp(prefix="fishcorpus-daily-window-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "data"
        self.root.mkdir(parents=True)
        self.sessions = self.tmp / "sessions"
        self.sessions.mkdir()
        self.wb = self.tmp / "wb"
        self.wb.mkdir()
        # 扫描根**必须**钉在临时目录：缺省根是真日志（515 个会话 + 4GB WorkBuddy）。
        self._env = {key: os.environ.get(key)
                     for key in (fish_corpus.ENV_DSH_SESSIONS, fish_corpus.ENV_WB_PROJECTS)}
        os.environ[fish_corpus.ENV_DSH_SESSIONS] = str(self.sessions)
        os.environ[fish_corpus.ENV_WB_PROJECTS] = str(self.wb)
        # 主库：日报的 root 同时是主库与语料库的根，project/citizen 解析要读它。
        conn = fish.connect(self.root)
        try:
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
        finally:
            conn.close()
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _write(self, sid, text, age_days):
        """一个 DSH 会话文件；`age_days` 用 `os.utime` 钉死——窗口唯一的输入是 mtime。"""
        folder = self.sessions / "--I-app-daily-window--" / sid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "session.v4.jsonl.zstd"
        path.write_bytes(b"".join(frame(ev)
                                  for ev in (ev_session(sid), ev_user(9, text))))
        ts = time.time() - age_days * self.DAY
        os.utime(path, (ts, ts))
        return path

    def _source_ids(self):
        conn = store.connect(store.db_path(self.root), readonly=True)
        try:
            rows = conn.execute(
                "SELECT session_id FROM corpus_source WHERE host='dsh'"
                " ORDER BY session_id").fetchall()
        finally:
            conn.close()
        return [row["session_id"] for row in rows]

    def test_default_window_is_three_days(self):
        """用户拍板的口径是 3 天；改这个数 = 改生产入口的语义，必须同步改判据。"""
        self.assertEqual(fish_corpus.DEFAULT_WINDOW_DAYS, 3)

    def test_step_only_ingests_the_last_three_days(self):
        self._write("win-old", "四天前的原话", 4)
        self._write("win-fresh", "两天前的原话", 2)
        line = self.daily.corpus_ingest_step(self.root, enabled=True)
        self.assertNotIn("失败", line, line)
        self.assertIn("新增 1 段", line)
        self.assertIn("会话 1", line)
        self.assertEqual(self._source_ids(), ["win-fresh"],
                         "日报步没按 3 天窗口采（4 天前的会话也进了库）")
        payload = fish_corpus.stats(root=self.root)
        self.assertEqual(payload["window_days"], fish_corpus.DEFAULT_WINDOW_DAYS,
                         "语料库里的窗口读数不是 3 天（日报步没传 days）")
        self.assertEqual(payload["skipped_by_window"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
