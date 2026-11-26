#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2 判据：状态写与 append_event **同一个事务**（2026-11-25 外部审查）。

## 修的是什么

旧实现大量写成：

    conn.execute("UPDATE ...")   # 状态
    conn.commit()                # 先落盘
    append_event(conn, "...")    # 事件再单独提交

事件写失败时状态已经落盘 → 投影与事件流分裂（ADR-001 的"事件是唯一真相源"失效）。
现在统一改成：

    try:
        状态写
        append_event(..., commit=False)
        conn.commit()
    except Exception:
        conn.rollback(); raise

## 本文件怎么证明它

每条"失败注入"判据都用 `mock.patch` 把 `append_event` 换成**必抛异常**，然后：

  1. 异常必须**向上抛**（不许被吞）；
  2. 同连接读数必须回到写前（抓"没 rollback"：未提交的脏数据在同连接可见）；
  3. 另一条连接读数也必须回到写前（抓"先 commit 后 append"：已提交的脏数据别人可见）；
  4. 恢复后重跑一次 → 状态与事件各只落一份（抓"重试产生重复事件/重复状态"）。

对照组（不注入）证明正常路径确实把"状态 + 事件"一起提交了——否则"全 rollback"
也能让上面几条变绿，而那等于功能全丢。

覆盖的代表函数：session start / ensure / end、record_access、supersede、withdraw、
set_owner、fix_owner、cmd_forgetting，外加 set_config（fish_db）与正常路径对照。
"""
import contextlib
import gc
import io
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_db  # noqa: E402
import fish_session  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-atomicity-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------- 夹具 ----------------
    def _mem(self, title="原子性", project="TEST", **kw):
        # `record_memory` 要求前缀已在 `code_registry` 注册；测试库没有 seed，先补一行。
        if not self.conn.execute("SELECT 1 FROM code_registry WHERE prefix=?",
                                 (project,)).fetchone():
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,?,?,?)", (project, project, "", 0, "active"))
            self.conn.commit()
        code, _ = fish.record_memory(self.conn, self.root, project=project, title=title, **kw)
        self.conn.commit()
        return code

    def _citizen(self, name="原子公民", kind="resident", is_owner=0):
        cid = fish.ulid()
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,status,created_at,is_owner) "
            "VALUES (?,?,?,?,?,?)",
            (cid, name, kind, "active", fish.now_utc(), is_owner))
        self.conn.commit()
        return cid

    def _count(self, table, where="", params=()):
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += " WHERE " + where
        return self.conn.execute(sql, params).fetchone()[0]

    def _event_count(self, etype):
        return self._count("events", "type=?", (etype,))

    def _one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def _same_state_both_connections(self, sql, params=(), msg=""):
        """同连接 + 另一条连接都读一遍：前者抓未 rollback，后者抓提前 commit。"""
        mine = self.conn.execute(sql, params).fetchone()
        other = fish.connect(self.root)
        try:
            theirs = other.execute(sql, params).fetchone()
        finally:
            other.close()
        self.assertEqual(mine, theirs, f"{msg}：同连接与另一条连接读数不一致（有未提交脏数据）")
        return mine

    def _fail_append(self, module=fish):
        return mock.patch.object(
            module, "append_event",
            side_effect=RuntimeError("inject: append_event 写失败"))

    def _quiet(self):
        return contextlib.redirect_stdout(io.StringIO())


class SessionAtomicityTest(_Base):
    """session start / ensure / end 三条链。"""

    def test_start_session_failure_rolls_back_all_state(self):
        """start 失败注入：公民/凭证/档期/事件全部回到写前；旧 token 仍有效。"""
        first = fish.start_session(self.conn, alias="atom-start", environ={})
        token = first["token"]
        cid = first["citizen_id"]
        self.conn.commit()
        before_citizens = self._count("citizens")
        before_tokens = self._count("session_tokens")
        before_events = self._event_count("session.opened")
        self.assertEqual(fish.validate_session_token(self.conn, token), cid, "前置：旧 token 有效")

        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.start_session(self.conn, alias="atom-start", environ={})

        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM citizens", (), "start 失败后公民数"), (before_citizens,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_tokens", (), "start 失败后凭证数"), (before_tokens,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM events WHERE type='session.opened'", (),
            "start 失败后事件数"), (before_events,))
        self.assertEqual(fish.validate_session_token(self.conn, token), cid,
                         "start 失败后旧 token 必须仍然有效（轮换被回滚）")

        # 重试：状态与事件各只落一份
        second = fish.start_session(self.conn, alias="atom-start", environ={})
        self.assertEqual(self._count("citizens"), before_citizens, "重试不该新建公民")
        self.assertEqual(self._event_count("session.opened"), before_events + 1,
                         "重试只该写一条 session.opened")
        live = self._one(
            "SELECT COUNT(*) FROM session_tokens WHERE session_key=? AND revoked_at IS NULL",
            (first["session_key"],))[0]
        self.assertEqual(live, 1, "同一会话任一时刻只能有一条活凭证")
        self.assertEqual(fish.validate_session_token(self.conn, second["token"]), cid)

    def test_start_session_first_use_rolls_back_new_citizen(self):
        """首次 start（要新建公民）失败注入：嵌套的 citizen.registered 也不许提前落盘。

        这条专抓"外层回滚了、但 `find_or_create_session_citizen` 内部 append_event
        用的是默认 commit=True"——那会让公民已经提交，而 session.opened 没写。
        """
        before_citizens = self._count("citizens")
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.start_session(self.conn, alias="atom-first-use", environ={})
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM citizens", (), "首次 start 失败后公民数"), (before_citizens,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_identity", (), "首次 start 失败后身份投影"), (0,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_tokens", (), "首次 start 失败后凭证"), (0,))

    def test_start_session_nested_event_failure_rolls_back_citizen(self):
        """只让**外层**事件失败：嵌套的 citizen.registered 也必须一起回滚。

        与上一条的区别：上一条把所有 append_event 都打挂，抓不到"嵌套那次已经
        提交了"——这里放行 citizen.registered、只打挂 session.opened，专门验证
        `find_or_create_session_citizen(commit=False)` 的接线。
        """
        real = fish.append_event
        before_citizens = self._count("citizens")

        def flaky(conn, etype, *args, **kwargs):
            if etype == "session.opened":
                raise RuntimeError("inject: session.opened 写失败")
            return real(conn, etype, *args, **kwargs)

        with mock.patch.object(fish, "append_event", side_effect=flaky):
            with self.assertRaises(RuntimeError, msg="session.opened 异常必须向上抛"):
                fish.start_session(self.conn, alias="atom-nested", environ={})

        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM citizens", (), "嵌套事件失败后公民数"), (before_citizens,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM events WHERE type='citizen.registered'", (),
            "嵌套事件失败后 citizen.registered"), (0,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_tokens", (), "嵌套事件失败后凭证"), (0,))

    def test_start_session_normal_path_commits_state_and_event(self):
        """正常路径对照：状态与事件同事务提交，事件 hash 与回执一致。"""
        res = fish.start_session(self.conn, alias="atom-normal", environ={})
        row = self._one("SELECT citizen_id FROM citizens WHERE citizen_id=?",
                        (res["citizen_id"],))
        self.assertIsNotNone(row, "公民没落库")
        ev = self._one("SELECT payload FROM events WHERE type='session.opened' "
                       "ORDER BY seq DESC LIMIT 1")
        self.assertIsNotNone(ev, "session.opened 事件没落库")
        self.assertIn(res["token_hash"], ev[0], "事件里的 token_hash 与回执不一致")

    def test_ensure_session_failure_rolls_back_new_citizen(self):
        """ensure 失败注入：新建公民/身份投影/凭证/事件全部回滚。"""
        before_citizens = self._count("citizens")
        before_events = self._event_count("session.ensured")
        key = "cli:atom-ensure"
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.ensure_session(self.conn, session_key=key, host="cli",
                                    evidence="self-reported", parent_session="no-such-parent")
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM citizens", (), "ensure 失败后公民数"), (before_citizens,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_identity WHERE session_key=?", (key,),
            "ensure 失败后身份投影"), (0,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT COUNT(*) FROM session_tokens WHERE session_key=?", (key,),
            "ensure 失败后凭证"), (0,))
        self.assertEqual(self._event_count("session.ensured"), before_events,
                         "ensure 失败后不该有 session.ensured")

        res = fish.ensure_session(self.conn, session_key=key, host="cli",
                                  evidence="self-reported", parent_session="no-such-parent")
        self.assertEqual(self._count("citizens"), before_citizens + 1, "重试只该建一个公民")
        self.assertEqual(self._event_count("session.ensured"), before_events + 1,
                         "重试只该写一条 session.ensured")
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM session_identity WHERE session_key=?", (key,))[0], 1)
        self.assertIsNotNone(res["token"], "重试必须签发凭证")

    def test_end_session_failure_rolls_back_revocation_and_stints(self):
        """end 失败注入：吊销/关档期/活跃时间全部回滚。"""
        res = fish.start_session(self.conn, alias="atom-end", environ={}, project="ATOM")
        token = res["token"]
        cid = res["citizen_id"]
        self.conn.commit()
        before_token = self._one("SELECT revoked_at FROM session_tokens WHERE token_hash=?",
                                 (res["token_hash"],))[0]
        before_stint = self._one("SELECT to_ts FROM project_stints WHERE citizen_id=? "
                                 "AND project='ATOM' AND to_ts IS NULL", (cid,))[0]
        before_seen = self._one("SELECT last_seen_at FROM citizens WHERE citizen_id=?",
                                (cid,))[0]
        before_events = self._event_count("session.closed")

        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.end_session(self.conn, token=token)

        self.assertEqual(self._same_state_both_connections(
            "SELECT revoked_at FROM session_tokens WHERE token_hash=?",
            (res["token_hash"],), "end 失败后 token 状态"), (before_token,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT to_ts FROM project_stints WHERE citizen_id=? AND project='ATOM' "
            "ORDER BY id DESC LIMIT 1", (cid,), "end 失败后档期"), (before_stint,))
        self.assertEqual(self._same_state_both_connections(
            "SELECT last_seen_at FROM citizens WHERE citizen_id=?", (cid,),
            "end 失败后活跃时间"), (before_seen,))
        self.assertEqual(self._event_count("session.closed"), before_events)

        again = fish.end_session(self.conn, token=token)
        self.assertGreaterEqual(again["revoked"], 1, "重试必须真的吊销")
        self.assertIsNotNone(self._one(
            "SELECT revoked_at FROM session_tokens WHERE token_hash=?",
            (res["token_hash"],))[0], "重试后 token 必须已吊销")
        self.assertEqual(self._event_count("session.closed"), before_events + 1)


class RenameAtomicityTest(_Base):
    """`rename_citizen` 的名字与 `citizen.renamed` 必须同一事务（W2 评审 🟠 残留）。"""

    def test_rename_failure_rolls_back_name_and_event(self):
        cid = self._citizen("旧名")
        before = self._one("SELECT display_name FROM citizens WHERE citizen_id=?", (cid,))[0]
        with self._fail_append(fish_session):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish_session.rename_citizen(self.conn, cid, "新名", actor=cid)
        got = self._same_state_both_connections(
            "SELECT display_name FROM citizens WHERE citizen_id=?", (cid,), "改名失败后名字")
        self.assertEqual(got[0], before, "事件失败时名字必须回滚")
        self.assertEqual(self._event_count("citizen.renamed"), 0)

    def test_rename_success_commits_both(self):
        cid = self._citizen("旧名")
        out = fish_session.rename_citizen(self.conn, cid, "新名", actor=cid)
        self.assertTrue(out["changed"])
        self.assertEqual(self._one("SELECT display_name FROM citizens WHERE citizen_id=?",
                                   (cid,))[0], "新名")
        self.assertEqual(self._event_count("citizen.renamed"), 1)


class MemoryAtomicityTest(_Base):
    """记忆写路径：record_access / supersede / withdraw。"""

    def test_record_access_failure_rolls_back_counters(self):
        code = self._mem("会被访问")
        before = self._one("SELECT access_count,last_accessed_at FROM code_index WHERE code=?",
                           (code,))
        self.assertEqual(before[0], 0)
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.record_access(self.conn, [code], "recall")
        self.assertEqual(self._same_state_both_connections(
            "SELECT access_count,last_accessed_at FROM code_index WHERE code=?",
            (code,), "record_access 失败后计数"), before)
        self.assertEqual(self._event_count("memory.accessed"), 0)

        fish.record_access(self.conn, [code], "recall")
        after = self._one("SELECT access_count FROM code_index WHERE code=?", (code,))[0]
        self.assertEqual(after, 1, "重试只该加一次")
        self.assertEqual(self._event_count("memory.accessed"), 1, "重试只该写一条事件")

    def test_supersede_failure_rolls_back_status_and_link(self):
        old = self._mem("旧结论")
        new = self._mem("新结论")
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.supersede_memory(self.conn, old, new, reason="测试")
        self.assertEqual(self._same_state_both_connections(
            "SELECT status,superseded_by FROM code_index WHERE code=?", (old,),
            "supersede 失败后旧编码"), ("active", None))
        self.assertEqual(self._event_count("memory.superseded"), 0)
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM links WHERE src=? AND dst=? AND rel='supersedes'",
            (old, new))[0], 0, "失败后不该有 supersedes 边")

        fish.supersede_memory(self.conn, old, new, reason="测试")
        self.assertEqual(self._one("SELECT status,superseded_by FROM code_index WHERE code=?",
                                   (old,)), ("superseded", new))
        self.assertEqual(self._event_count("memory.superseded"), 1)
        self.assertEqual(self._one(
            "SELECT COUNT(*) FROM links WHERE src=? AND dst=? AND rel='supersedes'",
            (old, new))[0], 1, "重试后取代边必须落库")

    def test_withdraw_failure_rolls_back_status(self):
        code = self._mem("会被作废")
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.withdraw_memory(self.conn, code, reason="测试")
        self.assertEqual(self._same_state_both_connections(
            "SELECT status FROM code_index WHERE code=?", (code,), "withdraw 失败后状态"),
            ("active",))
        self.assertEqual(self._event_count("memory.retired"), 0)

        fish.withdraw_memory(self.conn, code, reason="测试")
        self.assertEqual(self._one("SELECT status FROM code_index WHERE code=?", (code,)),
                         ("withdrawn",))
        self.assertEqual(self._event_count("memory.retired"), 1)


class OwnerAtomicityTest(_Base):
    """归属写路径：set_owner / fix_owner。"""

    def test_set_owner_failure_rolls_back_flags(self):
        a = self._citizen("主人A", is_owner=1)
        b = self._citizen("主人B", is_owner=0)
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.set_owner(self.conn, b)
        self.assertEqual(self._same_state_both_connections(
            "SELECT citizen_id FROM citizens WHERE is_owner=1", (),
            "set_owner 失败后主人"), (a,))
        self.assertEqual(self._count("events", "type='config.changed'"), 0)

        fish.set_owner(self.conn, b)
        self.assertEqual(self._one("SELECT citizen_id FROM citizens WHERE is_owner=1"), (b,))
        self.assertEqual(self._count("events", "type='config.changed'"), 1)

    def test_fix_owner_failure_rolls_back_owner(self):
        a = self._citizen("归属A")
        b = self._citizen("归属B")
        code = self._mem("归属会被修正")
        self.conn.execute("UPDATE code_index SET owner=? WHERE code=?", (a, code))
        self.conn.commit()
        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.fix_owner(self.conn, code, b, reason="测试")
        self.assertEqual(self._same_state_both_connections(
            "SELECT owner FROM code_index WHERE code=?", (code,), "fix_owner 失败后归属"),
            (a,))
        self.assertEqual(self._event_count("memory.owner_fixed"), 0)

        fish.fix_owner(self.conn, code, b, reason="测试")
        self.assertEqual(self._one("SELECT owner FROM code_index WHERE code=?", (code,)), (b,))
        self.assertEqual(self._event_count("memory.owner_fixed"), 1)


class CliAtomicityTest(_Base):
    """CLI 写路径：cmd_forgetting（循环 append_event 的代表）。"""

    def test_cmd_forgetting_failure_rolls_back_dormant_flags(self):
        code = self._mem("会被遗忘")
        old = "2026-08-01T00:00:00Z"       # 距 2026-11-25 约 116 天 → fact 60d 半衰期 → dormant
        self.conn.execute("UPDATE code_index SET last_accessed_at=?, recorded_at=?, dormant=0 "
                          "WHERE code=?", (old, old, code))
        self.conn.commit()
        args = types.SimpleNamespace(root=str(self.root), apply=True)

        with self._fail_append(fish):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                with self._quiet():
                    fish.cmd_forgetting(args)
        gc.collect()

        self.assertEqual(self._same_state_both_connections(
            "SELECT dormant FROM code_index WHERE code=?", (code,),
            "cmd_forgetting 失败后 dormant"), (0,))
        self.assertEqual(self._event_count("memory.dormant"), 0)

        with self._quiet():
            fish.cmd_forgetting(args)
        self.assertEqual(self._one("SELECT dormant FROM code_index WHERE code=?", (code,)),
                         (1,), "重试后必须标记休眠")
        self.assertEqual(self._event_count("memory.dormant"), 1, "重试只该写一条事件")


class ConfigAtomicityTest(_Base):
    """配置写路径在 fish_db 层：set_config。"""

    def test_set_config_failure_rolls_back_value(self):
        old = fish.get_config(self.conn, "timezone")      # 缺省值也是"写前状态"
        with self._fail_append(fish_db):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish.set_config(self.conn, "timezone", "UTC")
        self.assertEqual(fish.get_config(self.conn, "timezone"), old,
                         "set_config 失败后配置值必须回到写前")
        other = fish.connect(self.root)
        try:
            self.assertEqual(fish.get_config(other, "timezone"), old,
                             "set_config 失败后另一条连接也不该看到新值")
        finally:
            other.close()
        self.assertEqual(self._count("events", "type='config.changed'"), 0)

        fish.set_config(self.conn, "timezone", "UTC")
        self.assertEqual(self._one("SELECT value FROM config WHERE key='timezone'"), ("UTC",))
        self.assertEqual(self._count("events", "type='config.changed'"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
