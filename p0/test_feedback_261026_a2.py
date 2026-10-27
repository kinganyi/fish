#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反馈修复 A2（2026-10-26）：FB-05 / 06 / 08 / 11 / 12 / 13 的判据。

六条分两类，判据也分两类：

  · **契约/文案类**（FB-05 前缀、FB-06 看板文案、FB-08 同名提示、FB-13 入口行）
    → 判据读**回执文本**，因为缺的就是"说给人听的那句话"；
  · **行为类**（FB-11 替代入口、FB-12 公民归档）
    → 判据读**库内状态**（status / 冷热表 / 事件条数），文案只是附带。

每条都配了变异目标（改哪一行会让它红），变异自证见交付报告。

## 最小运行（**不要**跑全量：603 条归 QA）

    & "C:\\Users\\15596\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" `
      -m unittest test_feedback_261026_a2 -v

必须在 `fish-fix/p0` 目录下跑（`fish.py` 按相对路径加载，与其他测试一致）。
"""
import argparse
import contextlib
import datetime
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

import daily_report      # noqa: E402
import fish_activity     # noqa: E402
import install_cli       # noqa: E402
import mcp_server        # noqa: E402


def _iso_days_ago(days):
    """真实时钟往前推 N 天的 ISO 串（`daily_report` 那条用真 now，所以不能用死时间）。"""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Case(unittest.TestCase):
    """共用夹具：一个 tempfile 根 + 一台 FishServer（测试产物只在临时目录里）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.srv = mcp_server.FishServer(self.root)
        self.conn = self.srv.conn
        self.anon = self.srv._resolve_identity(None)
        # 真实环境里宿主会 scrub 掉会话 env；测试进程带着它们，不清会让
        # "两个不同 session_name = 两个会话"这个前提失效（同 A1 夹具的理由）。
        drop = {v for _h, v in fish.HOST_SESSION_ENVS} | set(fish.SESSION_ALIAS_ENVS)
        self.clean_env = {k: v for k, v in os.environ.items() if k not in drop}

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------ 造数据的小工具
    def _project(self, prefix="UU"):
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES (?,?,NULL,0,'active')", (prefix, prefix))
        self.conn.commit()
        return prefix

    def _citizen(self, name, *, kind="visitor", host="cli", last_seen=None,
                 owner=0, created="2026-10-01T00:00:00Z"):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
            "created_at,is_owner,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, name, kind, None, host, "active", created, int(owner), last_seen))
        self.conn.commit()
        return cid

    def _token(self, cid, *, session_key="self:x", revoked_at=None, expires_at=None):
        self.conn.execute(
            "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
            "channel,issued_at,expires_at,revoked_at) VALUES (?,?,?,?,?,?,?,?)",
            (fish.ulid(), cid, session_key, "self-reported", "cli",
             "2026-10-01T00:00:00Z", expires_at, revoked_at))
        self.conn.commit()

    def _stint(self, cid, project="UU"):
        self.conn.execute(
            "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "
            "VALUES (?,?,?,?,?)", (cid, None, project, "2026-10-01T00:00:00Z", "test"))
        self.conn.commit()

    def _archived_events(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='citizen.archived'").fetchone()[0]


# ============================================================== FB-05 前缀不叠加
class FB05SessionPrefixTest(_Case):
    """FB-05：入参已是 `self:` / 宿主前缀时**不得再拼一层**。

    生产库实锤是 `self:self:fish外部评估`——同一个会话两条键，身份直接分裂。
    """

    def test_FB05_known_prefixes_are_stripped(self):
        """`self:abc` / `dsh:abc` / 历史脏值 `self:self:abc` → 全部等于裸名 `abc`。"""
        base = fish.build_session_identity(alias="abc", environ={})
        self.assertEqual(base["session_key"], "self:abc", "裸名的既有行为不能变")
        for alias in ("self:abc", "dsh:abc", "codebuddy:abc", "zcode:abc",
                      "self:self:abc", "self:dsh:abc"):
            got = fish.build_session_identity(alias=alias, environ={})
            self.assertEqual(got["session_key"], base["session_key"],
                             f"{alias!r} 不该再拼一层：{got}")
            self.assertEqual(got["alias"], "abc", f"{alias!r} 的别名应归一成裸名")

    def test_FB05_env_carried_prefix_also_stripped(self):
        """env 里的自报名也走同一套（历史环境的残留值同样会长出 `self:self:`）。"""
        got = fish.build_session_identity(environ={"FISH_SESSION_ACTOR": "self:abc"})
        self.assertEqual(got["session_key"], "self:abc")

    def test_FB05_prefix_only_is_not_stripped_to_empty(self):
        """纯前缀不剥（`self:` 剥成空串 = 把"给了名字"变成"没给"，那是另一条分支）。"""
        self.assertEqual(fish.strip_session_name_prefixes("self:"), "self:")
        got = fish.build_session_identity(alias="self:", environ={})
        self.assertEqual(got["session_key"], "self:self:")

    def test_FB05_start_session_no_double_prefix(self):
        """端到端：`session start --alias self:fish外部评估` 落库的键不叠加。"""
        res = fish.start_session(self.conn, alias="self:fish外部评估", environ={})
        self.assertEqual(res["session_key"], "self:fish外部评估")
        self.assertEqual(res["display_name"], "fish外部评估",
                         "显示名也不该带前缀（名字是给人看的）")
        keys = [r[0] for r in self.conn.execute("SELECT session_key FROM session_tokens")]
        self.assertFalse([k for k in keys if k.startswith("self:self:")],
                         f"库里出现了叠加键：{keys}")
        # 幂等：换一种写法（裸名）重跑 → 同一个公民
        again = fish.start_session(self.conn, alias="fish外部评估", environ={})
        self.assertEqual(again["citizen_id"], res["citizen_id"],
                         "self:abc 与 abc 必须命中同一个公民（幂等承诺）")


# ============================================================ FB-06 看板活动列文案
class FB06BoardActivityTextTest(_Case):
    """FB-06：「实际在动」无数据时不许只给 `—`，且表下要注明依赖宿主 hook。"""

    def _board_with_presence(self, doing="写 A2"):
        cid = self._citizen("看板人", host="dsh")
        board = fish.board_checkin(self.conn, cid, doing=doing,
                                   scope="project", scope_key="UU")
        return cid, board

    def test_FB06_empty_activity_says_no_data(self):
        """没有任何 tool_activity → 格子里是"无活动数据（依赖宿主 hook）"，不是 `—`。"""
        _cid, board = self._board_with_presence()
        text = fish._render_one(self.conn, board, [])
        self.assertIn(fish.NO_ACTIVITY_TEXT, text,
                      f"空活动必须自解释（判据变异点：改回 '—' 即红）：\n{text}")
        self.assertIn("无活动数据", text)
        self.assertNotIn("| — |", text, "空格子不许再用破折号糊过去")

    def test_FB06_note_line_under_table(self):
        """表下方那行说明必须在（它回答"为什么这一列全是没数据"）。"""
        _cid, board = self._board_with_presence()
        text = fish._render_one(self.conn, board, [])
        self.assertIn(fish.ACTIVITY_COLUMN_NOTE, text, text)
        self.assertIn("PostToolUse hook", text)
        self.assertIn("纯 MCP 宿主", text)
        # 位置：说明在表格之后（先看到列，再读到解释）
        self.assertGreater(text.index(fish.ACTIVITY_COLUMN_NOTE),
                           text.index("| 会话 | 状态 | 声明在做 | 实际在动 | 最后活跃 |"))

    def test_FB06_with_activity_shows_counts(self):
        """有动作事实 → 显示 `目录（N 次写，HH:MM）`，而不是"无活动数据"。"""
        cid, board = self._board_with_presence()
        fish_activity.record(
            self.conn, host="dsh", session_key="dsh:test", citizen_id=cid,
            classification={"project": "UU", "tool_name": "Write", "action": "write",
                            "path": "p0/a.py", "path_dir": "p0",
                            "path_abs": "I:/x/p0/a.py", "args_digest": "d", "ok": 1})
        text = fish._render_one(self.conn, board, [])
        self.assertIn("p0（1 次写", text, f"有数据必须显示次数：\n{text}")
        self.assertNotIn(fish.NO_ACTIVITY_TEXT, text, "有数据时不能说没有数据")
        # 说明行仍在（它是列的能力边界，与有没有数据无关）
        self.assertIn(fish.ACTIVITY_COLUMN_NOTE, text)


# ============================================================== FB-08 同名公民提示
class FB08SameNameNoticeTest(_Case):
    """FB-08：签发后若存在同名在用公民，回执要说出"你可能又建了一个自己"。"""

    def test_FB08_notice_when_same_name_exists(self):
        """先 register 一个 role 名，再 `session start --alias 同名` → 提示出现。"""
        reg = self.srv.tool_register({"display_name": "a2-同名", "kind": "role"}, self.anon)
        reg_id = reg.splitlines()[0].strip()
        self.assertTrue(reg_id, reg)

        res = fish.start_session(self.conn, alias="a2-同名", environ={})
        conflicts = res["name_conflicts"]
        self.assertEqual([c["citizen_id"] for c in conflicts], [reg_id],
                         f"应命中那条 role 公民：{res}")
        self.assertEqual(conflicts[0]["kind"], "role")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish._print_session_start(res)
        out = buf.getvalue()
        self.assertIn(fish.same_name_notice(reg_id, "role"), out, out)
        self.assertIn("检测到同名公民", out)
        self.assertIn("不自动合并", out, "必须说清「不合并」（决策 14）")
        self.assertIn("--citizen", out, "要给出沿用它的具体动作")

    def test_FB08_no_notice_when_name_is_fresh(self):
        """换名 → 不出现（否则提示会退化成每次都说的噪音）。"""
        self.srv.tool_register({"display_name": "a2-同名2", "kind": "role"}, self.anon)
        res = fish.start_session(self.conn, alias="a2-另一个名字", environ={})
        self.assertEqual(res["name_conflicts"], [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish._print_session_start(res)
        self.assertNotIn("检测到同名公民", buf.getvalue())

    def test_FB08_mcp_receipt_same_wording(self):
        """MCP 的 session_start 回执照样提示，且文案与 CLI **同一个实现**。"""
        reg = self.srv.tool_register({"display_name": "a2-同名3", "kind": "role"}, self.anon)
        reg_id = reg.splitlines()[0].strip()
        out = self.srv.tool_session_start({"session_name": "a2-同名3"}, self.anon)
        self.assertIn(fish.same_name_notice(reg_id, "role"), out, out)

    def test_FB08_self_is_not_a_conflict(self):
        """复用同一条会话（同 key）不该把自己报成同名冲突。"""
        fish.start_session(self.conn, alias="a2-自我", environ={})
        again = fish.start_session(self.conn, alias="a2-自我", environ={})
        self.assertEqual(again["name_conflicts"], [],
                         "自己不是「另一个同名公民」")
        self.assertTrue(again["created"] is False)


# ======================================================= FB-11 CLI supersede 双入口
class FB11SupersedeCliTest(_Case):
    """FB-11：CLI 补 `supersede` 与 `remember --supersedes`，回执与 MCP 同口径。"""

    def _two_memories(self, tag):
        self._project("UU")
        old, _ = fish.record_memory(self.conn, self.root, project="UU",
                                    title=f"旧结论{tag}", body="旧正文")
        new, _ = fish.record_memory(self.conn, self.root, project="UU",
                                    title=f"新结论{tag}", body="新正文")
        return old, new

    def test_FB11_cli_supersede_marks_superseded(self):
        """CLI supersede → 旧条目 status=superseded 且 superseded_by 指向新条目。"""
        old, new = self._two_memories("a")
        ns = argparse.Namespace(root=str(self.root), code=old, by=new, reason="a2 测试")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_supersede(ns)
        row = self.conn.execute(
            "SELECT status, superseded_by FROM code_index WHERE code=?", (old,)).fetchone()
        self.assertEqual(row[0], "superseded")
        self.assertEqual(row[1], new)
        self.assertIn(f"{old} 已标记为被 {new} 替代", buf.getvalue(),
                      f"CLI 回执：{buf.getvalue()!r}")

    def test_FB11_cli_and_mcp_receipts_match(self):
        """同口径：两条回执**除了编码本身**逐字相同（两入口只差一个结尾换行）。"""
        old_a, new_a = self._two_memories("b")
        old_b, new_b = self._two_memories("c")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_supersede(argparse.Namespace(
                root=str(self.root), code=old_a, by=new_a, reason="r"))
        cli_out = buf.getvalue()
        mcp_out = self.srv.tool_supersede({"code": old_b, "by": new_b, "reason": "r"},
                                          self.anon)

        def norm(msg, old, new):
            return msg.replace(old, "<OLD>").replace(new, "<NEW>")

        self.assertEqual(norm(mcp_out, old_b, new_b),
                         norm(cli_out, old_a, new_a),
                         f"MCP 与 CLI 必须说同一句话：\nMCP={mcp_out!r}\nCLI={cli_out!r}")
        self.assertEqual(mcp_out.count("\n"), 1, "MCP 回执只多一个结尾换行")
        self.assertEqual(cli_out.count("\n"), 1, "CLI 的 print 只加一个换行")

    def test_FB11_cli_supersede_missing_code_errors(self):
        """旧/新编码不存在 → 响亮失败（退出码 2），不静默成功。"""
        self._project("UU")
        new, _ = fish.record_memory(self.conn, self.root, project="UU",
                                    title="只有新的", body="x")
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            fish.cmd_supersede(argparse.Namespace(
                root=str(self.root), code="UU-261099-99", by=new, reason=""))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("旧编码不存在", err.getvalue())

    def test_FB11_remember_supersedes_equivalent(self):
        """`remember --supersedes` 等价于 supersede：旧条目标 superseded（顺序：新的先成）。"""
        old, _ = self._two_memories("d")
        ns = argparse.Namespace(
            root=str(self.root), project="UU", title="另一条新结论", body="正文",
            type="fact", visibility="shared", tags=[], owner=None, cause=None,
            occurred_at=None, attach=[], session_actor=None, supersedes=[old])
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            fish.cmd_remember(ns)
        new_code = buf.getvalue().split("\t")[0].strip()
        row = self.conn.execute(
            "SELECT status, superseded_by FROM code_index WHERE code=?", (old,)).fetchone()
        self.assertEqual(row[0], "superseded")
        self.assertEqual(row[1], new_code, "替代者必须是这次刚写下的新条目")
        self.assertIn("已替代：", err.getvalue(), f"stderr 要留痕：{err.getvalue()!r}")

    def test_FB11_remember_supersedes_missing_old_keeps_new(self):
        """旧编码不存在 → 新条目**照样写成功**、不留半状态（替代失败只是提示）。"""
        self._project("UU")
        ns = argparse.Namespace(
            root=str(self.root), project="UU", title="新条目仍在", body="正文",
            type="fact", visibility="shared", tags=[], owner=None, cause=None,
            occurred_at=None, attach=[], session_actor=None,
            supersedes=["UU-261099-99"])
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            fish.cmd_remember(ns)
        new_code = buf.getvalue().split("\t")[0].strip()
        row = self.conn.execute("SELECT status FROM code_index WHERE code=?",
                                (new_code,)).fetchone()
        self.assertIsNotNone(row, "新条目必须已经落盘（顺序：先写新，再置旧）")
        self.assertEqual(row[0], "active")
        self.assertIn("替代未生效", err.getvalue())
        self.assertIn("旧编码不存在", err.getvalue())


# ============================================================= FB-12 公民归档接线
class FB12ArchiveCitizensTest(_Case):
    """FB-12：`archive_citizens` 五条判据 + CLI + 日报接线。

    五条判据里的是非：闲置 → 归档；有活凭证 / 开档期 / 主人 / 刚活跃 → 一律不动。
    """

    NOW = "2026-10-26T04:00:00Z"

    def test_FB12_archives_only_idle_plain_citizen(self):
        stale = self._citizen("闲置者", last_seen="2026-10-01T00:00:00Z")
        fresh = self._citizen("刚活跃", last_seen="2026-10-26T03:00:00Z")
        owner = self._citizen("主人", kind="resident", owner=1,
                              last_seen="2026-09-01T00:00:00Z")
        tokenized = self._citizen("有活凭证", last_seen="2026-09-01T00:00:00Z")
        self._token(tokenized)
        stinted = self._citizen("开档期", last_seen="2026-09-01T00:00:00Z")
        self._stint(stinted)
        none_ts = self._citizen("从没活跃", last_seen=None)
        before = self._archived_events()

        res = fish.archive_citizens(self.conn, days=7, now=self.NOW)
        self.assertEqual(res["archived"], [stale], f"只该归档那一个：{res}")
        self.assertEqual(res["archived_count"], 1)
        # 冷热表：搬而不是复制
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (stale,)).fetchone(),
            "归档后热表不该还有它")
        row = self.conn.execute(
            "SELECT archived_at FROM citizens_archive WHERE citizen_id=?",
            (stale,)).fetchone()
        self.assertIsNotNone(row, "冷表必须有它")
        self.assertEqual(row[0], self.NOW, "archived_at 用注入的时钟（判据要能钉时间）")
        # 事件 +1，且带 reason / evidence
        self.assertEqual(self._archived_events(), before + 1)
        payload = self.conn.execute(
            "SELECT payload FROM events WHERE type='citizen.archived' "
            "ORDER BY seq DESC LIMIT 1").fetchone()[0]
        self.assertIn("闲置归档", payload)
        self.assertIn("evidence", payload)
        self.assertIn("cutoff", payload)
        # 四个不动的人各有**具体**理由（不是一句"跳过"）
        why = {it["citizen_id"]: it["why"] for it in res["skipped"]}
        self.assertIn("仍活跃", why[fresh])
        self.assertIn("未关档期", why[stinted])
        self.assertIn("未失效凭证", why[tokenized])
        self.assertIn("无法判定", why[none_ts])
        for cid in (fresh, owner, tokenized, stinted, none_ts):
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM citizens WHERE citizen_id=?", (cid,)).fetchone(),
                f"{cid} 不该被归档")

    def test_FB12_two_days_idle_archived_at_days_1(self):
        """交付判据原样复审：`last_seen` 2 天前 + 无 token/无档期/非主人 → 归档且事件 +1。"""
        idle = self._citizen("两天没动", last_seen="2026-10-24T04:00:00Z")
        before = self._archived_events()
        res = fish.archive_citizens(self.conn, days=1, now=self.NOW)
        self.assertEqual(res["archived"], [idle], res)
        self.assertEqual(self._archived_events(), before + 1)
        self.assertEqual(res["cutoff"], "2026-10-25T04:00:00Z",
                         "截止时间必须是 now-days（判据要能钉住时钟）")

    def test_FB12_dry_run_moves_nothing(self):
        """演练只算不动（`planned` 有它、热表还有它）。"""
        stale = self._citizen("闲置者2", last_seen="2026-10-01T00:00:00Z")
        res = fish.archive_citizens(self.conn, days=7, now=self.NOW, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual([it["citizen_id"] for it in res["planned"]], [stale])
        self.assertEqual(res["archived"], [])
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (stale,)).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM citizens_archive WHERE citizen_id=?", (stale,)).fetchone())

    def test_FB12_days_zero_is_rejected(self):
        """days=0 的语义是"不自动归档"，把它当阈值会归档掉所有人 → 响亮拒绝。"""
        with self.assertRaises(ValueError) as ctx:
            fish.archive_citizens(self.conn, days=0, now=self.NOW)
        self.assertIn("days 必须 ≥1", str(ctx.exception))

    def test_FB12_cli_dry_run_then_apply(self):
        """CLI：默认演练不搬；`--apply` 才搬。"""
        stale = self._citizen("闲置者3", last_seen="2026-10-01T00:00:00Z")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fish.cmd_archive(argparse.Namespace(
                root=str(self.root), citizens=True, days=7, apply=False,
                action=None, code=None, dry_run=False, year=None, limit=None,
                withdrawn=True))
        self.assertEqual(code, 0)
        self.assertIn("演练", buf.getvalue())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (stale,)).fetchone())
        with contextlib.redirect_stdout(io.StringIO()):
            code = fish.cmd_archive(argparse.Namespace(
                root=str(self.root), citizens=True, days=7, apply=True,
                action=None, code=None, dry_run=False, year=None, limit=None,
                withdrawn=True))
        self.assertEqual(code, 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (stale,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM citizens_archive WHERE citizen_id=?", (stale,)).fetchone())

    def test_FB12_cli_zero_days_skips(self):
        """`--days 0` → 打印跳过，一个都不搬（CLI 侧对"未配置"的显式表达）。"""
        self._citizen("闲置者4", last_seen="2026-10-01T00:00:00Z")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fish.cmd_archive(argparse.Namespace(
                root=str(self.root), citizens=True, days=0, apply=True,
                action=None, code=None, dry_run=False, year=None, limit=None,
                withdrawn=True))
        self.assertIn("跳过（未配置）", buf.getvalue())

    def test_FB12_daily_report_step_zero_prints_skip(self):
        """日报：config=0 → 明写"跳过（未配置）"（默认就是 0，静默不算过关）。"""
        fish.set_config(self.conn, "session.archive_after_days", 0)
        line = daily_report.archive_citizens_step(data_dir=self.root)
        self.assertIn("跳过（未配置）", line, line)

    def test_FB12_daily_report_step_archives_when_configured(self):
        """日报：config=N → 真的归档（这一步此前完全没有调用点）。"""
        fish.set_config(self.conn, "session.archive_after_days", 7)
        stale = self._citizen("闲置者5", last_seen=_iso_days_ago(10))
        line = daily_report.archive_citizens_step(data_dir=self.root)
        self.assertIn("公民归档：1 个", line, line)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (stale,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM citizens_archive WHERE citizen_id=?", (stale,)).fetchone())


# ============================================================== FB-13 CLI 入口安装
class FB13InstallCliTest(_Case):
    """FB-13：`install_cli.py` 默认演练 / `--apply` 生成 shim + 两个回执带入口行。"""

    def test_FB13_dry_run_writes_nothing(self):
        bin_dir = self.base / "bin"
        res = install_cli.install(apply=False, bin_dir=bin_dir)
        self.assertFalse(res["written"])
        self.assertFalse(bin_dir.exists(), "演练不许创建目录/文件")
        self.assertTrue(res["content"].startswith('@"'),
                        f"shim 内容要以 @\" 开头：{res['content']!r}")
        self.assertIn(str(Path("fish.py").resolve()), res["content"])
        self.assertIn("%*", res["content"], "参数必须原样转发")

    def test_FB13_apply_generates_shim(self):
        bin_dir = self.base / "bin"
        res = install_cli.install(apply=True, bin_dir=bin_dir)
        shim = bin_dir / "fish.cmd"
        self.assertTrue(res["verified"], "写后必须逐字校验通过")
        self.assertTrue(shim.exists())
        # 2026-10-26 合并后修复：shim 按 Windows ANSI（mbcs）写，读也得同编码
        # （主树路径含中文时，artifacts 里是中文而非 ASCII）。
        text = shim.read_text(encoding="mbcs" if os.name == "nt" else "utf-8")
        self.assertIn(str(Path("fish.py").resolve()), text, "shim 必须指向本仓 fish.py")
        self.assertIn("%*", text, "参数必须原样转发")

    def test_FB13_main_defaults_to_dry_run(self):
        """`main()` 不加 --apply 时同样不落文件，并打印"没有写任何文件"。"""
        bin_dir = self.base / "bin"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = install_cli.main(["--bin-dir", str(bin_dir)])
        self.assertEqual(code, 0)
        self.assertFalse((bin_dir / "fish.cmd").exists())
        self.assertIn("没有写任何文件", buf.getvalue())
        self.assertIn(str(bin_dir), buf.getvalue(), "要打印需要加进 PATH 的目录")

    def test_FB13_main_apply_writes_shim(self):
        bin_dir = self.base / "bin"
        with contextlib.redirect_stdout(io.StringIO()):
            code = install_cli.main(["--apply", "--bin-dir", str(bin_dir)])
        self.assertEqual(code, 0)
        self.assertTrue((bin_dir / "fish.cmd").exists())

    def test_FB13_whoami_receipt_has_entry_line(self):
        out = self.srv.tool_whoami({}, self.anon)
        self.assertIn("CLI 入口：python ", out, out)
        self.assertIn("fish.py", out)
        self.assertIn("install_cli.py --apply", out)
        self.assertIn("fish.cmd", out)

    def test_FB13_register_receipt_has_entry_line(self):
        """register 的两条分支（新建 / 已存在）都要有入口行。"""
        first = self.srv.tool_register({"display_name": "a2-入口"}, self.anon)
        self.assertIn("CLI 入口：python ", first, first)
        again = self.srv.tool_register({"display_name": "a2-入口"}, self.anon)
        self.assertIn("已存在，直接使用", again)
        self.assertIn("CLI 入口：python ", again, again)


if __name__ == "__main__":
    unittest.main(verbosity=2)
