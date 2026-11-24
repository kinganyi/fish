#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4 · 身份 ensure / 继承（113 §5.4 / §5.8 · J-W4E / J-D5 / J4 · 2026-11-25）。

## 这一批判据守什么

`session_identity`（N 个会话键 → 1 个公民）的**解析**：子会话沿父链继承最近一个
有身份的祖先，**不造新公民**；`ensure` 幂等、**绝不轮换**；结束子会话不伤父会话。

## 三条写法纪律（与 110/113 的判据纪律同源）

1. **每个判据一个临时库**（`tempfile.mkdtemp`），绝不碰生产 `p0/data`；
2. **变异在同一文件里**：mock 换掉一环 → 同一组断言必须**红**（`assertRaises(AssertionError)`）。
   只证明"绿"的判据不算判据——它可能只是读空；
3. **不锚固定数字**：只锚关系（公民数 / 活凭证数 / evidence 分级 / 祖先键）。

## 判据与小节

  J-W4E   子会话 end 不伤父（含两个变异）
  J-D5    4 层链只有 1 个公民；子全指向根；无身份链落 provisional 而不是静默 machine
  J4      假公民停止新增（未命名·dsh#% 计数不变）
  W4.0    触发路径：`session ensure` CLI + DSH hook 只喂 session_id 也能继承
"""
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish           # noqa: E402
import fish_activity  # noqa: E402
import fish_lineage   # noqa: E402

PY = sys.executable
FISH_PY = str(HERE / "fish.py")
HOOK_PY = str(HERE / "fish_dsh_hook.py")


def clean_env(**over):
    """去掉宿主会话变量后的环境（与 conftest 同一套口径；子进程用）。"""
    drop = {"DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID",
            "CODEX_SESSION_ID", "FISH_SESSION_ACTOR", "FISH_ACTOR",
            "FISH_SESSION_TOKEN", "FISH_SESSION_KEY"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: str(v) for k, v in over.items()})
    return env


def link(conn, child, parent):
    """把一个会话投影成"有父"（W2 的产物形状；本判据不测 classify，只喂父链）。"""
    conn.execute(
        "INSERT INTO session_lineage (host,session_id,parent_session,origin,relation,"
        "basis,source,observed_at) VALUES ('dsh',?,?,'subagent','spawn','header',"
        "'dsh:header',?) ON CONFLICT(host,session_id) DO UPDATE SET "
        "parent_session=excluded.parent_session",
        (child, parent, fish.now_utc()))
    conn.commit()


def frame_bytes(events):
    """若干事件压成一个 zstd 帧（真实 DSH 文件的形状；与 test_lineage_dsh 同一手法）。"""
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    import zstandard
    return zstandard.ZstdCompressor().compress(body.encode("utf-8"))


def write_session_file(root, session_id, *, parent=None):
    """在 `<root>/<workspace>/<session_id>/session.v4.jsonl.zstd` 写一个会话头。"""
    event = {"type": "session", "version": 4, "id": session_id,
             "createdAt": 1_763_000_000_000, "cwd": "I:\\w",
             "agentPreset": "dafish-team"}
    if parent:
        event["parentSession"] = parent
        event["origin"] = "subagent"
        event["delegationDepth"] = 1
    path = Path(root) / "--ws--" / session_id / "session.v4.jsonl.zstd"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame_bytes([event]))
    return path


class _TempDb(unittest.TestCase):
    """临时库夹具：生产装配路径（connect + SCHEMA + migrate）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w4-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self._conns = []
        # 身份解析会**现读宿主会话头**（113 §5.7）：把根指到临时目录，
        # 否则本判据会读到这台机器真实的 DSH 会话（= 判据不可复现）。
        self._old_env = {k: os.environ.get(k) for k in ("FISH_CORPUS_DSH_SESSIONS",)}
        os.environ["FISH_CORPUS_DSH_SESSIONS"] = str(self.tmp / "sessions")
        (self.tmp / "sessions").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for conn in self._conns:
            try:
                conn.close()
            except Exception:      # noqa: BLE001 —— 收尾不掩盖真正的失败
                pass
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def conn(self):
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self._conns.append(conn)
        return conn

    # ------------------------------------------------------------ 读数
    @staticmethod
    def citizens(conn):
        return conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]

    @staticmethod
    def live_tokens(conn, session_key):
        return conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE session_key=? AND revoked_at IS NULL",
            (session_key,)).fetchone()[0]

    @staticmethod
    def identity_row(conn, session_key):
        return conn.execute(
            "SELECT citizen_id,origin,inherited_from,evidence FROM session_identity "
            "WHERE session_key=?", (session_key,)).fetchone()

    def row_for(self, conn, session_key):
        """该会话**自己的**令牌行（末次签发）。"""
        return conn.execute(
            "SELECT token_hash,citizen_id,session_key,evidence,channel,parent_token "
            "FROM session_tokens WHERE session_key=? ORDER BY issued_at DESC LIMIT 1",
            (session_key,)).fetchone()


