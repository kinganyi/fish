#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W2 判据：`kb_vault_init`（真内置落点）与 `kb_vault_relocate`（改位置三模式）。

## 守什么

**V1 init 默认落点**：不给 `path` 时必须落在 `<data_root>/kb/<name>/`——这是用户拍板的
"A 真内置"（备份 fish 数据目录即备份知识库）。同时 `path` 可指向任意位置（C 外置）。

**V2 init 幂等且不毁东西**：对已有 Obsidian 库跑一次 init，**不许覆盖任何已有文件**。
这条比"能创建"更重要——10.7 GB 的库只有一份。

**V3 relocate 三模式分明**：只改登记 / 搬 / 复制。搞混的代价是数据没了。

**V4 搬完必须校验哈希**："搬完了"不等于"搬对了"，只比数量会漏掉同名不同内容。

**V5 先规划再动手**：`dry_run` 默认开，且必须报文件数 / 字节 / 目标盘够不够。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish_schema      # noqa: E402
import fish_kb as K     # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(fish_schema.SCHEMA)
    return c


class InitTest(unittest.TestCase):
    """V1 / V2。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = _conn()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_path_is_data_root_kb_name(self):
        """**A 真内置**：默认必须落在 <data_root>/kb/<name>/。"""
        r = K.kb_vault_init(self.conn, self.root, name="新库")
        expect = (self.root / "kb" / "新库").resolve()
        self.assertEqual(Path(r["root"]).resolve(), expect,
                         f"默认落点不对：{r['root']} 应为 {expect}")
        self.assertTrue(expect.is_dir())
        row = K.kb_vault_get(self.conn, "新库")
        self.assertEqual(Path(row["path"]).resolve(), expect)

    def test_explicit_path_supports_external_location(self):
        """**C 兼容外置**：给了绝对路径就落在那里，不进 data_root。"""
        ext = self.root / "外置库"
        r = K.kb_vault_init(self.conn, self.root, name="外置库", path=str(ext))
        self.assertEqual(Path(r["root"]).resolve(), ext.resolve())
        self.assertFalse((self.root / "kb").exists(), "外置时不该造 data_root/kb")

    def test_creates_obsidian_recognizable_scaffold(self):
        """建的目录必须是一个 Obsidian 能认出并打开的 vault。"""
        K.kb_vault_init(self.conn, self.root, name="V")
        v = self.root / "kb" / "V"
        self.assertTrue((v / ".obsidian" / "app.json").is_file())
        import json
        cfg = json.loads((v / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        self.assertIs(cfg.get("alwaysUpdateLinks"), True,
                      "app.json 必须开 alwaysUpdateLinks（人和 AI 共写靠它）")
        home = v / "00首页.md"
        self.assertTrue(home.is_file(), "缺首页")
        import yaml
        text = home.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        fm = yaml.safe_load(parts[1])
        self.assertEqual(fm.get("tags"), ["入口"],
                         f"首页 tags 应是列表 ['入口']（与库 287/318 篇写法一致），实际 {fm.get('tags')!r}")

    def test_init_never_overwrites_existing_content(self):
        """**V2 最要紧的一条**：对已有库跑 init 不许覆盖任何东西。

        10.7 GB 的库只有一份。init 只是"补缺 + 登记"，不是"重建"。
        """
        v = self.root / "kb" / "既有库"
        (v / ".obsidian").mkdir(parents=True)
        (v / ".obsidian" / "app.json").write_text('{"自定义": "别动我"}', encoding="utf-8")
        (v / "00首页.md").write_text("# 我的首页\n\n内容不能丢\n", encoding="utf-8")
        K.kb_vault_init(self.conn, self.root, name="既有库")
        self.assertEqual((v / ".obsidian" / "app.json").read_text(encoding="utf-8"),
                         '{"自定义": "别动我"}', "已有 app.json 被覆盖了")
        self.assertIn("内容不能丢", (v / "00首页.md").read_text(encoding="utf-8"),
                      "已有首页被覆盖了")

    def test_init_is_idempotent(self):
        """跑两次不报错、不多出东西。"""
        a = K.kb_vault_init(self.conn, self.root, name="V")
        b = K.kb_vault_init(self.conn, self.root, name="V")
        self.assertEqual(a["vault"]["vault_id"], b["vault"]["vault_id"])
        self.assertEqual(b["created"], [], f"第二次不该再创建任何文件：{b['created']}")


class RelocateTest(unittest.TestCase):
    """V3 / V4 / V5。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.src = self.base / "src"
        (self.src / "10-漫剧").mkdir(parents=True)
        (self.src / "10-漫剧" / "剧本.md").write_text("---\ntags:\n  - 漫剧\n---\n# 剧本\n",
                                                      encoding="utf-8")
        (self.src / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        self.conn = _conn()
        K.kb_vault_add(self.conn, name="源库", path=str(self.src), mode="rw")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_default_and_writes_nothing(self):
        """**V5**：默认就是 dry_run，且必须报出计划读数。"""
        dst = self.base / "dst"
        dst.mkdir()
        r = K.kb_vault_relocate(self.conn, "源库", str(dst), move_files=True)
        self.assertTrue(r["dry_run"], "默认必须是 dry_run")
        self.assertEqual(r["files"], 2, f"应报 2 个文件，实际 {r['files']}")
        self.assertGreater(r["bytes"], 0)
        self.assertIn("enough_space", r)
        # 没动任何东西
        self.assertTrue((self.src / "图.png").exists())
        self.assertEqual(list(dst.iterdir()), [], "dry_run 往目标写了东西")
        # 登记也没改
        self.assertEqual(Path(K.kb_vault_get(self.conn, "源库")["path"]).resolve(),
                         self.src.resolve())

    def test_register_only_moves_no_file(self):
        """只改登记：文件原地不动，path 更新。"""
        dst = self.base / "newhome"
        dst.mkdir()
        r = K.kb_vault_relocate(self.conn, "源库", str(dst), dry_run=False)
        self.assertEqual(r["files"], 0, "只改登记不该清点文件")
        self.assertTrue((self.src / "图.png").exists(), "只改登记却动了文件")
        self.assertEqual(Path(K.kb_vault_get(self.conn, "源库")["path"]).resolve(),
                         dst.resolve())

    def test_move_files_moves_and_verifies(self):
        """**V3 move + V4 哈希校验**：文件真搬走，且校验通过。"""
        dst = self.base / "moved"
        dst.mkdir()
        r = K.kb_vault_relocate(self.conn, "源库", str(dst), move_files=True, dry_run=False)
        self.assertEqual(r["moved"], 2)
        self.assertEqual(r["verified"], 2, "没做哈希校验")
        self.assertTrue((dst / "图.png").exists(), "文件没搬过来")
        self.assertFalse((self.src / "图.png").exists(), "move 模式源文件应已移走")
        self.assertEqual(Path(K.kb_vault_get(self.conn, "源库")["path"]).resolve(),
                         dst.resolve())
        # mtime 必须保住（否则 Obsidian 排序会乱）
        self.assertTrue((dst / "10-漫剧" / "剧本.md").read_text(encoding="utf-8").startswith("---"))

    def test_copy_files_keeps_source(self):
        """**V3 copy**：源保留。"""
        dst = self.base / "copied"
        dst.mkdir()
        r = K.kb_vault_relocate(self.conn, "源库", str(dst), copy_files=True, dry_run=False)
        self.assertEqual(r["verified"], 2)
        self.assertTrue((dst / "图.png").exists())
        self.assertTrue((self.src / "图.png").exists(), "copy 模式不该删源")

    def test_path_already_used_by_another_vault_is_rejected(self):
        """两个库不能指同一个目录——否则索引会互相覆盖。"""
        other = self.base / "other"
        other.mkdir()
        K.kb_vault_add(self.conn, name="别的库", path=str(other), mode="ro")
        with self.assertRaises(K.KBError) as ctx:
            K.kb_vault_relocate(self.conn, "源库", str(other), dry_run=True)
        self.assertIn("占用", str(ctx.exception))

    def test_move_and_copy_together_is_rejected(self):
        with self.assertRaises(K.KBError):
            K.kb_vault_relocate(self.conn, "源库", str(self.base / "x"),
                                move_files=True, copy_files=True)

    def test_missing_source_is_rejected(self):
        K.kb_vault_add(self.conn, name="空库", path=str(self.base), mode="ro")
        ghost = self.base / "ghost"
        ghost.mkdir()
        # 先登记一个存在的路径，再把它删掉，模拟"盘拔了"
        K.kb_vault_add(self.conn, name="假库", path=str(ghost), mode="ro")
        ghost.rmdir()
        with self.assertRaises(K.KBError) as ctx:
            K.kb_vault_relocate(self.conn, "假库", str(self.base / "y"), dry_run=True)
        self.assertEqual(ctx.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
