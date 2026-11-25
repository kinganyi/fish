#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-9 + P2-5（外部审查 2026-11-25）判据：体检的**三态**与**只读连接**。

## 修的是什么

P1-9：`cmd_healthcheck` 里 `fish_purge.scan_broken` 抛异常时只 `warn_ignored` 吞掉，
`broken` 保持 `[]` → 打印「断链 0 条」→ `exit 0`。**把"判不到"说成"干净"是体检最坏的
形态**：CI/脚本把 `fish healthcheck` 当门禁时，扫描失败会给出绿色。

P2-5：体检用 `connect()`（读写 + 设 WAL）。一个自称"只读检查"的动作不该有写能力——
在只读介质上它会直接失败，在正常库上也会创建/触碰 `-wal`/`-shm`。

**核对结论（本轮逐段看过）**：`cmd_healthcheck` 全函数没有任何 INSERT/UPDATE/DELETE/
`executescript`/`migrate`；三个子检查（`fish_purge.scan_broken` / `fish_kb_index.health_lines`
/ `fish_activity.collection_liveness`）也全是 SELECT（用 AST/正则扫过它们的函数体）——
所以"只读连接"不需要拆出一个写连接，没有任何一段必须写。

## 判定口径

    0 = 干净 / 1 = 有问题（断链或幽灵归属）/ 2 = 判不到（库缺失 **或核心检查抛异常**）

## 这批判据为什么能失败

  · 判不到：真 monkeypatch `scan_broken` 抛异常，走**进程内**真实路径 —— 把
    `broken is None` 那个哨兵删掉（变异 M-HEALTH）就退回 exit 0，这条必红；
  · 只读：把库文件设成**只读**再跑 —— 旧实现（读写连接 + `PRAGMA journal_mode=WAL`）
    会抛 `attempt to write a readonly database`，这条必红（变异 M-READONLY）；
  · 断链 1 条 → 1 与干净 → 0 是对照组：没有它们，"永远 exit 2"也能让判不到那条变绿。
"""
import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_purge  # noqa: E402  —— 被 monkeypatch 的就是它


def _run(root):
    """子进程跑真 CLI（退出码是脚本唯一能读到的结论，必须在**进程外**验）。"""
    return subprocess.run(
        [sys.executable, str(HERE / "fish.py"), "--root", str(root), "healthcheck"],
        capture_output=True, text=True, encoding="utf-8")


class HealthcheckTristateTest(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="fish-hc3-")
        self.data = Path(self._tmpdir) / "data"

    def tearDown(self):
        import shutil
        # 只读属性（本文件有一条判据会设它）要先去掉，否则 Windows 上删不掉。
        for p in (self.data / "index.sqlite",):
            if p.exists():
                try:
                    os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
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
            c.close()          # 关掉最后一个连接：WAL 归并回主库文件
        return code

    # ------------------------------------------------------- 判不到 → exit 2
    def test_scan_failure_exits_two_and_says_unknown(self):
        """**核心**：断链扫描抛异常 → exit 2，且输出里明说「判不到」。

        走进程内真实路径（子进程 monkeypatch 不进去）：patch `fish_purge.scan_broken`，
        直接调 `cmd_healthcheck` 抓 `SystemExit.code` 与 stdout。
        """
        self._make_db()
        args = type("A", (), {"root": str(self.data)})()
        buf = io.StringIO()
        with mock.patch.object(fish_purge, "scan_broken",
                               side_effect=RuntimeError("模拟扫描失败")):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    fish.cmd_healthcheck(args)
        out = buf.getvalue()
        self.assertEqual(ctx.exception.code, 2,
                         f"扫描失败必须 exit 2（判不到），实际 {ctx.exception.code}\n{out}")
        self.assertIn("判不到", out, f"输出里没说「判不到」：{out!r}")
        self.assertNotIn("断链 0 条", out,
                         f"把判不到念成「断链 0 条」——正是 P1-9 的缺陷：\n{out!r}")
        self.assertIn("模拟扫描失败", out, "判不到要给出原因（异常类型/消息）")

    # ------------------------------------------------------- 三态对照组
    def test_clean_db_exits_zero(self):
        """对照组：干净库 → 0（否则"永远 exit 2"也能让上面那条变绿）。"""
        self._make_db(broken=False)
        r = _run(self.data)
        self.assertEqual(r.returncode, 0, f"干净库应为 0，实际 {r.returncode}\n{r.stdout}")

    def test_broken_link_exits_one(self):
        """对照组：断链 1 条 → 1（有问题是 1，不是 2：2 只留给判不到）。"""
        code = self._make_db(broken=True)
        r = _run(self.data)
        self.assertEqual(r.returncode, 1,
                         f"断链 1 条（{code}）应退出 1，实际 {r.returncode}\n{r.stdout}")

    # ------------------------------------------------------- 只读连接
    def test_healthcheck_uses_the_readonly_opener(self):
        """**P2-5 核心**：体检必须走只读开法（`fish_ui.open_readonly`），不许用写作连接。

        为什么不在"只读文件"上验：Windows 上 `os.chmod(0o444)` 之后 SQLite 会**静默
        降级**成只读打开——实测读写 `connect()` 照样成功、`PRAGMA journal_mode=WAL`
        也返回 `wal`。所以"只读介质会失败"在 Windows 上不是可判据的事（第一版这么写，
        变异体 M-READONLY 照样绿——那条判据是假的，已改成这一条）。

        真正的契约是"它用哪个开法"。这条同时做两件事：
          · 把写作连接 `fish.connect` 换成炸弹 → 用它就炸（旧实现立刻红）；
          · 用一个探针包住 `fish_ui.open_readonly` → 检查拿到的连接 `query_only=1`。
        """
        self._make_db()
        import fish_ui
        seen = {}
        real = fish_ui.open_readonly

        def _spy(db_path):
            conn = real(db_path)
            seen["query_only"] = conn.execute("PRAGMA query_only").fetchone()[0]
            return conn

        def _boom(_root):
            raise AssertionError("体检用了写作连接 connect()：它会设 WAL / 建 -wal/-shm")

        args = type("A", (), {"root": str(self.data)})()
        with mock.patch.object(fish, "connect", _boom), \
                mock.patch.object(fish_ui, "open_readonly", _spy):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    fish.cmd_healthcheck(args)
        self.assertEqual(ctx.exception.code, 0, "干净库应当 exit 0")
        self.assertEqual(seen.get("query_only"), 1,
                         "体检的连接不是 query_only —— 它可能还能写（P2-5 未修）")

    def test_readonly_path_still_reports_unknown(self):
        """两条合起来：只读开法 + 核心检查抛异常 → 仍是 exit 2（既不许崩，也不许 0）。"""
        self._make_db()
        args = type("A", (), {"root": str(self.data)})()
        buf = io.StringIO()

        def _boom(_root):
            raise AssertionError("体检用了写作连接 connect()")

        with mock.patch.object(fish, "connect", _boom), \
                mock.patch.object(fish_purge, "scan_broken",
                                  side_effect=RuntimeError("模拟扫描失败")):
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    fish.cmd_healthcheck(args)
        self.assertEqual(ctx.exception.code, 2, buf.getvalue())
        self.assertIn("判不到", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