class ChildInheritsTest(_TempDb):
    """J-W4E：子会话继承父公民；end 子会话不伤父会话。"""

    def _parent(self, conn, sid="w4-parent"):
        res = fish.start_session(conn, environ={"DSH_SESSION_ID": sid})
        return res, f"dsh:{sid}"

    def test_JW4E_child_inherits_and_end_does_not_hurt_parent(self):
        conn = self.conn()
        parent, pkey = self._parent(conn)
        link(conn, "w4-child", "w4-parent")
        before = self.citizens(conn)

        res = fish.ensure_session(conn, session_key="dsh:w4-child", host="dsh",
                                  evidence="machine")
        # ①② 子有父公民、父仍活
        self.assertEqual(res["citizen_id"], parent["citizen_id"],
                         "子会话必须继承父公民（不造第二个）")
        self.assertEqual(res["identity_origin"], "inherited", "解析来源必须是 inherited")
        self.assertEqual(res["inherited_from"], pkey, "inherited_from 必须指向最近的祖先键")
        self.assertEqual(res["created"], False, "继承不该创建公民")
        self.assertEqual(self.live_tokens(conn, pkey), 1, "父会话的活凭证必须还在")
        row = self.row_for(conn, "dsh:w4-child")
        self.assertEqual(row[3], "inherited", "子凭证的 evidence 必须是 inherited")
        self.assertEqual(row[4], "parent", "子凭证的 channel 必须是 parent")
        self.assertEqual(row[5], parent["token_hash"], "parent_token 必须指向父的 hash")
        # ④ citizens 计数不变
        self.assertEqual(self.citizens(conn), before, "继承不该新增公民")

        # ① 不抛 + ② 子键活凭证 0 + ③ 父键活凭证 1
        out = fish.end_session(conn, token=res["token"])
        self.assertEqual(out["revoked"], 1, "end 子会话应吊销它自己的那一条")
        self.assertEqual(self.live_tokens(conn, "dsh:w4-child"), 0)
        self.assertEqual(self.live_tokens(conn, pkey), 1, "结束子会话**不许**吊销父凭证")
        self.assertEqual(self.citizens(conn), before)
        # env 支（不带 token）也必须走得通：这正是"只认 host_session_key"时会抛的那条
        link(conn, "w4-child2", "w4-parent")
        again = fish.ensure_session(conn, session_key="dsh:w4-child2", host="dsh",
                                    evidence="machine")
        out2 = fish.end_session(conn, environ={"DSH_SESSION_ID": "w4-child2"})
        self.assertEqual(out2["citizen_id"], parent["citizen_id"])
        self.assertEqual(again["citizen_id"], parent["citizen_id"])

    def test_JW4E_ensure_is_idempotent_and_never_rotates(self):
        conn = self.conn()
        parent, pkey = self._parent(conn)
        link(conn, "w4-child", "w4-parent")
        first = fish.ensure_session(conn, session_key="dsh:w4-child", host="dsh",
                                    evidence="machine")
        self.assertEqual(first["token_source"], "issued")
        self.assertTrue(first["token"], "首次 ensure 必须给明文 token（hook 要打印）")
        tokens_before = conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0]

        second = fish.ensure_session(conn, session_key="dsh:w4-child", host="dsh",
                                     evidence="machine")
        self.assertEqual(second["token_source"], "existing", "第二次必须是复用")
        self.assertIsNone(second["token"], "复用时**拿不到明文**（明文只在签发时出现一次）")
        self.assertEqual(second["citizen_id"], parent["citizen_id"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0],
                         tokens_before, "重跑 ensure 不许签发第二条")
        self.assertEqual(self.live_tokens(conn, "dsh:w4-child"), 1, "更不许轮换")
        self.assertEqual(self.live_tokens(conn, pkey), 1, "父凭证不许被重跑 ensure 吊销")

    # ------------------------------------------------------------ 变异
    def test_JW4E_mutation_revoke_by_citizen_kills_parent(self):
        """变异 ①：吊销改 `citizen_id` 口径 → 父凭证一起失效 → J-W4E 的 ③ 必红。"""
        conn = self.conn()
        parent, pkey = self._parent(conn)
        link(conn, "w4-child", "w4-parent")
        res = fish.ensure_session(conn, session_key="dsh:w4-child", host="dsh",
                                  evidence="machine")

        def assert_parent_alive():
            self.assertEqual(self.live_tokens(conn, pkey), 1, "父会话的活凭证必须还在")

        # 坏实现：按公民吊销（110 §3.3 支 B 的实证：父凭证一并失效）
        fish.revoke_session_tokens(conn, citizen_id=parent["citizen_id"])
        with self.assertRaises(AssertionError, msg="变异没有被判据抓住：J-W4E 失去判别力"):
            assert_parent_alive()
        # 对照：恢复成正确口径（按会话）时同一断言必须绿
        conn.rollback()
        conn.execute("UPDATE session_tokens SET revoked_at=NULL WHERE token_hash=?",
                     (parent["token_hash"],))
        conn.commit()
        assert_parent_alive()
        self.assertTrue(res["token"])

    def test_JW4E_mutation_find_session_citizen_back_to_legacy(self):
        """变异 ②：`find_session_citizen` 退回"只认 host_session_key" → end 子必抛。"""
        conn = self.conn()
        self._parent(conn)
        link(conn, "w4-child", "w4-parent")
        fish.ensure_session(conn, session_key="dsh:w4-child", host="dsh", evidence="machine")

        legacy = fish.find_session_citizen

        def only_host_key(conn_, key, *, allow_identity=True):
            return legacy(conn_, key, allow_identity=False)

        with mock.patch.object(fish, "find_session_citizen", only_host_key):
            with self.assertRaises(ValueError, msg="只认 host_session_key 时 end 子会话该抛"):
                fish.end_session(conn, environ={"DSH_SESSION_ID": "w4-child"})


