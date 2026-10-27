#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撞车自撞修复 · T02 判据（入口与采集层：把 `session_key` 从 token 一路传到底）。

## 来源

设计：`45-撞车自撞修复-设计.md` §1.3（数据流）+ §2.3（写链路）+ §7.1。
这一层的问题不是"判定错"，而是**信息在入口处就丢了**：手工签到不传 `agent_id`、
MCP 的 `board checkin` 不传会话键 → 库里那一行的会话归属永远是 `''` →
下游所有"排除自己"的判定都失效。

## 判据清单

  S1  同一公民两会话各一行（`doing` 互不覆盖、`sessions_of_citizen`=2）
  S21 MCP 与 CLI 一致：同一会话键经两条通道各签一次 → **同一行**；不同键 → 两行；
      `refresh_presence_cache` 只更新**自己那一行**的 `auto_*`
  另：三条真实链路（MCP checkin / CLI checkin / 自动签到）都能把会话键写进列；
      无会话键时字段是 `''`（**不编**）
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402

spec = importlib.util.spec_from_file_location("fish_cli", HERE / "fish.py")
fish_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish_cli)

import mcp_server  # noqa: E402

CID_A = "01SESSIONROWS00000000A1"
CID_B = "01SESSIONROWS00000000B2"


class SessionRowsBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-session-rows-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        for cid, name in ((CID_A, "AI-甲"), (CID_B, "AI-乙")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,host)"
                " VALUES (?,?,'resident','active',?,'dsh')",
                (cid, name, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _checkin(self, cid, session_key, doing, surface=""):
        return fish.board_checkin(self.conn, cid, doing, surface=surface,
                                  scope="project", scope_key="PJ",
                                  session_key=session_key)

    def _run_cli(self, *argv):
        """真跑 CLI（`fish.py --root <tmp> …`）：这条通道没有 token 上下文。"""
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, str(HERE / "fish.py"), "--root", str(self.root), *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"CLI 失败（{' '.join(argv)}）：{proc.stderr[:500]}")
        return proc.stdout

    def _show_json(self):
        return json.loads(self._run_cli("board", "show", "--project", "PJ", "--json"))

    def _ident(self, srv, name):
        """按 `session_name` 签一条凭证 → 解析出身份（会话键 = `self:<name>`）。"""
        drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        clean = {k: v for k, v in os.environ.items() if k not in drop}
        with mock.patch.dict(os.environ, clean, clear=True):
            out = srv.tool_session_start({"session_name": name}, srv._resolve_identity(None))
        token = out.split("token: ", 1)[1].split("\n", 1)[0].strip()
        return srv._resolve_identity(token)


class TwoSessionsSameCitizenTest(SessionRowsBase):
    """S1：同公民多会话各占一行，`doing` 不再互相覆盖（设计 P1 的现场）。"""

    def test_S1_two_sessions_two_rows(self):
        board = self._checkin(CID_A, "dsh:aaa1111", "改 fish 判定", "p0/fish.py")
        self._checkin(CID_A, "dsh:bbb2222", "改看板渲染", "p0/board.py")
        rows = fish.board_presence_rows(self.conn, board)
        self.assertEqual(len(rows), 2, f"同公民两个会话必须两行：{rows}")
        by_key = {r["session_key"]: r for r in rows}
        self.assertEqual(by_key["dsh:aaa1111"]["doing"], "改 fish 判定")
        self.assertEqual(by_key["dsh:bbb2222"]["doing"], "改看板渲染")
        self.assertEqual(by_key["dsh:aaa1111"]["session_key_source"], "column",
                         "有列值时来源必须记 column")
        self.assertTrue(all(r["sessions_of_citizen"] == 2 for r in rows))
        self.assertTrue(all("·dsh#" in r["label"] for r in rows),
                        f"同公民多会话的标签要带会话短标识：{[r['label'] for r in rows]}")

    def test_S1_auto_row_carries_session_key(self):
        """自动签到同样要写会话键（否则"自动行"落在 `''` 上，签到的仍是同一个坑）。"""
        res = fish.board_auto_checkin(self.conn, CID_A, project="PJ",
                                      session_key="dsh:ccc3333")
        self.assertTrue(res["created"], f"自动行没建出来：{res}")
        row = self.conn.execute(
            "SELECT session_key, agent_id, doing FROM presence WHERE citizen_id=?",
            (CID_A,)).fetchone()
        self.assertEqual(row[0], "dsh:ccc3333")
        self.assertEqual(row[1], "dsh:ccc3333", "agent_id 同步带上（外部读者一直按它读）")
        self.assertIn("自动登记", row[2])


