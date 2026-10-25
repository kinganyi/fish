#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取侧必须消费改名映射（2026-10-25 · 外部评估 `EVAL-261025-06`）。

背景
----
2026-10-25 的项目改名把 162 条编码从 `ANYI-*` / `cdql-*` 换成了新前缀。
`rebuild_projection` 早就读 `code.renamed.batch` 还原编码，但**读取侧没接**：

| 调用 | 修前 |
|---|---|
| `resolve_code("ANYI-261022-75")` | 「编码不存在」，而正文明明在 |
| `read_body("ANYI-261022-75")` | 「记忆不存在」 |

后果比"查不到"更糟：**AI 会判定这条记忆已丢失，然后重记一条**，制造重复。
而且会持续复发——本地工作空间的 MEMORY.md / daily / artifacts 里还有大量旧编码写法。

判据
----
  Q1  `resolve` 用旧编码 → 返回**新编码的信息** + 一行改名提示
  Q2  `read_body` 用旧编码 → 返回**正文** + 一行改名注释
  Q3  真·不存在的编码仍然报不存在（不因新逻辑而把任意编码"解析"出来）
  Q4  新编码不受影响（不该多出改名提示）
  Q5  映射加载是**纯读**（连跑不改库）
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

import mcp_server  # noqa: E402

OLD = "OLD-261022-01"
NEW = "NEWP-261022-01"
CID = "01RENAMETEST0000000000001"


class RenameReadPathTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (CID, "改名测试AI", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('NEWP','NEWP',NULL,0,'active')")
        self.conn.commit()
        # 造一条"改名后"的记忆（用新编码），正文按新命名规则落盘
        self.rel = fish.body_relpath("NEWP", NEW, "一条改过名的记忆")
        p = self.root / self.rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# 一条改过名的记忆\n\n> 编码：{NEW}\n\n正文内容。\n", encoding="utf-8")
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active','[]',?,?,?,0,0)",
            (NEW, "一条改过名的记忆", "NEWP", CID, "fact", self.rel,
             fish.now_utc(), fish.now_utc()))
        # 造改名事件（旧 → 新）
        seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        self.conn.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("EVT-RENAME-TEST", seq, fish.now_utc(), "code.renamed.batch",
             json.dumps({"citizen_id": "system", "evidence_level": "signed"}),
             json.dumps({"kind": "project", "id": "OLD"}),
             json.dumps({"mapping": {OLD: NEW}, "count": 1}, ensure_ascii=False), "", "evt@1"))
        self.conn.commit()
        fish.reindex_fts(self.conn, self.root)
        self.srv = mcp_server.FishServer(self.root)
        self.ident = mcp_server.Identity(CID, "test-session", "test", mcp_server.IDENTITY_VALID)

    def tearDown(self):
        try:
            self.srv.conn.close()
        except Exception:
            pass
        self.conn.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    # ------------------------------------------------------------------ Q1
    def test_Q1_resolve_old_code_redirects(self):
        """旧编码查询必须给新编码的信息，并明说"已改名"。"""
        out = self.srv.tool_resolve({"code": OLD}, self.ident)
        self.assertIn(NEW, out, f"应返回新编码 {NEW}，实际：{out[:200]}")
        self.assertIn("已改名", out, f"必须明说已改名，实际：{out[:200]}")
        self.assertNotIn("编码不存在", out, "不该报不存在（正文还在）")
        # 旧编码哪去了也要写清楚，否则 AI 会以为映射是临时的
        self.assertIn("映射永久保留", out)

    # ------------------------------------------------------------------ Q2
    def test_Q2_read_body_old_code_follows(self):
        """旧编码读正文必须跟随重定向，并把正文真读出来。"""
        out = self.srv.tool_read_body({"code": OLD}, self.ident)
        self.assertIn("正文内容", out, f"应读到正文，实际：{out[:200]}")
        self.assertIn("已改名", out, "应带一行改名注释")
        self.assertIn(NEW, out)
        # 正文尾部注释里的编码也应是新的（否则 AI 会以为自己在读旧条目）
        self.assertIn(f"<!-- {NEW}", out)

    # ------------------------------------------------------------------ Q3
    def test_Q3_unknown_code_still_not_found(self):
        """真不存在的编码仍然报不存在——不能让新逻辑把任意编码"解析"出来。"""
        with self.assertRaises(ValueError) as ctx:
            self.srv.tool_read_body({"code": "NOPE-261022-99"}, self.ident)
        self.assertIn("不存在", str(ctx.exception))
        out = self.srv.tool_resolve({"code": "NOPE-261022-99"}, self.ident)
        self.assertIn("编码不存在", out)

    # ------------------------------------------------------------------ Q4
    def test_Q4_new_code_unaffected(self):
        """新编码正常：读得到、而且**不该**冒出改名提示。"""
        out = self.srv.tool_resolve({"code": NEW}, self.ident)
        self.assertIn("一条改过名的记忆", out)
        self.assertNotIn("已改名", out, "新编码不该有改名提示")
        body = self.srv.tool_read_body({"code": NEW}, self.ident)
        self.assertIn("正文内容", body)
        self.assertNotIn("该编码已改名", body)

    # ------------------------------------------------------------------ Q5
    def test_Q5_read_is_pure(self):
        """加载映射与解析都是**纯读**：连跑不改库。"""
        before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_ci = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        for _ in range(3):
            fish.load_rename_map(self.conn)
            fish.resolve_renamed(self.conn, OLD)
            fish.resolve_renamed(self.conn, "NOPE-261022-99")
        after = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        after_ci = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertEqual(before, after, "解析不该写事件")
        self.assertEqual(before_ci, after_ci, "解析不该改投影")


if __name__ == "__main__":
    unittest.main(verbosity=2)
