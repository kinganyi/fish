# -*- coding: utf-8 -*-
"""F821 暴露的三类问题回归判据（2026-11-26，ruff 基线 301 条时发现）。

为什么单独建文件：这三条都在**异常/动态注入**路径上，正常路径永远走不到；
没有判据的话，下一次重构把 import 删掉也不会有人发现——直到真正的错误发生时
被 NameError 掩盖（`fish_git_store` 的注释里就记着一次同类事故）。
"""
from __future__ import annotations

import sqlite3
import unittest

import fish
import fish_cli
import fish_git_store
import fish_profile
import fish_schema


class ProfileSqliteImportTest(unittest.TestCase):
    """`fish_profile._open_pending_question` 的 `except sqlite3.Error` 必须真的可捕获。"""

    def test_missing_table_returns_none_not_nameerror(self):
        conn = sqlite3.connect(":memory:")
        try:
            # 没有 profile_questions 表 → 走 except 分支；修复前是 NameError: sqlite3
            self.assertIsNone(fish_profile._open_pending_question(conn))
        finally:
            conn.close()


class GitStoreWarnIgnoredTest(unittest.TestCase):
    """`fish_git_store.rebuild_commit_projection` 的坏 payload 分支不许 NameError，且必须留痕。"""

    def test_bad_payload_survives_and_warns(self):
        conn = sqlite3.connect(":memory:")
        calls = []
        original = fish_git_store.fish_common.warn_ignored
        fish_git_store.fish_common.warn_ignored = lambda tag, exc: calls.append((tag, type(exc).__name__))
        try:
            conn.executescript(fish_schema.SCHEMA)
            conn.execute(
                "INSERT INTO events (event_id, ts, type, actor, subject, payload, cause, schema) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("e1", "2026-11-26T00:00:00Z", "git.commit", "{}", "{}", "{not-json", None, "evt@1"))
            conn.commit()
            # 修复前：warn_ignored 未定义 → NameError；改成 pass → 不留痕
            self.assertEqual(fish_git_store.rebuild_commit_projection(conn), 0)
            self.assertEqual(len(calls), 1, "坏 payload 必须经 warn_ignored 留痕一次")
            self.assertIn("fish_git_store.rebuild_commit_projection", calls[0][0])
        finally:
            fish_git_store.fish_common.warn_ignored = original
            conn.close()


class CliDynamicGlobalsTest(unittest.TestCase):
    """`fish_cli` 通过 `install(globals())` 注入 fish 的名字；ruff 看不见，必须运行期核。"""

    NAMES = (
        "DEFAULT_ROOT", "_ALIAS_KINDS", "BOARD_SCOPES",
        "DISTILL_DEFAULT_DAYS", "DISTILL_MIN_GROUP", "DISTILL_MAX_GROUPS",
        "KINDS", "INJECTION_SECTIONS", "fish_corpus",
    )

    def test_all_dynamic_names_exist_in_fish_namespace(self):
        missing = [n for n in self.NAMES if not hasattr(fish, n)]
        self.assertEqual(missing, [],
                         "fish_cli 依赖的动态注入名在 fish 命名空间里不存在：" + ", ".join(missing))

    def test_install_copies_foreign_names_into_cli_globals(self):
        # install(namespace) 的语义：把 dict 里的名字补进本模块 globals（不覆盖已有定义）。
        sentinel = {"_F821_TEST_SENTINEL": 42, "sys": "must-not-overwrite"}
        try:
            fish_cli.install(sentinel)
            self.assertEqual(fish_cli.__dict__.get("_F821_TEST_SENTINEL"), 42)
            # 已有名字不被覆盖（sys 是 fish_cli 自己 import 的模块）
            self.assertNotEqual(fish_cli.sys, "must-not-overwrite")
        finally:
            fish_cli.__dict__.pop("_F821_TEST_SENTINEL", None)


if __name__ == "__main__":
    unittest.main()
