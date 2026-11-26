#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W2 独立评审残留判据（2026-11-26）：P1-2 原子性在 mcp / push 配置两条链上的回归。

## 判据（每条都能失败）

  A1  `tool_add_project` 注入 `append_event` 失败 → 前缀回滚（同连接 + 另一连接都查不到）、
      `project.added` 事件 0 条；正常路径对照：1 行 + 1 事件。
  A2  `tool_register` 注入 `append_event` 失败 → 公民回滚、`citizen.registered` 0 条；
      正常路径对照：1 行 + 1 事件。
  B1  `write_config` 注入**审计文件写失败**（`_append_audit` 抛 OSError）→ 配置值回到写前、
      `config.changed` 事件 0 条、审计文件不存在（旧实现会：配置已生效、审计 0 行）。
  B2  `write_config` 注入 `append_event` 失败 → 配置值回到写前、事件 0 条。
  B3  正常路径：配置写入 + 恰好 1 条 `config.changed` + 恰好 1 行审计；
      同值重写短路（changed=False，不多写事件/审计）。

## 为什么放独立文件

`test_event_atomicity.py` 是 W3 拆分前写的：其中 session 组用
`mock.patch.object(fish, "append_event")` 打补丁，而 W3 把 `start_session` 等搬进
`fish_session.py` 后，补丁目标会随搬迁进度变化。本文件只测**没有搬家**的
`mcp_server` 与 `fish_push_audit`，补丁目标明确，不依赖 W3 的搬迁进度。
"""
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
import fish_db  # noqa: E402
import fish_push_audit  # noqa: E402
import mcp_server  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-w2res-"))
        self.root = self.tmp / "data"
        conn = fish.connect(self.root)
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        conn.commit()
        conn.close()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---- 读数一律走**另一条连接**：抓"本连接看得见、别人看不见"的未提交脏数据 ----
    def _one(self, sql, params=()):
        conn = fish.connect(self.root)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _count(self, table, where="", params=()):
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += " WHERE " + where
        row = self._one(sql, params)
        return row[0] if row else 0


class McpToolAtomicityTest(_Base):
    """A1 / A2：MCP 两个"先 commit 再 append_event"的残留写口。"""

    def setUp(self):
        super().setUp()
        self.srv = mcp_server.FishServer(self.root)
        self.addCleanup(self.srv.conn.close)
        self.ident = mcp_server.IDENTITY_ANONYMOUS

    @staticmethod
    def _fail_append():
        return mock.patch.object(
            fish, "append_event",
            side_effect=RuntimeError("inject: append_event 写失败"))

    def test_A1_add_project_failure_rolls_back_registry(self):
        with self._fail_append():
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                self.srv.tool_add_project(
                    {"prefix": "W2ATOM", "namespace": "W2 原子项目"}, self.ident)
        # 同连接（抓"没 rollback"）+ 另一条连接（抓"提前 commit"）都必须是 0
        self.assertIsNone(
            self.srv.conn.execute(
                "SELECT 1 FROM code_registry WHERE prefix='W2ATOM'").fetchone(),
            "失败后前缀仍在本连接可见（rollback 没执行）")
        self.assertIsNone(self._one("SELECT 1 FROM code_registry WHERE prefix='W2ATOM'"),
                          "失败后前缀已被提交（先 commit 后 append_event）")
        self.assertEqual(self._count("events", "type='project.added'"), 0,
                         "失败路径写了 project.added 事件")

        # 正常路径对照
        out = self.srv.tool_add_project(
            {"prefix": "W2ATOM", "namespace": "W2 原子项目"}, self.ident)
        self.assertIn("已注册前缀：W2ATOM", out)
        self.assertIsNotNone(self._one(
            "SELECT 1 FROM code_registry WHERE prefix='W2ATOM'"), "正常路径没落库")
        self.assertEqual(self._count("events", "type='project.added'"), 1,
                         "正常路径应恰好 1 条 project.added")

    def test_A2_register_failure_rolls_back_citizen(self):
        with self._fail_append():
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                self.srv.tool_register(
                    {"display_name": "W2原子公民", "kind": "visitor"}, self.ident)
        self.assertIsNone(
            self.srv.conn.execute(
                "SELECT 1 FROM citizens WHERE display_name='W2原子公民'").fetchone(),
            "失败后公民仍在本连接可见（rollback 没执行）")
        self.assertIsNone(
            self._one("SELECT 1 FROM citizens WHERE display_name='W2原子公民'"),
            "失败后公民已被提交（先 commit 后 append_event）")
        self.assertEqual(self._count("events", "type='citizen.registered'"), 0,
                         "失败路径写了 citizen.registered 事件")

        # 正常路径对照
        out = self.srv.tool_register(
            {"display_name": "W2原子公民", "kind": "visitor"}, self.ident)
        cid = out.splitlines()[0].strip()
        self.assertTrue(cid, f"register 回执没有 citizen_id：{out!r}")
        self.assertIsNotNone(self._one(
            "SELECT 1 FROM citizens WHERE citizen_id=?", (cid,)), "正常路径没落库")
        self.assertEqual(self._count("events", "type='citizen.registered'"), 1,
                         "正常路径应恰好 1 条 citizen.registered")


class PushConfigAtomicityTest(_Base):
    """B1 / B2 / B3：`fish_push_audit.write_config` 的配置 + 审计同事务。"""

    KEY = "push.after_tool_every_n_calls"        # CONFIG_DEFAULTS 缺省 10（int）

    def setUp(self):
        super().setUp()
        self.db_path = str(self.root / "index.sqlite")
        self.audit_file = Path(fish_push_audit.audit_path(self.db_path))

    def _value(self):
        conn = fish.connect(self.root)
        try:
            return fish.get_config(conn, self.KEY)
        finally:
            conn.close()

    def test_B1_audit_write_failure_rolls_back_config(self):
        before = self._value()
        before_events = self._count("events", "type='config.changed'")
        with mock.patch.object(fish_push_audit, "_append_audit",
                               side_effect=OSError("inject: 审计写失败")):
            with self.assertRaises(OSError, msg="审计写失败必须向上抛（配置要回滚）"):
                fish_push_audit.write_config(self.db_path, self.KEY, 321)
        self.assertEqual(self._value(), before, "审计失败后配置值没有回到写前")
        self.assertEqual(self._count("events", "type='config.changed'"), before_events,
                         "审计失败后 config.changed 事件仍然落盘")
        self.assertFalse(self.audit_file.exists(),
                         "审计失败却创建/留下了审计文件（旧行为是配置已生效、审计 0 行）")

    def test_B2_event_failure_rolls_back_config(self):
        before = self._value()
        before_events = self._count("events", "type='config.changed'")
        with mock.patch.object(fish_db, "append_event",
                               side_effect=RuntimeError("inject: append_event 写失败")):
            with self.assertRaises(RuntimeError, msg="append_event 异常必须向上抛"):
                fish_push_audit.write_config(self.db_path, self.KEY, 321)
        self.assertEqual(self._value(), before, "事件失败后配置值没有回到写前")
        self.assertEqual(self._count("events", "type='config.changed'"), before_events,
                         "事件失败后 config.changed 仍然落盘")

    def test_B3_normal_path_writes_config_event_and_audit(self):
        before = self._value()
        out = fish_push_audit.write_config(self.db_path, self.KEY, 321)
        self.assertTrue(out["changed"], f"正常路径 changed 应为 True：{out}")
        self.assertEqual(out["new"], 321)
        self.assertIsNotNone(out["auditAt"], "正常路径没有审计时间戳")
        self.assertEqual(self._value(), 321, "正常路径配置没写进去")
        self.assertEqual(self._count("events", "type='config.changed'"), 1,
                         "正常路径应恰好 1 条 config.changed")
        lines = self.audit_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1, f"正常路径审计应恰好 1 行：{lines}")
        record = json.loads(lines[0])
        self.assertEqual(record["key"], self.KEY)
        self.assertEqual(record["old"], before)
        self.assertEqual(record["new"], 321)

        # 同值重写：短路，不写库、不记事件、不记审计
        again = fish_push_audit.write_config(self.db_path, self.KEY, 321)
        self.assertFalse(again["changed"], "同值重写应 changed=False")
        self.assertIsNone(again["auditAt"], "同值重写不该写审计")
        self.assertEqual(self._count("events", "type='config.changed'"), 1,
                         "同值重写多写了 config.changed")
        self.assertEqual(len(self.audit_file.read_text(encoding="utf-8").strip().splitlines()),
                         1, "同值重写多写了审计行")


if __name__ == "__main__":
    unittest.main(verbosity=2)
