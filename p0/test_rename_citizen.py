#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 B 判据：`rename_citizen` —— 名字认领。

背景（这是 BUG，不是增强）
--------------------------
用户开场说"你是 X"，系统里却没有任何机制把 X 绑到当前公民上：`display_name`
只在公民创建那一刻产生一次，此后全库 6 条 `UPDATE citizens` 没有一条碰它
（改的是 is_owner / last_seen_at / account_id / status）。于是用户指定的名字
只能以"另一条孤儿公民"的形式存在（`register` 建行但不绑定）——对用户而言，
"可被检索的名字"根本不存在。

判据
----
R1   改名后 display_name 变、**citizen_id 不变**
R1b  判别力：不改名时 R1 的核心断言不成立（"没实现 / 退回旧行为"会红）
R2   改名后 `find_citizen_by_name` 命中新名、旧名查不到
R3   历史记忆的 owner 不变（**名字不是身份**）
R4   同名幂等：changed=False 且**不写事件**
R5   空名 / 超长名 → ValueError
R6   重名允许，但 `same_name_exists` 报出另一个公民（决策 14：视作两个）
R7   写 `citizen.renamed` 事件，payload 含 from/to/actor/reason
R7b  判别力：把 `append_event` 变成 no-op → R7 的扫描必须为空
R8   凭证行不受影响（改名不动 `session_tokens`）
R9   非活跃公民拒绝改名

跑法
----
    python test_rename_citizen.py -v