class FourLayerChainTest(_TempDb):
    """J-D5：4 层链 A→B→C→D，只有 1 个公民；无身份链不许静默 machine。"""

    def _chain(self, conn, *, register_root=True):
        if register_root:
            parent = fish.start_session(conn, environ={"DSH_SESSION_ID": "chain-A"})
        else:
            parent = None
        link(conn, "chain-B", "chain-A")
        link(conn, "chain-C", "chain-B")
        link(conn, "chain-D", "chain-C")
        return parent

    def test_JD5_four_layer_chain_one_citizen(self):
        conn = self.conn()
        parent = self._chain(conn)
        before = self.citizens(conn)
        out = {}
        for sid in ("chain-B", "chain-C", "chain-D"):
            out[sid] = fish.ensure_session(conn, session_key=f"dsh:{sid}", host="dsh",
                                           evidence="machine")
        self.assertEqual(self.citizens(conn), before,
                         f"4 层链只许有 1 个公民，实际 {self.citizens(conn)} 个")
        for sid in ("chain-B", "chain-C", "chain-D"):
            row = self.identity_row(conn, f"dsh:{sid}")
            self.assertIsNotNone(row, f"{sid} 必须有 session_identity 行")
            self.assertEqual(row[0], parent["citizen_id"],
                             f"{sid} 的 citizen_id 必须等于根的")
            self.assertEqual(row[1], "inherited", f"{sid} 的 origin 必须是 inherited")
        # 继承的是**最近**一个有身份的祖先（不是一律指向根键）
        self.assertEqual(self.identity_row(conn, "dsh:chain-B")[2], "dsh:chain-A")
        self.assertEqual(self.identity_row(conn, "dsh:chain-C")[2], "dsh:chain-B")
        self.assertEqual(self.identity_row(conn, "dsh:chain-D")[2], "dsh:chain-C")
        # 每层的证据强度：根是 machine，所以每层都是 inherited（不是 provisional）
        for sid in ("chain-B", "chain-C", "chain-D"):
            self.assertEqual(out[sid]["evidence"], "inherited")

    def test_JD5_chain_without_any_identity_is_provisional_not_machine(self):
        """整链无身份 → provisional；**不许静默落 machine**（J4 的代价要看得见）。"""
        conn = self.conn()
        self._chain(conn, register_root=False)
        b = fish.ensure_session(conn, session_key="dsh:chain-B", host="dsh",
                                evidence="machine")
        row = self.identity_row(conn, "dsh:chain-B")
        self.assertEqual(row[1], "provisional", "无身份祖先的会话身份必须是 provisional")
        self.assertEqual(row[3], "provisional", "session_identity.evidence 同步降级")
        self.assertEqual(b["evidence"], "provisional", "凭证也不许标 machine")
        self.assertEqual(b["identity_origin"], "provisional")
        # 孙：身份继承自 B（有身份了），但根的等级是 provisional → 凭证仍落 provisional
        c = fish.ensure_session(conn, session_key="dsh:chain-C", host="dsh",
                                evidence="machine")
        self.assertEqual(c["citizen_id"], b["citizen_id"], "孙继承 B 的公民")
        self.assertEqual(c["evidence"], "provisional",
                         "根的等级不是 machine 时，子凭证不许标 inherited（防漂白）")

    def test_JD5_root_session_is_direct(self):
        """根会话（无父）→ direct，凭证仍是 machine（今天的行为不变）。"""
        conn = self.conn()
        res = fish.ensure_session(conn, session_key="dsh:solo-root", host="dsh",
                                  evidence="machine")
        self.assertEqual(res["identity_origin"], "direct")
        self.assertEqual(res["evidence"], "machine")
        self.assertIsNone(res["inherited_from"])
        row = self.identity_row(conn, "dsh:solo-root")
        self.assertEqual(row[1], "direct")
        self.assertIsNone(row[2])

    # ------------------------------------------------------------ 变异
    def test_JD5_mutation_per_level_creates_citizen(self):
        """变异：关掉继承分支（每层各建公民）→ 计数变 4，J-D5 必红。"""
        conn = self.conn()
        self._chain(conn)
        before = self.citizens(conn)

        def no_inherit(conn_, *, host, session_key):
            return None, None

        with mock.patch.object(fish, "_nearest_identity_ancestor", no_inherit):
            for sid in ("chain-B", "chain-C", "chain-D"):
                fish.ensure_session(conn, session_key=f"dsh:{sid}", host="dsh",
                                    evidence="machine")
        grown = self.citizens(conn)
        self.assertGreater(grown, before, "变异没生效：还是没建新公民")
        self.assertEqual(grown, before + 3, "关掉继承后每层各建 1 个公民")
        with self.assertRaises(AssertionError, msg="J-D5 的『citizens 不变』失去判别力"):
            self.assertEqual(grown, before, "4 层链只许有 1 个公民")


