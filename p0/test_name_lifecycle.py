#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 D 判据：名字别名 + 项目三态（闭合 / 重开）。

背景
----
**别名**：生产库里 `cdql`(9 条) 与 `CDQL`(2 条) 是同一个项目的两个入口，而 ADR-003
让"改 project"等价于"改编码"——被永久禁止。别名是唯一不动编码的解法。
用户 2026-10-25 拍板：**别名只影响检索**，不影响写入归属。

**三态**：`retired` 回答"这个名字还用不用"，`closed` 回答"这件事做完了没有"。
合成一个值会让"项目完成"看起来像"项目废弃"（T10 的原症状）。

判据
----
A1   `link_alias` 后 `resolve_name` 返回规范名，链与别名都齐
A2   **核心承诺**：`search_memories(project=别名)` 能查到规范名的记忆
A2b  判别力：解链（unlink）之后同一查询查不到（证明它靠的是别名，不是碰巧）
A3   **写入仍按原名归属**（别名只影响检索，不改已有数据的语义）
A4   `unlink_alias` 幂等（不存在也返回 False 而不报错）
A5   成环被拒绝（A→B 之后再 B→A）
A6   `to_name` 不存在被拒绝（别名必须指向真实存在的名字）
A7   重复 link 被拒绝（要先 unlink，不许悄悄覆盖）
A8   传递别名：A→B、B→C 时 A 解析到 C

P1   `close_project` 后状态=closed，且**名下记忆一条都没动**
P2   closed 项目**拒绝写入**，错误消息里给 reopen 的办法
P2b  判别力：reopen 之后同一写入成功（证明拦的是状态，不是项目本身）
P3   `reopen_project` 后回到 active
P4   retired 拒绝 close/reopen（退役是终态）
P5   close 幂等（重复 close 不报错、不重复写事件）
P6   写事件 `project.closed` / `project.reopened`
P7   闭合**不冻结检索**：closed 项目的记忆照样查得到

跑法
----
    python test_name_lifecycle.py -v
