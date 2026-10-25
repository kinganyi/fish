#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EVAL 未闭合项修复的判据（2026-10-25）。

每条用例对应 fish 库里一条 EVAL 记录；判据必须能失败（变异自证见各条注释）。
批 A：EVAL-261025-17（add_project 顺序）、EVAL-261025-18（slug 截断标记）。
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fish  # noqa: E402


class SlugTitleEval18Test(unittest.TestCase):
    """EVAL-261025-18：长标题文件名静默截断 → 必须显式标省略号。"""

    def test_truncation_marked_with_ellipsis(self):
        long_title = "这是一条非常非常长的标题" * 5
        s = fish.slug_title(long_title)
        self.assertLessEqual(len(s), fish._SLUG_MAX, f"超出上限：{len(s)}")
        self.assertTrue(s.endswith("..."), f"截断未标记：{s!r}")

    def test_short_title_untouched(self):
        s = fish.slug_title("短标题")
        self.assertEqual(s, "短标题")
        self.assertFalse(s.endswith("..."))

    def test_truncation_respects_limit_below_4(self):
        """QA 2026-10-25 边界：limit < 4 时放不下省略号，退回纯截断但**仍守总长上限**。"""
        s = fish.slug_title("这是一条很长的标题", limit=3)
        self.assertLessEqual(len(s), 3, f"limit=3 超长：{s!r}")

    def test_record_memory_title_intact(self):
        """文件名截断不影响 title 字段（EVAL-18 的痛点：像数据损坏）。"""
        tmp = tempfile.mkdtemp(prefix="fish-eval18-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = Path(tmp) / "data"
        root.mkdir(parents=True, exist_ok=True)
        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        cid = "01CIT00000000000000000001"
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, "测试公民", "resident", "owner", None, "active", fish.now_utc()))
        conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", ("TESTP", "测试项目", "", 0, "active"))
        conn.commit()
        long_title = "超长标题" * 10
        code, _rel = fish.record_memory(conn, root, project="TESTP", title=long_title,
                                        body="正文", owner=cid)
        row = conn.execute("SELECT title, file_path FROM code_index WHERE code=?",
                           (code,)).fetchone()
        self.assertEqual(row[0], long_title, "title 必须完整")
        fp = root / row[1]
        self.assertTrue(fp.exists(), f"正文文件不存在：{fp}")
        self.assertIn("...", fp.name, "文件名应带截断标记")
        conn.close()


class AddProjectEval17Test(unittest.TestCase):
    """EVAL-261025-17：已存在且正在使用的前缀（可能含 -）必须先报"已存在"。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-eval17-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = Path(self.tmp) / "data"
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", ("anyi-studio", "架构工作区", "", 0, "active"))
        self.conn.commit()

    def test_cli_existing_dash_prefix_reports_exists(self):
        args = argparse.Namespace(root=str(self.root), prefix="anyi-studio", name="x")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fish.cmd_project_add(args)
        self.assertIn("项目已存在", out.getvalue(),
                      f"stdout={out.getvalue()!r} stderr={err.getvalue()!r}")
        self.assertNotIn("不能含", err.getvalue(), "存量前缀不应报字符错误")

    def test_cli_new_dash_prefix_rejected(self):
        args = argparse.Namespace(root=str(self.root), prefix="new-bad-prefix", name="x")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            fish.cmd_project_add(args)
        self.assertIn("不能含", err.getvalue())
        n = self.conn.execute(
            "SELECT COUNT(*) FROM code_registry WHERE prefix=?",
            ("new-bad-prefix",)).fetchone()[0]
        self.assertEqual(n, 0, "非法前缀不得落库")

    def tearDown(self):
        self.conn.close()


class AddProjectMcpEval17Test(unittest.TestCase):
    """EVAL-261025-17 的 MCP 面（原 bug 就在这一面）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-eval17m-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "data")
        os.makedirs(self.root, exist_ok=True)
        conn = fish.connect(Path(self.root))
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", ("anyi-studio", "架构工作区", "", 0, "active"))
        conn.commit()
        conn.close()
        from test_mcp import MCPClient
        self.c = MCPClient(self.root)
        self.c.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})

    def test_mcp_existing_dash_prefix_reports_exists(self):
        r = self.c.request("tools/call", {
            "name": "add_project",
            "arguments": {"prefix": "anyi-studio", "namespace": "x"},
        })
        text = r["result"]["content"][0]["text"]
        self.assertFalse(r["result"]["isError"], f"MCP 报错：{text}")
        self.assertIn("前缀已存在", text, f"应为已存在而非字符错误：{text}")

    def test_mcp_new_dash_prefix_rejected(self):
        r = self.c.request("tools/call", {
            "name": "add_project",
            "arguments": {"prefix": "brand-new-dash", "namespace": "x"},
        })
        text = r["result"]["content"][0]["text"]
        self.assertTrue(r["result"]["isError"], f"新前缀含 - 应被拒：{text}")
        self.assertIn("不能含", text)

    def tearDown(self):
        self.c.close()