class FakeCitizenTest(_TempDb):
    """J4：假公民停止新增（`未命名·dsh#%` 计数不变）。"""

    def _placeholder_count(self, conn):
        return conn.execute(
            "SELECT COUNT(*) FROM citizens WHERE host='dsh' "
            "AND display_name LIKE '未命名%'").fetchone()[0]

    def test_J4_inheritance_does_not_add_placeholder(self):
        conn = self.conn()
        fish.start_session(conn, environ={"DSH_SESSION_ID": "j4-parent"})
        link(conn, "j4-child", "j4-parent")
        link(conn, "j4-grand", "j4-child")
        before = self._placeholder_count(conn)
        fish.ensure_session(conn, session_key="dsh:j4-child", host="dsh", evidence="machine")
        fish.ensure_session(conn, session_key="dsh:j4-grand", host="dsh", evidence="machine")
        self.assertEqual(self._placeholder_count(conn), before,
                         "继承不该新增未命名公民（J4）")

    def test_J4_mutation_ensure_falls_back_to_start(self):
        """变异 ③：ensure 退回 `session start` → 同会话两公民（未命名 +1）→ J4 必红。"""
        conn = self.conn()
        fish.start_session(conn, environ={"DSH_SESSION_ID": "j4-parent"})
        link(conn, "j4-child", "j4-parent")
        before = self._placeholder_count(conn)
        # 坏实现：hook 仍调 start（start 走 find_or_create → 给子键建新公民）
        fish.start_session(conn, environ={"DSH_SESSION_ID": "j4-child"})
        after = self._placeholder_count(conn)
        self.assertEqual(after, before + 1, "变异没生效：start 没给子会话建新公民")
        with self.assertRaises(AssertionError, msg="J4 失去判别力："):
            self.assertEqual(after, before, "继承不该新增未命名公民（J4）")