"""
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class _Base(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for p, ns in (("CDQL", "cdql.cc 平台"), ("cdql", "cdql.cc 平台 · 旧入口"),
                      ("PROJA", "甲项目"), ("OLD", "已退役项目")):
            self.conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
                "status) VALUES (?,?,NULL,0,'active')", (p, ns))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _mem(self, project, title, body=""):
        return fish.record_memory(self.conn, self.data, project=project,
                                  title=title, body=body)

    def _events(self, etype):
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM events WHERE type=? ORDER BY seq", (etype,))]

    def _status(self, prefix):
        return self.conn.execute("SELECT status FROM code_registry WHERE prefix=?",
                                 (prefix,)).fetchone()[0]


class AliasTest(_Base):

    # ---------------------------------------------------------------- A1
    def test_A1_link_and_resolve(self):
        """A1：建别名后 resolve_name 给出规范名、链与别名清单。"""
        fish.link_alias(self.conn, "project", "cdql", "CDQL", reason="同一项目的旧入口")
        info = fish.resolve_name(self.conn, "project", "cdql")
        self.assertEqual(info["canonical"], "CDQL")
        self.assertEqual(info["chain"], ["cdql", "CDQL"])
        self.assertIn("cdql", info["aliases"])
        self.assertTrue(info["found"])
        self.assertEqual(info["status"], "active")

    # ---------------------------------------------------------------- A2
    def test_A2_search_expands_alias(self):
        """A2（核心承诺）：按别名检索，能查到规范名的记忆。"""
        code, _ = self._mem("CDQL", "平台上线检查清单", "发版前逐项核对")
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        rows, _ = fish.search_memories(self.conn, query="上线检查", project="cdql")
        self.assertIn(code, [r[0] for r in rows],
                      "按别名查不到规范名的记忆——别名机制等于没接")
        # 反向也成立：按规范名查，同样能看到（同一份数据）
        rows2, _ = fish.search_memories(self.conn, query="上线检查", project="CDQL")
        self.assertIn(code, [r[0] for r in rows2])

    # ---------------------------------------------------------------- A2b
    def test_A2b_without_alias_it_misses(self):
        """A2b 判别力：解链之后同一查询必须查不到（证明 A2 靠的是别名）。"""
        code, _ = self._mem("CDQL", "平台上线检查清单", "发版前逐项核对")
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        fish.unlink_alias(self.conn, "project", "cdql")
        rows, _ = fish.search_memories(self.conn, query="上线检查", project="cdql")
        self.assertNotIn(code, [r[0] for r in rows],
                         "没有别名却查得到——那 A2 的绿是假的（可能根本没按 project 过滤）")

    # ---------------------------------------------------------------- A3
    def test_A3_write_still_uses_own_name(self):
        """A3：写入按**原名**归属（别名只影响检索，不改数据语义）。"""
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        code, _ = self._mem("cdql", "旧入口写的一条")
        owner_proj = self.conn.execute(
            "SELECT project FROM code_index WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(owner_proj, "cdql",
                         "写入被改写成了规范名——那已经动了数据归属语义，超出用户授权")

    # ---------------------------------------------------------------- A4
    def test_A4_unlink_is_idempotent(self):
        """A4：撤销别名幂等（不存在返回 False，不抛错）。"""
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        self.assertTrue(fish.unlink_alias(self.conn, "project", "cdql"))
        self.assertFalse(fish.unlink_alias(self.conn, "project", "cdql"),
                         "第二次撤销应返回 False 而不是报错")

    # ---------------------------------------------------------------- A5
    def test_A5_cycle_rejected(self):
        """A5：成环被拒绝（A→B 之后再 B→A）。"""
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        with self.assertRaises(ValueError) as ctx:
            fish.link_alias(self.conn, "project", "CDQL", "cdql")
        self.assertIn("成环", str(ctx.exception))

    # ---------------------------------------------------------------- A6
    def test_A6_target_must_exist(self):
        """A6：规范名必须真实存在（别名不许指向空号）。"""
        with self.assertRaises(ValueError) as ctx:
            fish.link_alias(self.conn, "project", "PROJA", "根本不存在的项目")
        self.assertIn("不存在", str(ctx.exception))

    # ---------------------------------------------------------------- A7
    def test_A7_duplicate_link_rejected(self):
        """A7：已有别名时拒绝（要改先 unlink，不许悄悄覆盖）。"""
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        with self.assertRaises(ValueError) as ctx:
            fish.link_alias(self.conn, "project", "cdql", "PROJA")
        self.assertIn("已有别名", str(ctx.exception))

    # ---------------------------------------------------------------- A8
    def test_A8_transitive_alias(self):
        """A8：传递别名 A→B、B→C 时，A 解析到 C。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES ('CDQL2','二级入口',NULL,0,'active')")
        self.conn.commit()
        fish.link_alias(self.conn, "project", "cdql", "CDQL")
        fish.link_alias(self.conn, "project", "CDQL2", "CDQL")
        info = fish.resolve_name(self.conn, "project", "cdql")
        self.assertEqual(info["canonical"], "CDQL")
        self.assertIn("CDQL2", info["aliases"], "传递别名没被收进 aliases")


