#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4.6 · WorkBuddy 身份路径（113 §7 · J-W46-1/2 · 2026-11-25）。

## 这一批判据守什么

`fish_session_hook.py`（WorkBuddy 的 SessionStart）从"只查不签"改成"查不到就
`session ensure`"之后，三支行为必须都成立，且**都不许轮换**：

  · **继承**：payload 的 `transcript_path` 反解出父 → 子继承父公民，
    `session_identity.origin='inherited'`、凭证 `inherited`/`parent`、`parent_token` 指父；
  · **幂等**：重跑 SessionStart → 同一条凭证（`token_source='existing'`），
    不许互踢、不许"同会话两公民"；
  · **provisional**：宿主说这是子代理（有 `agent_id`）却真判不到父 → `provisional`，
    **绝不造 machine**（113 §7.1「宁可不推，不串推」）。

## 为什么键口径不许改（J-W46-1 的机制）

推送侧用 `fish_activity.make_session_key(host, payload['session_id'])` 解析公民
（`fish_push_hook.py:207-208` / `fish.py::cmd_prompt_push`）。hook 若把身份签到别的键上
（比如 `agent-<uuid>`），推送行照样"无身份"——**判定必须调应用自己的函数**，
所以本文件直接拿 `make_session_key` 对拍，不另写一份推导。

实测（2026-11-25T00:3xZ，只读）：7d 内 WB prompt 推送的 12 个会话键**全部**是
宿主会话目录名，没有一个 `agent-*` —— 键口径改了就会与推送侧分叉。

## 三条写法纪律

1. 每个判据一个临时库（`tempfile.mkdtemp`），生产 `p0/data` 一个字节都不碰；
2. 变异在同一文件里（`MutationTest`）：把 hook 的动词换回 `start` → 同一组断言必须红；
3. 不锚固定数字：只锚关系（公民数 / 活凭证 / evidence 分级 / 父键）。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish           # noqa: E402
import fish_activity  # noqa: E402

PY = sys.executable
FISH_PY = HERE / "fish.py"
HOOK_PY = HERE / "fish_session_hook.py"
PROD_DB = HERE / "data" / "index.sqlite"      # 生产库（只读；只有回放判据会碰它）

# 去掉宿主会话变量后的环境（与 conftest 同一套口径；子进程用）
_DROP = {"DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID",
         "CODEX_SESSION_ID", "FISH_SESSION_ACTOR", "FISH_ACTOR",
         "FISH_SESSION_TOKEN", "FISH_SESSION_KEY"}


def clean_env(**over):
    env = {k: v for k, v in os.environ.items() if k not in _DROP}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: str(v) for k, v in over.items()})
    return env


def wb_transcript(parent, agent="11111111-2222-3333-4444-555555555555"):
    """WB 子代理 transcript 的真实形态（113 §7.1）：`<父>/subagents/agent-<uuid>.jsonl`。"""
    return f"C:/wb/projects/i-app-ws/{parent}/subagents/agent-{agent}.jsonl"