class TriggerPathTest(_TempDb):
    """W4.0：父链只能从**会话头**读（113 §1-9 / §5.7），投影表没填也必须能继承。"""

    def test_header_path_inherits_without_projection(self):
        conn = self.conn()
        parent = fish.start_session(conn, environ={"DSH_SESSION_ID": "hdr-parent"})
        sessions = self.tmp / "sessions"
        write_session_file(sessions, "hdr-child", parent="hdr-parent")
        lineage_before = conn.execute("SELECT COUNT(*) FROM session_lineage").fetchone()[0]
        res = fish.ensure_session(conn, session_key="dsh:hdr-child", host="dsh",
                                  evidence="machine")
        self.assertEqual(res["citizen_id"], parent["citizen_id"],
                         "投影表没有行时，必须现读会话头判父")
        self.assertEqual(res["identity_origin"], "inherited")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM session_lineage").fetchone()[0],
            lineage_before, "ensure 是热路径：不许顺手写投影表（写投影只走 upsert_node）")

    def test_activity_resolve_citizen_third_level(self):
        """`fish_activity.resolve_citizen` 的第三级：session_identity（113 §5.2）。"""
        conn = self.conn()
        parent = fish.start_session(conn, environ={"DSH_SESSION_ID": "act-parent"})
        link(conn, "act-child", "act-parent")
        res = fish.ensure_session(conn, session_key="dsh:act-child", host="dsh",
                                  evidence="machine")
        # 把子会话的凭证全部吊销 → 前两级都查不到，只剩 session_identity
        fish.revoke_session_tokens(conn, session_key="dsh:act-child")
        conn.commit()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM citizens_all WHERE host_session_key='dsh:act-child'").fetchone())
        self.assertEqual(fish_activity.resolve_citizen(conn, "dsh:act-child"),
                         parent["citizen_id"], "第三级必须命中 session_identity")
        # `#role` 尾：join 由 identity_key_of 统一剥离（113 §1-5）
        self.assertEqual(fish_activity.resolve_citizen(conn, "dsh:act-child#reviewer"),
                         parent["citizen_id"], "带 #role 的键必须剥掉尾巴再查")
        self.assertTrue(res["token"])

    def test_cli_session_ensure_contract(self):
        """`session ensure` 的 stdout 契约：首次有 token、重跑 token_source=existing。"""
        conn = self.conn()
        fish.start_session(conn, environ={"DSH_SESSION_ID": "cli-parent"})
        link(conn, "cli-child", "cli-parent")
        conn.close()
        env = clean_env()
        first = subprocess.run(
            [PY, FISH_PY, "--root", str(self.data), "session", "ensure"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**env, "DSH_SESSION_ID": "cli-child"})
        self.assertEqual(first.returncode, 0, f"stderr={first.stderr[:400]}")
        fields = {}
        for line in first.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip()
        self.assertTrue(fields.get("token"), f"首次 ensure 必须打印明文 token：{first.stdout[:300]}")
        self.assertEqual(fields.get("identity"), "inherited")
        self.assertEqual(fields.get("token_source"), "issued")
        second = subprocess.run(
            [PY, FISH_PY, "--root", str(self.data), "session", "ensure"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**env, "DSH_SESSION_ID": "cli-child"})
        self.assertEqual(second.returncode, 0, f"stderr={second.stderr[:400]}")
        self.assertNotIn("token:", second.stdout, "重跑 ensure 不许重签（更不许轮换）")
        self.assertIn("token_source: existing", second.stdout)

    def test_dsh_hook_uses_ensure_and_inherits(self):
        """W4.0 端到端：hook 只喂 session_id → 走 ensure → 继承父公民。"""
        conn = self.conn()
        parent = fish.start_session(conn, environ={"DSH_SESSION_ID": "hook-parent"})
        link(conn, "hook-child", "hook-parent")
        before = self.citizens(conn)
        env = clean_env(FISH_ROOT=str(self.data), FISH_DATA=str(self.tmp / "fishdata"))
        payload = json.dumps({"session_id": "hook-child", "cwd": "",
                              "hook_event_name": "SessionStart"})
        proc = subprocess.run([PY, HOOK_PY], input=payload, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(proc.returncode, 0, f"hook 必须恒 0：stderr={proc.stderr[:300]}")
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("inherited", ctx, f"hook 注入必须体现继承：{ctx[:300]}")
        self.assertEqual(self.citizens(conn), before, "hook 走 ensure → 不该新增公民")
        # 再跑一次：不许轮换（同会话活凭证仍是 1）
        proc2 = subprocess.run([PY, HOOK_PY], input=payload, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(proc2.returncode, 0)
        self.assertEqual(self.live_tokens(conn, "dsh:hook-child"), 1,
                         "重跑 SessionStart 不许轮换（ensure 幂等）")
        self.assertEqual(self.live_tokens(conn, "dsh:hook-parent"), 1)
        self.assertIn("已有活凭证", json.loads(proc2.stdout)
                      ["hookSpecificOutput"]["additionalContext"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