class SessionHookIdentityEval19Test(unittest.TestCase):
    """EVAL-261025-19：WorkBuddy 侧 hook 不得硬编码身份（workbuddy-main）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-eval19-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = Path(self.tmp) / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self.cid = "01CIT00000000000000000002"
        conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "会话公民甲", "visitor", "session", "codebuddy", "active",
             fish.now_utc()))
        fish.issue_session_token(conn, citizen_id=self.cid,
                                 session_key="codebuddy:test-session-19",
                                 evidence="machine", channel="hook")
        conn.commit()
        conn.close()

    def _run_hook(self, payload):
        import subprocess
        env = {**os.environ, "FISH_DATA": str(self.data), "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, str(HERE / "fish_session_hook.py")],
            input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", env=env, timeout=60)

    def test_hook_uses_session_citizen_not_hardcoded(self):
        p = self._run_hook({"session_id": "test-session-19"})
        self.assertEqual(p.returncode, 0, p.stderr[:500])
        out = p.stdout.strip()
        self.assertTrue(out.isascii(), f"stdout 非 ASCII：{out[:200]}")
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("会话公民甲", ctx)
        self.assertNotIn("workbuddy-main", ctx)

    def test_hook_unknown_session_no_fake_identity(self):
        p = self._run_hook({"session_id": "unknown-session-19"})
        self.assertEqual(p.returncode, 0, p.stderr[:500])
        ctx = json.loads(p.stdout.strip())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("注入失败", ctx)          # 诚实降级
        self.assertNotIn("workbuddy-main", ctx)


class OwnerFixEval21Test(unittest.TestCase):
    """EVAL-261025-21：归属错了必须能修正（追加事件 + 更新投影）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-eval21-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = Path(self.tmp) / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", ("TESTP", "测试项目", "", 0, "active"))
        self.cid = "01CIT00000000000000000003"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "正确公民", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.commit()
        self.code, _ = fish.record_memory(
            self.conn, self.data, project="TESTP", title="归属错误的记忆",
            body="正文", owner="unattributed")
        self.conn.commit()

    def test_fix_owner_updates_and_appends_event(self):
        res = fish.fix_owner(self.conn, self.code, self.cid, reason="补正")
        self.assertEqual(res["old_owner"], "unattributed")
        row = self.conn.execute("SELECT owner FROM code_index WHERE code=?",
                                (self.code,)).fetchone()
        self.assertEqual(row[0], self.cid)
        ev = self.conn.execute(
            "SELECT payload FROM events WHERE type='memory.owner_fixed' "
            "ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertIsNotNone(ev, "必须追加 memory.owner_fixed 事件")
        payload = json.loads(ev[0])
        self.assertEqual(payload["old_owner"], "unattributed")
        self.assertEqual(payload["new_owner"], self.cid)

    def test_fix_owner_rejects_unknown(self):
        with self.assertRaises(ValueError):
            fish.fix_owner(self.conn, "TESTP-999999-99", self.cid)
        with self.assertRaises(ValueError):
            fish.fix_owner(self.conn, self.code, "01NOPE")

    def test_fix_owner_survives_rebuild(self):
        """QA 2026-10-25 发现：修正必须能被 rebuild 重放，否则重建即静默回退。"""
        fish.fix_owner(self.conn, self.code, self.cid, reason="补正")
        rep = fish.rebuild_projection(self.conn, self.data)
        row = self.conn.execute("SELECT owner FROM code_index WHERE code=?",
                                (self.code,)).fetchone()
        self.assertIsNotNone(row, "rebuild 后条目消失")
        self.assertEqual(row[0], self.cid, "rebuild 把归属修正回退了")
        self.assertFalse(
            [m for m in rep["mismatch"] if self.code in m and "owner" in m],
            f"修正后 rebuild 不应报 owner mismatch：{rep['mismatch']}")
        self.assertGreaterEqual(rep.get("owner_fixed_applied", 0), 1)

    def test_fix_owner_skipped_when_target_gone(self):
        """目标公民在 rebuild 时已不存在 → 跳过并留痕，不写幽灵归属（QA 判据 S1）。"""
        # 先补一个"其他公民"：否则 citizens 表为空 → rebuild 按既有设计**空库不做归一**
        # （不凭猜测改写归属），这条判据就失去判别力——空库无法区分"公民不存在"
        # 与"库没有公民知识"。
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("01CIT00000000000000000009", "其他公民", "resident", "owner", None,
             "active", fish.now_utc()))
        self.conn.commit()
        fish.fix_owner(self.conn, self.code, self.cid, reason="补正")
        self.conn.execute("DELETE FROM citizens WHERE citizen_id=?", (self.cid,))
        self.conn.commit()
        rep = fish.rebuild_projection(self.conn, self.data)
        self.assertTrue(rep.get("owner_fixed_skipped"), "跳过必须留痕，不能静默")
        self.assertEqual(rep.get("owner_fixed_applied", 0), 0)

    def test_rebuild_event_wins_over_dirty_projection(self):
        """投影被改脏 → 事件（memory.owner_fixed）胜出（QA 判据 S2）。"""
        fish.fix_owner(self.conn, self.code, self.cid, reason="补正")
        self.conn.execute("UPDATE code_index SET owner='drifted' WHERE code=?",
                          (self.code,))
        self.conn.commit()
        fish.rebuild_projection(self.conn, self.data)
        row = self.conn.execute("SELECT owner FROM code_index WHERE code=?",
                                (self.code,)).fetchone()
        self.assertEqual(row[0], self.cid, "事件应胜出")

    def tearDown(self):
        self.conn.close()