class _WbBase(unittest.TestCase):
    """临时库夹具：数据目录、WB 根、DSH 会话根全部指向临时目录（可复现）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w46-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.env = clean_env(
            FISH_DATA=str(self.data),
            # 血缘兜底会去读宿主树：指到临时（空）根，否则本判据会读到这台机器真 WB
            FISH_WB_HOME=str(self.tmp / "wb"),
            FISH_CORPUS_DSH_SESSIONS=str(self.tmp / "sessions"),
        )
        self._conns = []

    def tearDown(self):
        for conn in self._conns:
            try:
                conn.close()
            except Exception:      # noqa: BLE001 —— 收尾不掩盖真正的失败
                pass

    # ------------------------------------------------------------ 装配 / 运行
    def conn(self):
        conn = fish.connect(self.data)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        self._conns.append(conn)
        return conn

    def hook(self, payload, **env):
        """真跑 hook（不是调函数）：判据要看的是这条链路，不是某个函数。"""
        proc = subprocess.run([PY, str(HOOK_PY)], input=json.dumps(payload),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env={**self.env, **env}, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"SessionStart hook 必须恒 0（失败也不许破坏会话）：{proc.stderr[-400:]}")
        return proc

    def context(self, proc):
        self.assertTrue(proc.stdout.strip(), "hook 必须输出协议块")
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    # ------------------------------------------------------------ 读数
    @staticmethod
    def citizens(conn):
        return conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]

    @staticmethod
    def placeholder(conn):
        """`未命名%` 占位公民（J4 盯的就是这一列）。"""
        return conn.execute(
            "SELECT COUNT(*) FROM citizens WHERE display_name LIKE '未命名%'").fetchone()[0]

    @staticmethod
    def machine_evidence(conn):
        """`evidence='machine'` 的凭证行数（J-W46-2：子代理路径不许把它抬高）。"""
        return conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE evidence='machine'").fetchone()[0]

    @staticmethod
    def identity(conn, key):
        return conn.execute(
            "SELECT citizen_id,origin,inherited_from,evidence FROM session_identity "
            "WHERE session_key=?", (key,)).fetchone()

    @staticmethod
    def tokens(conn, key):
        return conn.execute(
            "SELECT token_hash,citizen_id,evidence,channel,parent_token,revoked_at "
            "FROM session_tokens WHERE session_key=? ORDER BY issued_at DESC", (key,)).fetchall()

    def live(self, conn, key):
        return conn.execute(
            "SELECT COUNT(*) FROM session_tokens WHERE session_key=? AND revoked_at IS NULL",
            (key,)).fetchone()[0]

    def notes(self):
        path = self.data / "_audit" / "hook-calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["note"] for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ============================================================ ① 继承 / ② 幂等


class InheritTest(_WbBase):
    """J-W46-1 的继承支：父有身份 → 子继承，**公民数不变**。"""

    PARENT, CHILD = "parent-A", "child-B"

    def _parent(self):
        conn = self.conn()
        out = fish.start_session(conn, environ={"CODEBUDDY_SESSION_ID": self.PARENT})
        return out, "codebuddy:" + self.PARENT

    def test_child_inherits_parent_citizen(self):
        parent, pkey = self._parent()
        conn = self.conn()
        before = self.placeholder(conn)
        citizens_before = self.citizens(conn)

        proc = self.hook({"session_id": self.CHILD, "cwd": "i:/proj",
                          "agent_id": "store-xyz",
                          "transcript_path": wb_transcript(self.PARENT)})
        self.assertIn("injected", " ".join(self.notes()), "hook 必须真的走完注入链")

        ckey = "codebuddy:" + self.CHILD
        row = self.identity(conn, ckey)
        self.assertIsNotNone(row, "子会话必须有 session_identity 行")
        self.assertEqual(row[0], parent["citizen_id"], "子会话必须继承父公民，不许新建")
        self.assertEqual(row[1], "inherited", "origin 必须是 inherited")
        self.assertEqual(row[2], pkey, "inherited_from 必须指向父键")
        self.assertEqual(row[3], "inherited", "session_identity.evidence 必须同步")
        token = self.tokens(conn, ckey)[0]
        self.assertEqual(token[2], "inherited", "子凭证 evidence 必须是 inherited")
        self.assertEqual(token[3], "parent", "子凭证 channel 必须是 parent")
        self.assertEqual(token[4], parent["token_hash"], "parent_token 必须指向父的 hash")
        self.assertEqual(self.citizens(conn), citizens_before, "继承不该新增公民")
        self.assertEqual(self.placeholder(conn), before, "继承不该新增未命名占位公民")
        self.assertEqual(self.live(conn, pkey), 1, "父凭证必须还在")
        self.assertEqual(self.live(conn, ckey), 1, "子会话必须有一条活凭证")
        self.assertIn("identity=inherited", " ".join(self.notes()),
                      "审计行要能回看这次是继承来的")

    def test_rerun_is_idempotent_and_never_rotates(self):
        self._parent()
        payload = {"session_id": self.CHILD, "cwd": "i:/proj",
                   "agent_id": "store-xyz",
                   "transcript_path": wb_transcript(self.PARENT)}
        self.hook(payload)
        conn = self.conn()
        ckey = "codebuddy:" + self.CHILD
        first = self.tokens(conn, ckey)
        self.assertEqual(len(first), 1, "首次 ensure 只许签一条")

        self.hook(payload)
        second = self.tokens(conn, ckey)
        self.assertEqual(len(second), 1, "重跑 SessionStart 不许再签（更不许轮换）")
        self.assertEqual(second[0][0], first[0][0], "必须是**同一条**凭证（token_hash 相同）")
        self.assertEqual(self.live(conn, ckey), 1)
        self.assertEqual(self.citizens(conn), 1, "重跑不许造出第二个公民")
        self.assertIn("existing", " ".join(self.notes()),
                      "第二次必须走只读分支（existing），不是又签一次")

        # 直接打 CLI 的 `existing` 分支（ensure 的契约面：不重签、不打印明文）
        proc = subprocess.run([PY, str(FISH_PY), "--root", str(self.data),
                               "session", "ensure"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace",
                              env=clean_env(FISH_DATA=str(self.data),
                                            CODEBUDDY_SESSION_ID=self.CHILD,
                                            FISH_WB_HOME=str(self.tmp / "wb")))
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("token_source: existing", proc.stdout)
        self.assertNotIn("token:", proc.stdout, "复用时不许再打印明文 token")

    def test_parent_without_identity_is_ensured_first(self):
        """父无身份 → 先 ensure 父（钩子做的），子再继承；**全程只 +1 个公民**。"""
        conn = self.conn()
        before = self.citizens(conn)
        pkey = "codebuddy:" + self.PARENT
        self.assertIsNone(self.identity(conn, pkey))

        self.hook({"session_id": self.CHILD, "cwd": "i:/proj",
                   "transcript_path": wb_transcript(self.PARENT)})
        prow = self.identity(conn, pkey)
        self.assertIsNotNone(prow, "父会话必须被先 ensure 出来")
        self.assertEqual(prow[1], "direct", "父是根会话 → direct")
        self.assertEqual(prow[3], "machine", "父的凭证来自机器事实 → machine")
        ckrow = self.identity(conn, "codebuddy:" + self.CHILD)
        self.assertEqual(ckrow[1], "inherited")
        self.assertEqual(ckrow[0], prow[0], "子必须继承父刚建的那个公民")
        self.assertEqual(self.citizens(conn), before + 1,
                         "整条链只许 +1 个公民（父的），子不许再建")

    def test_walks_up_multi_level_chain_and_survives_cycle(self):
        """向上走：投影表给的父链 P→Q 由**远**及近 ensure；成环时不死循环。"""
        conn = self.conn()
        for child, parent in (("Q-1", "P-1"), ("P-1", "Q-1")):   # 故意成环（U 形回指）
            conn.execute(
                "INSERT INTO session_lineage (host,session_id,parent_session,origin,"
                "relation,basis,source,observed_at) VALUES "
                "('workbuddy',?,?,'subagent','spawn','path','wb:path',?)",
                (child, parent, fish.now_utc()))
        conn.commit()
        before = self.citizens(conn)
        proc = self.hook({"session_id": "deep-child", "cwd": "i:/proj",
                          "transcript_path": wb_transcript("Q-1")})
        self.assertEqual(proc.returncode, 0)
        q = self.identity(conn, "codebuddy:Q-1")
        p = self.identity(conn, "codebuddy:P-1")
        self.assertIsNotNone(q, "近端祖先必须被 ensure")
        self.assertIsNotNone(p, "远端祖先必须先被 ensure（由远及近）")
        self.assertEqual(q[1], "inherited", "近端祖先继承远端")
        self.assertEqual(q[2], "codebuddy:P-1")
        self.assertEqual(self.citizens(conn), before + 1,
                         "成环的链也只能有 1 个根公民（防环 + 只建一个）")
        child = self.identity(conn, "codebuddy:deep-child")
        self.assertEqual(child[1], "inherited")
        self.assertEqual(child[0], q[0])


# ============================================================ ③ provisional / 根


class ProvenanceTest(_WbBase):
    """J-W46-2：子代理路径落 provisional（不造 machine）；根会话才是 machine。"""

    def test_subagent_without_parent_is_provisional(self):
        conn = self.conn()
        machine_before = self.machine_evidence(conn)
        before = self.citizens(conn)
        self.hook({"session_id": "orphan-C", "cwd": "i:/proj", "agent_id": "store-abc"})
        ckey = "codebuddy:orphan-C"
        row = self.identity(conn, ckey)
        self.assertEqual(row[1], "provisional", "判不到父的子代理必须落 provisional")
        self.assertEqual(row[3], "provisional")
        token = self.tokens(conn, ckey)[0]
        self.assertEqual(token[2], "provisional", "凭证不许标 machine（J-W46-2）")
        self.assertEqual(self.machine_evidence(conn), machine_before,
                         "子代理路径**不许**抬高 machine 计数")
        self.assertEqual(self.citizens(conn), before + 1, "provisional 仍是一个公民（可逆）")

    def test_transcript_outside_subagents_is_provisional(self):
        """路径不在 `subagents/` 下 → 判不到父（不许兜回 main/root）。"""
        conn = self.conn()
        machine_before = self.machine_evidence(conn)
        self.hook({"session_id": "stray-D", "agent_id": "store-def",
                   "transcript_path": "C:/wb/projects/i-app-ws/stray-D/sessions/stray-D.jsonl"})
        row = self.identity(conn, "codebuddy:stray-D")
        self.assertEqual(row[1], "provisional")
        self.assertEqual(self.machine_evidence(conn), machine_before)

    def test_root_session_is_machine(self):
        """根会话（无 agent_id、无 transcript_path）：机器事实 → direct/machine。"""
        conn = self.conn()
        self.hook({"session_id": "root-E", "cwd": "i:/proj"})
        row = self.identity(conn, "codebuddy:root-E")
        self.assertEqual(row[1], "direct")
        self.assertEqual(row[3], "machine")
        self.assertEqual(self.tokens(conn, "codebuddy:root-E")[0][2], "machine")

    def test_self_parent_collapses_to_root(self):
        """`<本会话 id>/subagents/agent-x.jsonl`：父就是自己 → 按根会话（路径证明它是会话目录）。"""
        conn = self.conn()
        self.hook({"session_id": "session-F", "cwd": "i:/proj",
                   "transcript_path": wb_transcript("session-F")})
        row = self.identity(conn, "codebuddy:session-F")
        self.assertEqual(row[1], "direct", "自指不许当继承，也不许降成 provisional")
        self.assertEqual(row[3], "machine")
        self.assertIsNone(self.identity(conn, "codebuddy:session-F")[2])

    def test_existing_identity_is_a_pure_read(self):
        """已有身份 → 稳态**零写**（hook 只查）；连审计 note 都要写 existing。"""
        conn = self.conn()
        fish.start_session(conn, environ={"CODEBUDDY_SESSION_ID": "known-G"})
        tokens_before = conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0]
        notes_before = len(self.notes())
        self.hook({"session_id": "known-G", "cwd": "i:/proj"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM session_tokens").fetchone()[0],
                         tokens_before, "已有身份时 hook 不许签发任何东西")
        self.assertGreater(len(self.notes()), notes_before, "hook 必须留审计痕")
        self.assertIn("existing", self.notes()[-1])


# ============================================================ 键口径（J-W46-1 的机制）


class KeyContractTest(_WbBase):
    """钩子签的键必须**等于**推送侧解析公民的键，否则推送仍然"无身份"。"""

    def test_session_key_uses_payload_session_id(self):
        import fish_session_hook as hookmod
        payload = {"session_id": "abc-123", "agent_id": "store-zzz",
                   "transcript_path": wb_transcript("parent-Z")}
        self.assertEqual(hookmod.session_key_from(payload), "codebuddy:abc-123",
                         "键口径不许改（113 §7.1）；改了推送侧就查不到同一个键")
        self.assertEqual(hookmod.session_key_from({"agent_id": "store-zzz"}),
                         None, "没有 session_id 时不许编一个键")
        # 判定调**应用自己的函数**（推送侧就是它），不另写一份推导
        self.assertEqual(fish_activity.make_session_key("workbuddy", "abc-123"),
                         "codebuddy:abc-123")
        self.assertEqual(fish_activity.make_session_key("workbuddy", "abc-123"),
                         hookmod.session_key_from(payload))

    def test_cli_accepts_parent_and_agent(self):
        proc = subprocess.run([PY, str(FISH_PY), "session", "ensure", "--help"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=clean_env(FISH_DATA=str(self.data)))
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("--parent-session", proc.stdout)
        self.assertIn("--agent-id", proc.stdout)
        src = (FISH_PY).read_text(encoding="utf-8")
        self.assertIn("parent_session=None, agent_id=None", src,
                      "ensure_session 必须收这两个参数（不碰 start/rotate 的语义）")

    def test_hook_docstring_keeps_history_and_explains_why_ensure(self):
        src = HOOK_PY.read_text(encoding="utf-8")
        self.assertIn("会轮换", src, "旧理由（为什么不调 start）必须留档，不许删")
        self.assertIn("绝不轮换", src, "新理由必须写清 ensure 的语义")
        self.assertTrue(any(t in src for t in ("243/317", "243", "238")),
                        "必须留下 7d 无身份行这个动机读数")
        self.assertIn("path_parent", src, "反解只有一处实现（不写第二份正则）")
        self.assertNotRegex(src, r"subagents.*\[\^", "hook 里不许再出现第二份 transcript 正则")

    def test_push_gate_default_is_untouched(self):
        """W4.7 才开闸门：本波不许把 `push.no_identity` 默认值改成 skip。"""
        conn = self.conn()
        self.assertEqual(fish.get_config(conn, "push.no_identity"), "allow",
                         "W4.6 不许动闸门默认值（W4.7 的事）")

    def test_session_start_still_rotates(self):
        """`start` 的轮换语义一个字节不改（ensure 的存在不改变人驱动那条路）。"""
        conn = self.conn()
        first = fish.start_session(conn, environ={"CODEBUDDY_SESSION_ID": "rot-1"})
        second = fish.start_session(conn, environ={"CODEBUDDY_SESSION_ID": "rot-1"})
        rows = self.tokens(conn, "codebuddy:rot-1")
        self.assertEqual(len(rows), 2, "start 必须换一条新凭证")
        self.assertEqual(self.live(conn, "codebuddy:rot-1"), 1, "旧凭证必须被吊销")
        self.assertEqual(first["citizen_id"], second["citizen_id"], "公民不变")


# ============================================================ 变异（J4 必须红）


class MutationTest(_WbBase):
    """变异：把 hook 的 ensure 退回 `start` → 同会话两公民（未命名 +1）→ J4 必红。"""

    def setUp(self):
        super().setUp()
        import fish_session_hook as hookmod
        self.hookmod = hookmod
        self._old_data = hookmod.DATA
        self._old_verb = hookmod.ENSURE_VERB
        hookmod.DATA = self.data        # 进程内调用也要落临时库，绝不碰生产
        self.addCleanup(setattr, hookmod, "DATA", self._old_data)
        self.addCleanup(setattr, hookmod, "ENSURE_VERB", self._old_verb)

    def _seed_parent(self):
        conn = self.conn()
        return fish.start_session(conn, environ={"CODEBUDDY_SESSION_ID": "mut-parent"})

    def test_control_ensure_does_not_add_citizen(self):
        """对照组必须绿：ensure + 显式父 → 继承，公民数不变。"""
        self._seed_parent()
        conn = self.conn()
        before = self.placeholder(conn)
        got = self.hookmod._ensure_via_cli("mut-child", parent="mut-parent")
        self.assertTrue(got, "ensure 必须成功")
        self.assertEqual(got.get("identity"), "inherited")
        self.assertEqual(self.placeholder(conn), before, "ensure 不该新增未命名公民（J4）")

    def test_mutation_ensure_verb_to_start_creates_second_citizen(self):
        self._seed_parent()
        conn = self.conn()
        before = self.placeholder(conn)
        machine_before = self.machine_evidence(conn)
        self.hookmod.ENSURE_VERB = "start"      # ← 变异：退回轮换口径（旧 hook 的行为）
        got = self.hookmod._ensure_via_cli("mut-orphan")   # 旧 hook 只传 session_id
        self.assertTrue(got, "start 也必须能跑通（否则变异不成立，判据没有靶点）")
        grown = self.placeholder(conn) - before
        self.assertEqual(grown, 1, "变异没生效：start 没给该会话建新公民")
        self.assertGreater(self.machine_evidence(conn), machine_before,
                           "变异没生效：start 的凭证必须是 machine")
        with self.assertRaises(AssertionError, msg="J4 失去判别力：同会话两公民没被抓住"):
            self.assertEqual(self.placeholder(conn) - before, 0, "继承不该新增未命名公民（J4）")


# ============================================================ 验收回放（只读生产库）


class AcceptanceReplayTest(_WbBase):
    """J-W46-1 的回放面：7d 内 WB prompt 推送的**每一个会话键**，现在都能拿到身份。

    生产库**只读**：读 7d 推送键 → 在临时库里按根会话形态跑一遍 hook →
    断言每个键都能解析出公民（推送侧用的就是 `resolve_citizen`，不另写判定）。
    真实"0 行"要等上线后的新推送；这里证明的是**机制**：键对得上、身份签得出。
    """

    def _prod(self):
        if not PROD_DB.is_file():
            self.skipTest(f"这棵树没有生产库（{PROD_DB}）——回放判不到")

        return sqlite3.connect("file:" + str(PROD_DB) + "?mode=ro", uri=True)

    def test_every_7d_wb_push_key_resolves_after_session_start(self):
        prod = self._prod()
        try:
            rows = prod.execute(
                "SELECT session_key, COUNT(*), "
                "SUM(CASE WHEN citizen_id IS NULL OR citizen_id='' THEN 1 ELSE 0 END) "
                "FROM push_log WHERE session_key LIKE 'codebuddy:%' "
                "AND pushed_at >= datetime('now','-7 day') GROUP BY 1").fetchall()
        except sqlite3.Error as exc:
            self.skipTest(f"生产库读不到 push_log：{exc}")
        finally:
            prod.close()
        if not rows:
            self.skipTest("7d 窗口里没有 WB prompt 推送样本——回放无靶点")
        keys = [r[0] for r in rows]
        blocked = sum(int(r[2] or 0) for r in rows)
        self.assertTrue(blocked, f"7d 内本来没有'无身份'行（{blocked}）——回放失去意义")

        conn = self.conn()
        missing = []
        for key in keys:
            sid = key.split(":", 1)[1]
            self.hook({"session_id": sid, "cwd": "i:/proj"})   # 根会话形态 = 修复后的首启
            if fish_activity.resolve_citizen(conn, key) is None:
                missing.append(key)
        self.assertEqual(missing, [],
                         f"这些键在 SessionStart 之后仍然解析不到公民（推送仍会'无身份'）："
                         f"{missing}")
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM session_tokens WHERE session_key LIKE 'codebuddy:%' "
                "AND evidence='machine' AND revoked_at IS NULL").fetchone()[0],
            len(keys), "每个键恰好一条机器凭证（不多不少）")
        print(f"\n[W4.6 回放] 7d WB prompt 推送：{sum(int(r[1]) for r in rows)} 行 / "
              f"{len(keys)} 个会话键，其中无身份 {blocked} 行 → "
              f"一次 SessionStart 后剩余无身份键 {len(missing)} 个")


if __name__ == "__main__":
    unittest.main(verbosity=2)