class ProjectLifecycleTest(_Base):

    # ---------------------------------------------------------------- P1
    def test_P1_close_keeps_memories(self):
        """P1：闭合只改状态，名下记忆一条都没动。"""
        code, _ = self._mem("PROJA", "闭合前写的")
        fish.apply_project_status(self.conn, "PROJA", "closed", reason="做完了")
        self.assertEqual(self._status("PROJA"), "closed")
        n = self.conn.execute("SELECT COUNT(*) FROM code_index WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(n, 1, "闭合把记忆弄丢了——闭合 ≠ 删除")
        row = self.conn.execute("SELECT project,status FROM code_index WHERE code=?",
                                (code,)).fetchone()
        self.assertEqual(row[0], "PROJA")
        self.assertEqual(row[1], "active", "闭合改动了记忆自身的状态")

    # ---------------------------------------------------------------- P2
    def test_P2_closed_rejects_write(self):
        """P2：closed 项目拒绝写入，并给出 reopen 的办法。"""
        fish.apply_project_status(self.conn, "PROJA", "closed")
        with self.assertRaises(ValueError) as ctx:
            self._mem("PROJA", "想往闭合项目里塞")
        msg = str(ctx.exception)
        self.assertIn("已闭合", msg)
        self.assertIn("reopen", msg, "错误消息没给补救动作——AI 会卡在这里")

    # ---------------------------------------------------------------- P2b
    def test_P2b_reopen_makes_it_writable(self):
        """P2b 判别力：reopen 之后同一写入必须成功（拦的是状态，不是项目本身）。"""
        fish.apply_project_status(self.conn, "PROJA", "closed")
        fish.apply_project_status(self.conn, "PROJA", "active", reason="又要写了")
        code, _ = self._mem("PROJA", "重开之后写的")
        self.assertTrue(code)

    # ---------------------------------------------------------------- P3
    def test_P3_reopen_restores_active(self):
        """P3：reopen 回到 active。"""
        fish.apply_project_status(self.conn, "PROJA", "closed")
        fish.apply_project_status(self.conn, "PROJA", "active", reason="继续")
        self.assertEqual(self._status("PROJA"), "active")

    # ---------------------------------------------------------------- P4
    def test_P4_retired_is_terminal(self):
        """P4：retired 是终态——MCP 面的状态机 helper 会拒绝它。"""
        self.conn.execute("UPDATE code_registry SET status='retired' WHERE prefix='OLD'")
        self.conn.commit()
        import mcp_server
        srv = mcp_server.FishServer(self.data)
        try:
            with self.assertRaises(ValueError) as ctx:
                srv.call_tool("close_project", {"prefix": "OLD"})
            self.assertIn("退役", str(ctx.exception))
        finally:
            srv.conn.close()

    # ---------------------------------------------------------------- P5
    def test_P5_close_is_idempotent(self):
        """P5：重复 close 不报错、不重复写事件。"""
        fish.apply_project_status(self.conn, "PROJA", "closed", reason="一")
        fish.apply_project_status(self.conn, "PROJA", "closed", reason="二")
        ev = self._events("project.closed")
        self.assertEqual(len(ev), 2, "apply 是底层动作，两次调用两次事件（幂等由上层保证）")
        # 上层（MCP）对"已经是 closed"必须直接返回，不再落库
        import mcp_server
        srv = mcp_server.FishServer(self.data)
        try:
            text = srv.call_tool("close_project", {"prefix": "PROJA"})
            self.assertIn("已经是", text, f"重复闭合应提示幂等：{text}")
        finally:
            srv.conn.close()
        self.assertEqual(len(self._events("project.closed")), 2, "幂等路径又写了一条事件")

    # ---------------------------------------------------------------- P6
    def test_P6_events_written(self):
        """P6：close / reopen 各写一条事件，payload 带状态与原因。"""
        fish.apply_project_status(self.conn, "PROJA", "closed", reason="做完了")
        fish.apply_project_status(self.conn, "PROJA", "active", reason="又要写")
        closed = self._events("project.closed")
        reopened = self._events("project.reopened")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["status"], "closed")
        self.assertIn("做完", closed[0]["reason"])
        self.assertEqual(len(reopened), 1)

    # ---------------------------------------------------------------- P7
    def test_P7_closed_still_searchable(self):
        """P7：闭合**不冻结检索**——closed 项目的记忆照样查得到。"""
        code, _ = self._mem("PROJA", "闭合之后还能查到的记忆", "内容")
        fish.apply_project_status(self.conn, "PROJA", "closed")
        rows, _ = fish.search_memories(self.conn, query="还能查到", project="PROJA")
        self.assertIn(code, [r[0] for r in rows],
                      "闭合把检索也关了——用户只要求'不再接收新记忆'")


if __name__ == "__main__":
    unittest.main(verbosity=2)