class BoardChangePushEval20Test(unittest.TestCase):
    """EVAL-261025-20：看到之后看板有新动静，必须能出推送候选。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fish-eval20-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = Path(self.tmp) / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,?,?,?)", ("P1", "项目一", "", 0, "active"))
        self.cid = "01CIT00000000000000000004"
        self.other = "01CIT00000000000000000005"
        for cid, name in ((self.cid, "本会话"), (self.other, "别的会话")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (cid, name, "visitor", "session", "dsh", "active", fish.now_utc()))
        self.conn.commit()
        fish.board_checkin(self.conn, self.other, doing="别人的任务",
                           scope="project", scope_key="P1")
        self.conn.commit()

    def _board_cands(self):
        cands = fish.build_push_candidates(
            self.conn, self.data, citizen_id=self.cid, project="P1")
        return [c for c in cands if str(c["code"]).startswith("board:")]

    def test_board_change_produces_candidate(self):
        # 1) 先"看到"一次（记录快照）
        fish.board_block(self.conn, "P1", citizen_id=self.cid)
        self.assertEqual(self._board_cands(), [], "刚看过不应有看板候选")
        # 2) 别人发一条新动态 → 出候选
        b = fish.board_ensure(self.conn, "project", "P1")
        fish.board_post(self.conn, b, self.other, "progress", "新进展")
        self.conn.commit()
        cands = self._board_cands()
        self.assertTrue(cands, "看板有新动态必须出候选")
        self.assertIn("新动态", cands[0]["reason"])
        # 3) 再看一次（刷新快照）→ 候选消失
        fish.board_block(self.conn, "P1", citizen_id=self.cid)
        self.assertEqual(self._board_cands(), [], "刷新快照后不应再推")

    def tearDown(self):
        self.conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
