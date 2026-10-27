#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T05 判据 · 撞车 ask（PreToolUse）。

## 三态与红线（用户 2026-10-26 拍板 + 主理人决策）

`board.collision_ask` = `off` / `warn`（默认）/ `ask`。默认 `warn` 的理由是硬的：
当前宿主 approvals disabled 时 `ask` 会被**自动拒绝** → "撞车提示"变成"撞车拒写"。
所以：
  · `warn`：只记审计，**不输出任何决策**（不阻塞）；
  · `ask`：输出 `permissionDecision="ask"` + 可读原因；
  · `off`：连审计都不做。
`deny` 在本批**绝不出现**（用户拍板：凭证 deny / 危险命令 ask 交给宿主机制）。

## 判据清单

  P1  真重叠 → `ask`（含名字/项目/路径/分钟数，**不含 ULID、不含内容**）
  P2  自己会话 → 不问；同一公民的**另一会话** → 要问，并标「另一会话」
  P3  读类 / 无路径 / 快照缺失 / 快照过旧 → 零输出（放行）
  P4  冷却：同 (session, 目标) 60s 内不重复问；审计仍只有 1 行
  P5  三态：`warn` 不输出但记审计；`off` 什么都不做
  P6  路径方言：大写工具名 + 反斜杠路径 + 相对 cwd 都能判定
  P7  协议形状：`permissionDecision=ask`、`hookEventName=PreToolUse`、纯 ASCII
  P8  变异：删掉"排除自己会话" → P2 红；把 ask 改成 deny → P7 红
  P9  `warn` 的**可见性**（评审 L3）：PreToolUse 零输出但落一次性 pending 标记；
      下一次 PostToolUse 递一句含撞车信息的 additionalContext；只递一次；
      `off` 连标记都不写
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402
import fish_posttool_hook  # noqa: E402
import fish_pretool_hook  # noqa: E402
import fish_push_hook  # noqa: E402
import hook_io  # noqa: E402

MY_SESSION = "dsh:me"
OTHER_SESSION = "dsh:other"
MY_CITIZEN = "C0AAAAAAAAAAAAAAAAAAAAAAAA"
OTHER_CITIZEN = "C1BBBBBBBBBBBBBBBBBBBBBBBB"


class AskBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-ask-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO workspace_map (path_prefix,project,note,created_at) "
            "VALUES ('i:/proj','PJ',NULL,?)", (fish.now_utc(),))
        for cid, name, key in ((OTHER_CITIZEN, "别人-主AI", OTHER_SESSION),
                               (MY_CITIZEN, "我-主AI", MY_SESSION)):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,"
                "host,host_session_key) VALUES (?,?,'resident','active',?,'dsh',?)",
                (cid, name, fish.now_utc(), key))
        self.conn.commit()
        self.snapshot_path = self.root / fish_activity.SNAPSHOT_RELPATH
        self.asks_path = self.root / fish_activity.ASKS_RELPATH

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具
    def _declare(self, citizen, session_key, surface, *, project="PJ", minutes_ago=0):
        board = fish.board_checkin(self.conn, citizen, "在改这块", surface=surface,
                                   scope="project", scope_key=project)
        self.conn.execute(
            "UPDATE presence SET agent_id=?, last_seen_at=?, first_seen_at=? "
            "WHERE board_id=? AND citizen_id=?",
            (session_key, self._ts(minutes_ago), self._ts(minutes_ago),
             board["id"], citizen))
        self.conn.commit()
        return board

    @staticmethod
    def _ts(minutes_ago=0):
        import datetime
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _build_snapshot(self, *, minutes_ago=0):
        fish_activity.build_snapshot(self.conn, out_path=self.snapshot_path, window_min=90,
                                     now=self._ts(minutes_ago))

    def _payload(self, path="i:/proj/p0/fish.py", *, session="me", tool="write",
                 cwd="i:/proj"):
        return {"session_id": session, "cwd": cwd, "tool_name": tool,
                "tool_input": {"file_path": path}}

    def _run_main(self, payload, *, host="dsh"):
        """真跑 `main()`：注入 stdin，捕获 stdout（这就是宿主看到的东西）。

        ⚠️ 必须同时把模块级 `DATA`/`SNAPSHOT_PATH`/`ASKS_PATH` 指到临时目录：
        这三个常量在 import 时按 `FISH_DATA` 算好，测试进程里 import 过一次就固定了。
        只改环境变量是**不会**生效的（那会让判据悄悄读写生产库）。
        """
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout = io.StringIO()
        try:
            with mock.patch.multiple(fish_pretool_hook, DATA=self.root,
                                     SNAPSHOT_PATH=self.snapshot_path,
                                     ASKS_PATH=self.asks_path):
                code = fish_pretool_hook.main(["--host", host])
            out = sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        return code, out

    def _asks(self):
        if not self.asks_path.exists():
            return []
        return [json.loads(line) for line in
                self.asks_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # ---------------------------------------------------------------- 夹具（L3）
    def _pending_files(self):
        folder = self.root / fish_activity.HEARTBEAT_RELDIR
        if not folder.exists():
            return []
        return sorted(folder.glob(f"{fish_activity.WARN_PENDING_PREFIX}*.json"))

    def _posttool_payload(self, *, path="i:/proj/p0/_collision.txt", tool="write",
                          session="me", cwd="i:/proj"):
        """PostToolUse 的入参。

        ⚠️ 路径里**不许**出现 fish 的存储特征（`fish.py` / `_audit`…）：那会额外触发
        绕路提醒，让"这次输出是不是撞车提醒"变得分不清（判据就该只测一件事）。
        """
        return {"session_id": session, "cwd": cwd, "tool_name": tool,
                "tool_input": {"file_path": path}, "tool_response": {"ok": True}}

    def _run_posttool(self, payload, *, host="dsh"):
        """真跑 WorkBuddy 侧 PostToolUse（采集+绕路+撞车提醒+推送四段）。

        宿主必须与 PreToolUse 一致（`dsh`）：pending 标记按**会话键哈希**存，
        `workbuddy` 的键前缀是 `codebuddy:`，拿错宿主的键就永远读不到自己那一条。
        """
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout = io.StringIO()
        try:
            with mock.patch.multiple(fish_posttool_hook, DATA=self.root,
                                     AUDIT=self.root / "_audit"):
                fish_posttool_hook.main(["--host", host])
            return sys.stdout.getvalue()
        finally:
            sys.stdin, sys.stdout = old_in, old_out

    @staticmethod
    def _contexts(output):
        """把 hook 输出里的 `additionalContext` 全部取出来并拼成一段。

        输出是**纯 ASCII 的 JSON**（`hook_io` 硬编码 `ensure_ascii=True`，见其文档），
        所以断言必须解 JSON 之后看中文——直接 `assertIn("撞车", 原始输出)` 永远失败
        （那会变成"判据看起来对、其实永远红"的反面教材）。
        """
        texts = []
        for line in str(output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            block = payload.get("hookSpecificOutput") if isinstance(payload, dict) else None
            if isinstance(block, dict) and block.get("additionalContext"):
                texts.append(str(block["additionalContext"]))
        return " ".join(texts)


class RealCollisionTest(AskBase):
    """P1/P7：真重叠 → ask，且原因可读、不含机器码、不含内容。"""

    def test_ask_mode_emits_decision(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code, out = self._run_main(self._payload())
        self.assertEqual(code, 0)
        self.assertTrue(out.strip(), "真重叠时必须有决策输出")
        self.assertTrue(out.isascii(), f"决策输出必须纯 ASCII：{out!r}")
        payload = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(payload["hookEventName"], "PreToolUse")
        self.assertEqual(payload["permissionDecision"], "ask")
        reason = payload["permissionDecisionReason"]
        self.assertIn("别人-主AI", reason, "原因里必须有人能认的名字")
        self.assertIn("PJ", reason, "原因里要有项目名（让人知道是哪块板）")
        self.assertIn("p0", reason)
        self.assertNotIn(OTHER_CITIZEN, reason, "原因里不许出现 26 位公民 ID")
        self.assertNotIn(MY_CITIZEN, reason)
        self.assertNotIn("在改这块", reason,
                         "原因里不许带 doing/内容（只给路径与时间）")

    def test_warn_mode_is_silent_but_audited(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        code, out = self._run_main(self._payload())
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "默认 warn 模式：**不阻塞、不输出**")
        asks = self._asks()
        self.assertEqual(len(asks), 1, "但必须留审计（误报率靠它算）")
        self.assertEqual(asks[0]["decision"], "warn")
        self.assertEqual(asks[0]["target"], "i:/proj/p0/fish.py")
        self.assertEqual(len(asks[0]["hits"]), 1)
        self.assertNotIn("content", json.dumps(asks[0], ensure_ascii=False))


class ExclusionTest(AskBase):
    """P2：排除自己会话；同一公民的另一会话**不**排除。"""

    def test_own_session_excluded(self):
        self._declare(MY_CITIZEN, MY_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        res = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
        self.assertEqual(res["hits"], [], f"自己写自己不该问：{res}")
        code, out = self._run_main(self._payload())
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_same_citizen_other_session_asks(self):
        """同一公民的另一个会话是**真撞车**（两个窗口同改一个文件）。"""
        self._declare(MY_CITIZEN, "dsh:me-2", "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        res = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
        self.assertEqual(len(res["hits"]), 1)
        self.assertTrue(res["hits"][0]["another_session"],
                        "同一公民的另一会话要标「另一会话」，否则人会以为系统认错了人")
        self.assertIn("另一会话", res["reason"])


class SkipPathsTest(AskBase):
    """P3：所有"不确定"都必须放行（零输出）。"""

    def test_read_tool_skipped(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code, out = self._run_main(self._payload(tool="read"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "读类不撞车，绝不该问")

    def test_no_path_skipped(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code, out = self._run_main({"session_id": "me", "cwd": "i:/proj",
                                    "tool_name": "write", "tool_input": {}})
        self.assertEqual(out, "", "没有目标路径 → 放行")

    def test_missing_snapshot_skipped(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code, out = self._run_main(self._payload())     # 没建快照
        self.assertEqual(out, "", "快照缺失 → 放行（最坏漏报，不误报）")

    def test_stale_snapshot_skipped(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        payload["generated_at"] = self._ts(600)          # 10 小时前
        self.snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code, out = self._run_main(self._payload())
        self.assertEqual(out, "", "快照过旧 → 放行")

    def test_single_segment_surface_not_asked(self):
        """1 段的**相对**声明（如 `p0`）在没有项目根时不能拿去和绝对路径比。"""
        board = fish.board_checkin(self.conn, OTHER_CITIZEN, "改东西", surface="p0",
                                   scope="root", scope_key="")
        self.conn.execute("UPDATE presence SET agent_id=? WHERE board_id=?",
                          (OTHER_SESSION, board["id"]))
        self.conn.commit()
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        res = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
        self.assertEqual(res["hits"], [],
                         "根板没有项目根，相对面无法证明是同一处 → 放行（保守）")

    def test_expired_declaration_not_asked(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0", minutes_ago=200)  # > 90min
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        res = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
        self.assertEqual(res["hits"], [], "90 分钟没动的声明不算'正在写'")


class CooldownTest(AskBase):
    """P4：冷却只防"同一次调用重复弹窗"，不当作"用户已批准"。"""

    def test_second_ask_within_cooldown_is_silent(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        code1, out1 = self._run_main(self._payload())
        code2, out2 = self._run_main(self._payload())
        self.assertEqual((code1, code2), (0, 0))
        self.assertTrue(out1.strip(), "第一次必须问")
        self.assertEqual(out2, "", "冷却期内不该再问")
        self.assertEqual(len(self._asks()), 1, "审计只该有一行")

    def test_cooldown_expires(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        self._run_main(self._payload())
        # 把上一行的 ts 拨到 5 分钟前 → 冷却（60s）已过
        asks = self._asks()
        asks[0]["ts"] = self._ts(5)
        self.asks_path.write_text(json.dumps(asks[0], ensure_ascii=False) + "\n",
                                  encoding="utf-8")
        code, out = self._run_main(self._payload())
        self.assertTrue(out.strip(), "冷却过后应该再次提醒（不是永久闭嘴）")
        self.assertEqual(len(self._asks()), 2)


class ModeTest(AskBase):
    """P5：三态配置。"""

    def test_off_is_completely_silent(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "off")
        code, out = self._run_main(self._payload())
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(self._asks(), [], "off = 连审计都不做")

    def test_default_is_warn(self):
        """默认值必须是 warn（不是 ask）：这条钉住用户拍板。"""
        self.assertEqual(fish.CONFIG_DEFAULTS["board.collision_ask"][0], "warn")
        self.assertEqual(fish.CONFIG_DEFAULTS["board.collision_ask"][2],
                         ["off", "warn", "ask"], "三态词表是封闭的")
        self.assertEqual(fish.get_config(self.conn, "board.collision_ask"), "warn")


class DialectTest(AskBase):
    """P6：大小写与斜杠方言（WorkBuddy 首字母大写 + 反斜杠路径）。"""

    def test_uppercase_tool_and_backslashes(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        payload = {"session_id": "me", "cwd": "I:\\proj", "tool_name": "Write",
                   "tool_input": {"file_path": "I:\\proj\\p0\\fish.py"}}
        res = fish_pretool_hook.evaluate(payload, host="workbuddy", data_dir=self.root)
        self.assertEqual(len(res["hits"]), 1,
                         f"反斜杠/大写都要归一：{res}")
        self.assertEqual(res["target_abs"], "i:/proj/p0/fish.py")

    def test_relative_target_resolved_against_cwd(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        res = fish_pretool_hook.evaluate(
            self._payload(path="p0/fish.py"), data_dir=self.root)
        self.assertEqual(res["target_abs"], "i:/proj/p0/fish.py")
        self.assertEqual(len(res["hits"]), 1)


class MutationTest(AskBase):
    """P8：判据能变红。"""

    def test_drop_own_session_exclusion_goes_red(self):
        self._declare(MY_CITIZEN, MY_SESSION, "p0")
        self._build_snapshot()
        original = fish_activity.find_collisions

        def loose(snapshot, *, target_abs, own_session_key, **kw):
            return original(snapshot, target_abs=target_abs, own_session_key=None, **kw)

        try:
            fish_activity.find_collisions = loose
            res = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
            self.assertTrue(res["hits"],
                            "变异后自己写自己也会命中——说明 P2 确实在测'排除自己'")
        finally:
            fish_activity.find_collisions = original
        res2 = fish_pretool_hook.evaluate(self._payload(), data_dir=self.root)
        self.assertEqual(res2["hits"], [], "恢复后必须回到'不问自己'")

    def test_deny_would_break_protocol_judgement(self):
        """把 ask 换成 deny → P7 的协议判据必须红（用户红线：fish 不做 deny）。"""
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")

        def deny(reason):
            hook_io.emit_decision("PreToolUse", "deny", reason)

        try:
            fish_pretool_hook._emit_decision = deny
            _code, out = self._run_main(self._payload())
            decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
            self.assertNotEqual(decision, "ask",
                                "变异后决策变成 deny——说明 P7 确实在钉 ask")
        finally:
            fish_pretool_hook._emit_decision = (
                lambda reason: hook_io.emit_decision("PreToolUse", "ask", reason))
        # 清掉冷却记录再跑一次：否则第二次会被 60s 冷却挡住（那是另一条判据的事）
        if self.asks_path.exists():
            self.asks_path.unlink()
        _code, out = self._run_main(self._payload())
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")


class WarnPendingTest(AskBase):
    """P9（评审 L3）：`warn` 不阻塞，但也**不能对模型不可见**。

    旧实现：`warn` 只写 `pretool-asks.jsonl`——那是**人**的审计文件，模型永远读不到；
    于是"撞车提醒"在默认配置下对模型等于没发生（而默认值恰恰是 warn）。
    修法：warn 时落一次性 pending 标记，由**下一次 PostToolUse**（同一次调用的收尾）
    递一句 `additionalContext`——即时、不阻塞、只一次、文件留着可审计。
    """

    def test_warn_marks_pending_then_next_posttool_reports_once(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        code, out = self._run_main(self._payload())
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "warn 仍然不阻塞：PreToolUse 零输出")
        pending = self._pending_files()
        self.assertEqual(len(pending), 1,
                         "warn 必须留下一次性标记——否则模型永远看不到这次撞车")
        raw = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertIsNone(raw["delivered_at"], "还没递出去")
        self.assertEqual(raw["target"], "i:/proj/p0/fish.py")
        self.assertNotIn("在改这块", json.dumps(raw, ensure_ascii=False),
                         "标记里不许带 doing/内容")

        first = self._contexts(self._run_posttool(self._posttool_payload()))
        self.assertIn("撞车", first, f"下一次 PostToolUse 必须递一句话：{first!r}")
        self.assertIn("别人-主AI", first, "提醒里要有人能认的名字")
        self.assertIn("只提醒一次", first)
        self.assertNotIn(OTHER_CITIZEN, first, "不许出现 26 位公民 ID")
        delivered = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertIsNotNone(delivered["delivered_at"], "递过之后要留痕（可审计）")

        second = self._run_posttool(self._posttool_payload())
        self.assertEqual(second, "", "同一标记只递一次（冷却内不重复）")

    def test_off_mode_writes_no_pending(self):
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "off")
        code, out = self._run_main(self._payload())
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self._pending_files(), [], "off = 什么都不留（连标记也不写）")

    def test_ask_mode_writes_no_pending(self):
        """`ask` 走决策通道，不要再叠一句 pending（重复提醒=噪音）。"""
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        fish.set_config(self.conn, "board.collision_ask", "ask")
        _code, out = self._run_main(self._payload())
        self.assertTrue(out.strip(), "ask 必须输出决策")
        self.assertEqual(self._pending_files(), [])

    def test_dsh_push_hook_delivers_too(self):
        """DSH 侧的递送口也必须通——否则"默认 warn"在 DSH 上等于没提醒。"""
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        self._run_main(self._payload())          # 默认 warn
        old_out = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with mock.patch.object(fish_push_hook, "DATA", self.root):
                fish_push_hook.maybe_warn_pending(self._posttool_payload(), "dsh")
            raw = sys.stdout.getvalue()
        finally:
            sys.stdout = old_out
        text = self._contexts(raw)
        self.assertIn("撞车提醒", text)
        self.assertIn("别人-主AI", text)

    def test_stale_pending_not_delivered(self):
        """过旧的标记不再打扰（15 分钟前的"别人在写这块"只是噪音）。"""
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        self._run_main(self._payload())
        pending = self._pending_files()[0]
        payload = json.loads(pending.read_text(encoding="utf-8"))
        payload["ts"] = self._ts(60)             # 一小时前
        pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._run_posttool(self._posttool_payload()), "",
                         "过期标记不递")
        self.assertTrue(pending.exists(), "但文件留着当'过期没递出去'的证据")

    # ------------------------------------------------------------ 评审 F9 / F10
    #
    # F9：`delivered_at` 是"只递一次"的唯一凭据。旧实现把 `_write_state` 的返回值
    #     丢了 → 盘坏了也照样递 → 下一次 PostToolUse 再递一遍（盘一直坏就一直刷）。
    # F10：同会话并行两次撞车时保留哪一条，此前没写死；现在写死"后到者覆盖，
    #      保留最新一条"，并用 `ts` 挡一层乱序（迟到的旧事件不许盖掉更新的那条）。

    def test_write_failure_delivers_nothing_and_keeps_pending(self):
        """F9：写盘失败 → **不递**，标记留在盘上（下次再试，不许重复刷）。"""
        self._declare(OTHER_CITIZEN, OTHER_SESSION, "p0")
        self._build_snapshot()
        self._run_main(self._payload())          # 默认 warn → 落 pending
        self.assertEqual(len(self._pending_files()), 1)
        with mock.patch.object(fish_activity, "_write_state", return_value=False):
            first = self._run_posttool(self._posttool_payload())
            second = self._run_posttool(self._posttool_payload())
        self.assertEqual(first, "", "落不了盘就不递（否则同一句会被重复递给模型）")
        self.assertEqual(second, "", "第二次同样不递——盘坏不是重复提醒的理由")
        # 盘恢复后**恰好**再递一次（pending 没丢，delivered_at 仍为空）
        third = self._run_posttool(self._posttool_payload())
        self.assertIn("撞车", self._contexts(third), "盘恢复后要补上这一次（且只有一次）")
        self.assertEqual(self._run_posttool(self._posttool_payload()), "",
                         "补递之后就必须归零（否则 F9 只是把重复推迟了）")

    def test_later_overwrites_earlier_pending(self):
        """F10 覆盖规则：后到者覆盖，保留最新一条。"""
        session = "dsh:parallel"
        self.assertTrue(fish_activity.set_warn_pending(
            self.root, session_key=session, reason="先到的提醒",
            now="2026-10-26T06:00:00Z"))
        self.assertTrue(fish_activity.set_warn_pending(
            self.root, session_key=session, reason="后到的提醒",
            now="2026-10-26T06:01:00Z"))
        # 2026-10-26：`take_warn_pending` 按**墙钟**判过期（15 分钟），而这里的时间是
        # 硬编码的 06:00Z——跑得晚一点就必红（时间炸弹）。把同一基准显式传进去，
        # 断言一个字不改：判的是"只有一句 + 后到者胜"，不是"现在几点"。
        payload = fish_activity.take_warn_pending(self.root, session,
                                                  now="2026-10-26T06:02:00Z")
        self.assertIn("后到的提醒", payload["reason"], "后到者覆盖（只欠一句）")

    def test_out_of_order_older_pending_does_not_win(self):
        """F10 乱序保护：`ts` 更旧的写入不许盖掉更新的那条。"""
        session = "dsh:ooorder"
        self.assertTrue(fish_activity.set_warn_pending(
            self.root, session_key=session, reason="新的那条",
            now="2026-10-26T06:05:00Z"))
        self.assertFalse(fish_activity.set_warn_pending(
            self.root, session_key=session, reason="迟到的旧事件",
            now="2026-10-26T06:00:00Z"),
            "乱序到达的旧事件必须被丢掉（返回 False = 没落盘）")
        payload = fish_activity.take_warn_pending(self.root, session,
                                                 now="2026-10-26T06:05:30Z")
        self.assertIn("新的那条", payload["reason"])
        self.assertNotIn("迟到的旧事件", payload["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
