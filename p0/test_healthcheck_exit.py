#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P1-6 判据：`healthcheck` 的退出码必须是可读的结论。

## 审查方实测的问题（已复核成立，且根因更明确）

`fish.py` 自己就有注释写着：

    `return 1` 今天等于退出 0（§3.1 / R9）。这是既有缺陷
    `main()` 丢弃返回值；新命令族用 `raise SystemExit(code)`

也就是说：`cmd_healthcheck` 里写 `return 1` **等于永远成功**。断链 1 条在
`&&` 链条里表现为"体检通过"——退出码是脚本唯一能被机器读到的结论，写错它
等于让所有依赖它的自动化失效。

## 两个问题，一起修

1. **退出码恒为 0**：现在 0=干净 / 1=有问题 / 2=判不到（库缺失）
2. **体检会写 schema**：旧实现先 `executescript(SCHEMA)` + `migrate(conn)`。
   一个"检查"动作写 schema，在只读介质/权限受限/生产排障时都可能改变现场；
   更糟的是库不存在时它会**建一个空库然后报告一切正常**——把"没有"说成"没问题"。

## 这批判据为什么能失败

三条退出码都要**跑真子进程**验，不是读代码。特别是 exit 1：必须真造一条断链，
否则"exit 0"和"exit 1"在没断链的库上看起来是一样的。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402


def _run(root):
    return subprocess.run(
        [sys.executable, str(HERE / "fish.py"), "--root", str(root), "healthcheck"],
        capture_output=True, text=True, encoding="utf-8")


class HealthcheckExitCodeTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="fish-hc-")
        self.data = Path(self._tmpdir) / "data"

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_db(self, *, broken=False):
        self.data.mkdir(parents=True, exist_ok=True)
        c = fish.connect(self.data)
        try:
            c.executescript(fish.SCHEMA)
            fish.migrate(c)
            c.execute("INSERT INTO code_registry (prefix,namespace,last_date,last_seq)"
                      " VALUES ('T','T','2026-11-18',0)")
            c.commit()
            code = fish.next_code(c, "T")
            fp = "projects/T/not-exists.md" if broken else None
            c.execute("INSERT INTO code_index (code,title,project,mem_type,status,file_path)"
                      " VALUES (?,?,?,?,?,?)", (code, "x", "T", "fact", "active", fp))
            c.commit()
        finally:
            c.close()
        return code

    # ------------------------------------------------------------- 三态
    def test_clean_db_exits_zero(self):
        """干净库 → 0。这是对照组：没有它，正文明说的"红"可能只是永远红。"""
        self._make_db(broken=False)
        r = _run(self.data)
        self.assertEqual(r.returncode, 0, f"干净库应为 0，实际 {r.returncode}\n{r.stdout}")

    def test_broken_link_exits_one(self):
        """**核心判据**：断链 1 条 → 必须 1。

        旧实现恒为 0，所以这条在旧代码上必红——这就是它能失败的原因。
        """
        code = self._make_db(broken=True)
        r = _run(self.data)
        self.assertEqual(r.returncode, 1,
                         f"断链 1 条（{code}）应退出 1，实际 {r.returncode}\n{r.stdout}")
        self.assertIn("断链 1", r.stdout, "输出里也要点明断链数")

    def test_missing_db_exits_two_and_does_not_create_it(self):
        """库不存在 → 2，且**不许创建**。

        "体检把「没有」说成「没问题」"是最坏的形态：它会让一个空目录看起来健康，
        还会留下一个空库让后续命令以为初始化过。
        """
        self.data.mkdir(parents=True, exist_ok=True)
        r = _run(self.data)
        self.assertEqual(r.returncode, 2, f"库缺失应为 2，实际 {r.returncode}\n{r.stdout}")
        self.assertNotIn("Traceback", r.stderr, "不该抛裸异常")
        self.assertIn("不存在", r.stderr, f"要说清是库不存在：{r.stderr[:200]}")
        self.assertFalse((self.data / "index.sqlite").exists(),
                         "体检**创建了**数据库——它必须是只读检查")

    # ------------------------------------------------------------- 只读
    def test_healthcheck_does_not_migrate(self):
        """体检不许写 schema：改坏一个已有库的 schema 版本，跑完体检后必须是原样。

        用 `PRAGMA user_version` 做哨兵——migrate 会动它与表结构。
        """
        self._make_db(broken=False)
        import sqlite3
        db = self.data / "index.sqlite"
        conn = sqlite3.connect(str(db))
        before = conn.execute("PRAGMA user_version").fetchone()[0]
        tables_before = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        conn.close()
        _run(self.data)
        conn = sqlite3.connect(str(db))
        after = conn.execute("PRAGMA user_version").fetchone()[0]
        tables_after = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        conn.close()
        self.assertEqual(after, before, "体检改动了 user_version（说明跑了 migrate）")
        self.assertEqual(tables_after, tables_before, "体检增删了表")


if __name__ == "__main__":
    unittest.main(verbosity=2)