class EntryChannelTest(SessionRowsBase):
    """S21：三条真实链路 + 缓存分行。"""

    def test_S21_cli_checkin_session_key(self):
        self._run_cli("board", "checkin", "--project", "PJ", "--citizen", CID_A,
                      "--doing", "第一个窗口", "--session-key", "dsh:aaa1111")
        self._run_cli("board", "checkin", "--project", "PJ", "--citizen", CID_A,
                      "--doing", "第二个窗口", "--session-key", "dsh:bbb2222")
        payload = self._show_json()
        keys = sorted(p["session_key"] for p in payload["presence"])
        self.assertEqual(keys, ["dsh:aaa1111", "dsh:bbb2222"],
                         f"CLI 的 --session-key 必须落到列里：{payload['presence']}")
        self.assertEqual(sorted(p["doing"] for p in payload["presence"]),
                         ["第一个窗口", "第二个窗口"])

    def test_S21_cli_without_session_key_writes_blank(self):
        self._run_cli("board", "checkin", "--project", "PJ", "--citizen", CID_A,
                      "--doing", "无会话签一次")
        payload = self._show_json()
        self.assertEqual(len(payload["presence"]), 1)
        self.assertEqual(payload["presence"][0]["session_key"], "",
                         "没有会话键就是 ''——不许编一个")
        self.assertIsNone(payload["presence"][0]["agent_id"],
                          "`--agent` 没给、也没有会话键 → agent_id 保持 NULL（不编）")

    def test_S21_mcp_and_cli_land_on_the_same_row(self):
        srv = mcp_server.FishServer(self.root)
        try:
            ident = self._ident(srv, "rows-a")
            self.assertTrue(ident.session_key, "MCP 身份必须有会话键")
            srv.tool_board({"action": "checkin", "doing": "MCP 签到", "project": "PJ"},
                           ident)
            self._run_cli("board", "checkin", "--project", "PJ",
                          "--citizen", ident.citizen_id, "--doing", "CLI 签到",
                          "--session-key", ident.session_key)
            payload = self._show_json()
            self.assertEqual(len(payload["presence"]), 1,
                             f"同一会话键经两条通道必须落同一行：{payload['presence']}")
            self.assertEqual(payload["presence"][0]["session_key"], ident.session_key)
            self.assertEqual(payload["presence"][0]["doing"], "CLI 签到",
                             "同一行 → 后到的 doing 覆盖前一句（这是同一会话的意图更新）")
        finally:
            srv.conn.close()

    def test_S21_refresh_cache_only_touches_own_row(self):
        board = self._checkin(CID_A, "dsh:aaa1111", "第一个窗口")
        self._checkin(CID_A, "dsh:bbb2222", "第二个窗口")
        seen_at = fish.now_utc()
        wrote = fish_activity.refresh_presence_cache(
            self.conn, citizen_id=CID_A, project="PJ", dirs=["p0"],
            seen_at=seen_at, session_key="dsh:aaa1111")
        self.assertTrue(wrote, "第一次刷新应当写进去（没有缓存）")
        got = dict(self.conn.execute(
            "SELECT session_key, auto_surface FROM presence WHERE board_id=?",
            (board["id"],)))
        self.assertEqual(got["dsh:aaa1111"], "p0")
        self.assertIn(got["dsh:bbb2222"], (None, ""),
                      "同公民另一个会话的 auto_* 不许被覆盖（P5）")

    def test_refresh_cache_blank_key_hits_legacy_row(self):
        """不带会话键的旧调用点：命中 `''` 行，行为与今天一致（既有判据靠这条保绿）。"""
        board = self._checkin(CID_A, "", "无会话")
        self.assertTrue(fish_activity.refresh_presence_cache(
            self.conn, citizen_id=CID_A, project="PJ", dirs=["p0/fish"],
            seen_at=fish.now_utc()))
        got = self.conn.execute(
            "SELECT auto_surface FROM presence WHERE board_id=? AND session_key=''",
            (board["id"],)).fetchone()[0]
        self.assertEqual(got, "p0/fish")


class HookChainTest(SessionRowsBase):
    """WorkBuddy 开场那条链：hook 把 `codebuddy:<sid>` 传给 `inject --auto-presence`。"""

    def test_inject_auto_presence_writes_session_key(self):
        out = self._run_cli("inject", "--citizen", CID_A, "--project", "PJ",
                            "--json", "--auto-presence",
                            "--session-key", "codebuddy:hook-session-1")
        self.assertTrue(json.loads(out)["pack"], "注入包不许为空")
        row = self.conn.execute(
            "SELECT session_key FROM presence WHERE citizen_id=?", (CID_A,)).fetchone()
        self.assertIsNotNone(row, "`--auto-presence` 必须真的签到")
        self.assertEqual(row[0], "codebuddy:hook-session-1",
                         "hook 侧的会话键必须一路写进列")

    def test_session_start_auto_checkin_writes_session_key(self):
        """`session start` 自带会话键（这是 CLI 侧唯一自动拿到键的路径）。

        会话键的具体形态取决于环境（本机有宿主 session id 时是 `dsh:<id>`，
        干净环境里是 `self:<alias>`），所以这里**从回执里读**它再比对——把断言锚在
        "回执说的键 == 库里那一行的键"，而不是锚在一个会随环境变的字面量上。
        """
        out = self._run_cli("session", "start", "--project", "PJ", "--alias", "rows-hook")
        self.assertIn("session_key:", out, f"回执里应有会话键：{out[:400]}")
        key = out.split("session_key:", 1)[1].split("\n", 1)[0].strip()
        self.assertTrue(key, f"回执里应有会话键：{out[:400]}")
        row = self.conn.execute(
            "SELECT session_key FROM presence WHERE session_key=?", (key,)).fetchone()
        self.assertIsNotNone(row, f"`session start` 必须自动登记带会话键的一行：{key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
