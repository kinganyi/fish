#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 · 知识库登记（vault 六命令）判据（T04，2026-10-26）。

被测定义（KB-1 / KB-2 + 裁决 Q9/Q12）：
  `kb vault add` 幂等（按 `path_key = normcase(realpath(path))`，不是按 `path` 字符串）；
  六命令 add / list / set-default / set-mode / scan / remove 各有退出码语义；
  `remove` 只删注册行、**磁盘一个字节不动**；`scan` 只 walk+stat、不读正文；
  schema 默认 `ro`（Q9：AI 写要靠人显式授权）。

## 变异自证（X-1）

变异 —— 对应 `test_add_is_idempotent_across_case`：
  把 `kb_vault_add` 的去重键从 `path_key` 改成 `path`（原字符串）→ 用 `E:\\…` 与 `e:\\…`
  各 add 一次会**新增一行**（SQLite 的 UNIQUE 是二进制比较）→ 行数断言红。
  这条变异对应真实风险：Windows 上大小写/短名/junction 三种写法都能指向同一个目录。

纪律（K12）：`tempfile.mkdtemp()` 造 vault；**绝不指向 `E:\\Obsidian\\安逸知识库`**。
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class KBVaultTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbv-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()
        (self.vault_dir / "00-首页").mkdir()
        (self.vault_dir / "00-首页" / "欢迎.md").write_text(
            "---\ntags: 入门\n---\n\n# 欢迎\n", encoding="utf-8")
        (self.vault_dir / "附件.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具
    def _add(self, **kw):
        kw.setdefault("name", "测试库")
        kw.setdefault("path", str(self.vault_dir))
        return fish.kb_vault_add(self.conn, **kw)

    def _count(self):
        return self.conn.execute("SELECT COUNT(*) FROM kb_vault").fetchone()[0]

    def _run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["fish.py"] + argv
        out, err = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    fish.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    # ============================================================ add
    def test_add_defaults_to_ro_and_lists(self):
        """Q9：不显式 `--mode rw` 就是 ro；list 读得到（默认库排最前）。"""
        vault = self._add()
        self.assertEqual(vault["mode"], "ro")
        self.assertEqual(vault["max_open_bytes"], fish.KB_DEFAULT_MAX_OPEN_BYTES)
        self.assertEqual(vault["ignore_globs"],
                         list(fish.KB_DEFAULT_IGNORE_GLOBS))
        self.assertFalse(vault["is_default"])
        listed = fish.kb_vault_list(self.conn)
        self.assertEqual([v["name"] for v in listed], ["测试库"])
        self.assertTrue(vault["vault_id"])
        self.assertEqual(vault["status"], "active")

    def test_add_is_idempotent_across_case(self):
        """**变异证明点**：同一路径不同大小写 → 不新增行（按 path_key 归一）。"""
        self._add()
        alt = str(self.vault_dir).upper() if os.name == "nt" else str(self.vault_dir)
        self._add(path=alt, mode="rw")
        self.assertEqual(self._count(), 1, "同一路径不同大小写被当成两个库")
        vault = fish.kb_vault_list(self.conn)[0]
        self.assertEqual(vault["mode"], "rw", "重复 add 应更新 mode")

    def test_add_twice_same_args_is_noop(self):
        first = self._add()
        second = self._add()
        self.assertEqual(first["vault_id"], second["vault_id"])
        self.assertEqual(self._count(), 1)

    def test_add_rejects_bad_inputs(self):
        with self.assertRaises(fish.KBError) as ctx:
            self._add(mode="rwx")
        self.assertEqual(ctx.exception.code, "bad_usage")
        with self.assertRaises(fish.KBError) as ctx:
            self._add(path=str(self.tmp / "没有这个目录"))
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(fish.KBError) as ctx:
            self._add(path="相对/路径")
        self.assertEqual(ctx.exception.code, "bad_usage")
        (self.tmp / "afile.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(fish.KBError) as ctx:
            self._add(path=str(self.tmp / "afile.txt"))
        self.assertEqual(ctx.exception.code, "not_found")
        with self.assertRaises(fish.KBError) as ctx:
            self._add(name="  ")
        self.assertEqual(ctx.exception.code, "bad_usage")

    def test_add_rejects_name_taken_by_other_path(self):
        self._add(name="A")
        other = self.tmp / "vault2"
        other.mkdir()
        with self.assertRaises(fish.KBError) as ctx:
            self._add(name="A", path=str(other))
        self.assertEqual(ctx.exception.code, "exists")
        self.assertEqual(self._count(), 1)

    def test_add_ignore_appends_extra_rules(self):
        vault = self._add(ignore=["**/_media/**", "99-草稿/**"])
        self.assertIn("**/_media/**", vault["ignore_globs"])
        self.assertIn("99-草稿/**", vault["ignore_globs"])
        self.assertEqual(vault["ignore_globs"][0], ".obsidian/**")
        raw = self.conn.execute("SELECT ignore_globs FROM kb_vault").fetchone()[0]
        self.assertEqual(json.loads(raw), vault["ignore_globs"],
                         "ignore_globs 必须以 JSON 数组存（人读得懂、解析得回）")

    def test_add_custom_max_open_bytes_and_note(self):
        vault = self._add(max_open_bytes=1024, note="小库")
        self.assertEqual(vault["max_open_bytes"], 1024)
        self.assertEqual(vault["note"], "小库")
        with self.assertRaises(fish.KBError):
            self._add(max_open_bytes=0)

    # ============================================================ get / default
    def test_get_default_requires_explicit_default(self):
        """Q12：默认库靠 `is_default` 显式指定，不靠登记顺序。"""
        self._add(name="A")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_vault_get(self.conn)
        self.assertEqual(ctx.exception.code, "vault_missing")
        first = fish.kb_vault_get(self.conn, "A")
        self.assertEqual(first["name"], "A")

    def test_set_default_is_unique(self):
        a = self._add(name="A")
        second = self.tmp / "vault-b"
        second.mkdir()
        b = self._add(name="B", path=str(second))
        self.assertNotEqual(a["vault_id"], b["vault_id"])
        fish.kb_vault_set_default(self.conn, "B")
        defaults = [v["name"] for v in fish.kb_vault_list(self.conn) if v["is_default"]]
        self.assertEqual(defaults, ["B"], "默认库必须唯一")
        self.assertEqual(fish.kb_vault_get(self.conn)["vault_id"], b["vault_id"])
        fish.kb_vault_set_default(self.conn, a["vault_id"])   # 按 id 也认
        self.assertEqual(fish.kb_vault_get(self.conn)["name"], "A")

    def test_get_by_id_and_unknown(self):
        vault = self._add()
        self.assertEqual(fish.kb_vault_get(self.conn, vault["vault_id"])["name"], "测试库")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_vault_get(self.conn, "不存在")
        self.assertEqual(ctx.exception.code, "vault_missing")

    # ============================================================ set-mode
    def test_set_mode_roundtrip(self):
        self._add()
        vault = fish.kb_vault_set_mode(self.conn, "测试库", "rw")
        self.assertEqual(vault["mode"], "rw")
        self.assertEqual(fish.kb_vault_set_mode(self.conn, "测试库", "ro")["mode"], "ro")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_vault_set_mode(self.conn, "测试库", "rwx")
        self.assertEqual(ctx.exception.code, "bad_usage")

    # ============================================================ scan
    def test_scan_counts_files_notes_bytes_without_reading(self):
        self._add()
        res = fish.kb_vault_scan(self.conn, "测试库")
        # vault 里：00-首页/欢迎.md + 附件.png = 2 文件 / 1 笔记
        self.assertEqual(res["files"], 2)
        self.assertEqual(res["notes"], 1)
        self.assertGreater(res["bytes"], 0)
        row = self.conn.execute(
            "SELECT last_scan_at,last_scan_files,last_scan_notes,last_scan_bytes "
            "FROM kb_vault").fetchone()
        self.assertEqual(row[1:], (2, 1, res["bytes"]))
        self.assertTrue(row[0])
        opened = self.conn.execute(
            "SELECT COUNT(*) FROM kb_access WHERE action='scan'").fetchone()[0]
        self.assertEqual(opened, 1, "scan 应留一条审计")

    def test_scan_offline_raises_offline(self):
        self._add()
        shutil.rmtree(self.vault_dir)
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_vault_scan(self.conn, "测试库")
        self.assertEqual(ctx.exception.code, "offline")

    # ============================================================ remove
    def test_remove_deletes_row_but_not_disk(self):
        vault = self._add()
        res = fish.kb_vault_remove(self.conn, "测试库")
        self.assertTrue(res["removed"])
        self.assertTrue(res["disk_untouched"])
        self.assertEqual(self._count(), 0)
        self.assertTrue(self.vault_dir.is_dir(), "remove 不许动磁盘")
        self.assertTrue((self.vault_dir / "00-首页" / "欢迎.md").exists())
        # 审计仍可读（无 FK）
        fish.kb_log_access(self.conn, vault, rel="00-首页/欢迎.md",
                           action="open", citizen_id="CID1", via="cli:kb")
        rows = self.conn.execute("SELECT vault_id FROM kb_access").fetchall()
        self.assertEqual([r[0] for r in rows], [vault["vault_id"]])

    def test_remove_unknown_raises(self):
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_vault_remove(self.conn, "没有这个库")
        self.assertEqual(ctx.exception.code, "vault_missing")

    # ============================================================ CLI
    def test_cli_add_twice_keeps_one_row(self):
        """验收 ②：CLI 侧同一路径 add 两次 → 两次都 exit 0，行数不变。"""
        for _ in range(2):
            code, out, err = self._run_main(
                ["--root", str(self.root), "kb", "vault", "add",
                 "--name", "命令行库", "--path", str(self.vault_dir), "--mode", "rw"])
            self.assertIsNone(code, f"vault add 应 exit 0；stderr={err}")
            self.assertIn("已登记", out)
        self.assertEqual(self._count(), 1, "CLI 两次 add 后行数必须仍是 1")

    def test_cli_list_and_default(self):
        self._run_main(["--root", str(self.root), "kb", "vault", "add",
                        "--name", "命令行库", "--path", str(self.vault_dir), "--default"])
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "vault", "list"])
        self.assertIsNone(code, f"list 应 exit 0；stderr={err}")
        self.assertIn("命令行库", out)
        self.assertIn("default", out)

    def test_cli_mode_and_path_errors(self):
        code, _out, err = self._run_main(
            ["--root", str(self.root), "kb", "vault", "add",
             "--name", "X", "--path", str(self.tmp / "不存在")])
        self.assertEqual(code, 1, f"路径不存在应为域错误 exit 1；stderr={err}")
        self.assertIn("not_found", err)
        code, _out, err = self._run_main(
            ["--root", str(self.root), "kb", "vault", "add",
             "--name", "X", "--path", str(self.vault_dir), "--mode", "rwx"])
        self.assertEqual(code, 2, "非法 --mode 是用法错误（argparse choices）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