"""
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

CID = "01RENAME000000000000001"
CID2 = "01RENAME000000000000002"
CID_ARCHIVED = "01RENAME000000000000003"


class RenameCitizenTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = fish.connect(self.root / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for prefix in ("PROJA",):
            self.conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
                "status) VALUES (?,?,NULL,0,'active')", (prefix, "甲项目"))
        for cid, name, st in ((CID, "dsh-session-315b", "active"),
                              (CID2, "占位乙", "active"),
                              (CID_ARCHIVED, "退役丙", "retired")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
                "created_at,is_owner) VALUES (?,?,?,?,?,?,?,0)",
                (cid, name, "visitor", None, "dsh", st, fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- 辅助
    def _name(self, cid):
        return self.conn.execute(
            "SELECT display_name FROM citizens WHERE citizen_id=?", (cid,)).fetchone()[0]

    def _rename_events(self):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM events WHERE type='citizen.renamed' ORDER BY seq")]

    def _mem(self, code, owner):
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant,"
            "session_actor,attachments) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL)",
            (code, "旧名时期写的", "PROJA", owner, "fact", "shared", "active",
             "[]", f"projects/PROJA/{code}.md", fish.now_utc(), fish.now_utc()))
        self.conn.commit()

    # ---------------------------------------------------------------- R1
    def test_R1_renames_display_name_keeps_citizen_id(self):
        """R1：名字变了，身份没变。"""
        res = fish.rename_citizen(self.conn, CID, "fish-主AI-2", reason="用户在开场指定")
        self.assertTrue(res["changed"])
        self.assertEqual(res["old_name"], "dsh-session-315b")
        self.assertEqual(res["new_name"], "fish-主AI-2")
        self.assertEqual(self._name(CID), "fish-主AI-2")
        # citizen_id 是同一个（没有被"删了重建"）
        self.assertEqual(res["citizen_id"], CID)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM citizens WHERE display_name='fish-主AI-2'").fetchone()[0], 1)

    # ---------------------------------------------------------------- R1b
    def test_R1b_judgement_can_go_red(self):
        """R1b 判别力：**不改名**时，R1 的核心断言必须不成立。

        等价于"实现退回旧行为（名字改不动）"时判据会红。两头都跑过才叫判据。
        """
        self.assertEqual(self._name(CID), "dsh-session-315b",
                         "改名前的名字就应该是旧的——若这里已经变了，说明夹具污染")
        self.assertNotEqual(self._name(CID), "fish-主AI-2",
                            "没调用改名却已经是新名——R1 的断言失去判别力")

    # ---------------------------------------------------------------- R2
    def test_R2_findable_by_new_name(self):
        """R2：改名后按新名查得到，按旧名查不到。"""
        fish.rename_citizen(self.conn, CID, "fish-主AI-2")
        self.assertIsNotNone(fish.find_citizen_by_name(self.conn, "fish-主AI-2"),
                             "新名字查不到——那『可被检索的名字』仍然是假的")
        self.assertIsNone(fish.find_citizen_by_name(self.conn, "dsh-session-315b"),
                          "旧名字还能查到——说明库里有两份名字（改名没生效或没落库）")

    # ---------------------------------------------------------------- R3
    def test_R3_history_owner_unchanged(self):
        """R3：历史记忆的 owner 仍是 citizen_id，不因改名而变。"""
        self._mem("PROJA-261025-01", owner=CID)
        fish.rename_citizen(self.conn, CID, "fish-主AI-2")
        owner = self.conn.execute(
            "SELECT owner FROM code_index WHERE code='PROJA-261025-01'").fetchone()[0]
        self.assertEqual(owner, CID, "改名把历史记忆的主人换掉了——名字不是身份")

    # ---------------------------------------------------------------- R4
    def test_R4_same_name_is_idempotent(self):
        """R4：改成同名 → changed=False 且不写事件。"""
        res = fish.rename_citizen(self.conn, CID, "dsh-session-315b")
        self.assertFalse(res["changed"])
        self.assertIsNone(res["event"])
        self.assertEqual(self._rename_events(), [], "同名也写了事件——噪声")

    # ---------------------------------------------------------------- R5
    def test_R5_rejects_empty_and_overlong(self):
        """R5：空名与超长名都必须响亮失败。"""
        with self.assertRaises(ValueError):
            fish.rename_citizen(self.conn, CID, "   ")
        with self.assertRaises(ValueError):
            fish.rename_citizen(self.conn, CID, "名" * 65)
        self.assertEqual(self._name(CID), "dsh-session-315b", "失败的调用不该留下半截改名")

    # ---------------------------------------------------------------- R6
    def test_R6_duplicate_allowed_but_reported(self):
        """R6：重名允许（决策 14），但必须把另一个报出来。"""
        fish.rename_citizen(self.conn, CID2, "fish-主AI-2")
        res = fish.rename_citizen(self.conn, CID, "fish-主AI-2")
        self.assertTrue(res["changed"], "重名被拒绝了——决策 14 说视作两个公民，不该拦")
        self.assertIn(CID2, res["same_name_exists"],
                      f"没报出同名公民；实际 {res['same_name_exists']}")

    # ---------------------------------------------------------------- R7
    def test_R7_writes_rename_event(self):
        """R7：写 `citizen.renamed`，payload 带 from/to/actor/reason。"""
        fish.rename_citizen(self.conn, CID, "fish-主AI-2",
                            actor="dsh-session-315b", reason="用户开场指定")
        ev = self._rename_events()
        self.assertEqual(len(ev), 1, f"应恰好 1 条改名事件，实际 {ev}")
        self.assertEqual(ev[0]["from"], "dsh-session-315b")
        self.assertEqual(ev[0]["to"], "fish-主AI-2")
        self.assertEqual(ev[0]["actor"], "dsh-session-315b")
        self.assertIn("开场", ev[0]["reason"])

    # ---------------------------------------------------------------- R7b 变异
    def test_R7b_mutation_without_event_scans_empty(self):
        """R7b 变异自证：把 `append_event` 变成 no-op，R7 的扫描必须为空。

        若这里仍能扫到事件，说明 R7 命中的不是"本次调用写的事件"（可能读到了
        别处的历史行），那 R7 就没有判别力。
        """
        with mock.patch.object(fish, "append_event"):
            res = fish.rename_citizen(self.conn, CID, "fish-主AI-2")
        self.assertTrue(res["changed"], "改名本身应该成功（变异只针对事件写入）")
        self.assertEqual(self._rename_events(), [],
                         "append_event 被架空后仍扫到事件——R7 的判据不可信")

    # ---------------------------------------------------------------- R8
    def test_R8_tokens_untouched(self):
        """R8：改名不动凭证（凭证属于会话，不属于名字）。"""
        self.conn.execute(
            "INSERT INTO session_tokens (token_hash,citizen_id,session_key,evidence,"
            "channel,parent_token,issued_at,expires_at,revoked_at) "
            "VALUES (?,?,?,?,?,NULL,?,?,NULL)",
            ("hash-a", CID, "dsh:session-x", "machine", "cli", fish.now_utc(), None))
        self.conn.commit()
        before = self.conn.execute(
            "SELECT token_hash,citizen_id,session_key FROM session_tokens").fetchall()
        fish.rename_citizen(self.conn, CID, "fish-主AI-2")
        after = self.conn.execute(
            "SELECT token_hash,citizen_id,session_key FROM session_tokens").fetchall()
        self.assertEqual(before, after, "改名动了凭证行——名字与身份必须解耦")

    # ---------------------------------------------------------------- R9
    def test_R9_rejects_inactive_citizen(self):
        """R9：非活跃公民拒绝改名。"""
        with self.assertRaises(ValueError):
            fish.rename_citizen(self.conn, CID_ARCHIVED, "新名")
        with self.assertRaises(ValueError):
            fish.rename_citizen(self.conn, "不存在的公民", "新名")


class RenameCitizenMcpTest(unittest.TestCase):
    """MCP 层的两条硬边界：**无凭证必须拒绝** / **只能改自己**。

    这两条不是"顺手加的防御"，而是这个 BUG 的根因边界：
      · 无凭证也允许改名 → 只能凭空建一条公民去承载名字，正是"孤儿公民"的来源
        （生产库 30 个公民里 13 个无凭证）；
      · 允许改别人 → 名字就成了"远程改别人标签"的通道，与 P1-c"身份只认 token"冲突。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "data"
        import mcp_server
        self.mcp = mcp_server
        self.srv = mcp_server.FishServer(self.root)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _issue(self, alias):
        res = fish.start_session(self.srv.conn, alias=alias, environ={})
        return res["token"], res["citizen_id"]

    # ---------------------------------------------------------------- M1
    def test_M1_mcp_rejects_without_token(self):
        """M1：不带 token 调 rename_citizen 必须响亮拒绝（不建孤儿公民）。"""
        before = self.srv.conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]
        with self.assertRaises(ValueError) as ctx:
            self.srv.call_tool("rename_citizen", {"name": "随便一个名字"})
        msg = str(ctx.exception)
        self.assertIn("会话凭证", msg, f"拒绝理由没说清是缺凭证：{msg}")
        after = self.srv.conn.execute("SELECT COUNT(*) FROM citizens").fetchone()[0]
        self.assertEqual(before, after, "被拒绝的调用居然新建了公民（正是孤儿公民的来源）")

    # ---------------------------------------------------------------- M2
    def test_M2_mcp_only_renames_self(self):
        """M2：带 token 时只改自己，另一个公民的名字一个字都不动。"""
        tok_a, cid_a = self._issue("会话甲")
        _tok_b, cid_b = self._issue("会话乙")
        text = self.srv.call_tool("rename_citizen",
                                  {"name": "fish-主AI-2", "token": tok_a,
                                   "reason": "用户在开场指定"})
        self.assertIn("已改名", text)
        names = dict(self.srv.conn.execute(
            "SELECT citizen_id, display_name FROM citizens").fetchall())
        self.assertEqual(names[cid_a], "fish-主AI-2")
        self.assertNotEqual(names[cid_b], "fish-主AI-2",
                            "改到的不是自己那条——跨公民改名通道被打开了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
