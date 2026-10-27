#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QA 对抗性补测（B1/B2/B3 · 2026-10-26，独立于工程师测试）。

这份文件只回答一个问题：**判据在"坏输入"下还站得住吗**。它不复述
`test_layout` / `test_kb_*` / `test_project_status` 已覆盖的正例，而是逐条压边界：

- 路径：大小写变体、尾随点/空格、假前缀（`vault-evil`）、`..%2f` 编码、
  超长路径、设备名（CON/NUL）、空路径；以及 **tree/search 是否顺着 junction 越界**。
- 写：并发两次 update（乐观锁）、`content` 为空、坏 frontmatter（两形态）、
  超大 content、以及 `90-模板/` 是否真的禁写（裁决 Q13）。
- tree：空 vault、单文件 vault、目录符号链接（非 junction）的行为。
- 状态表：空 registry、全 retired、既无档期又无记忆的项目。

证据面纪律（同 `test_layout.py` 头注）：全部 `tempfile.mkdtemp()` 隔离；
**不**指向 `p0/data`、**不**指向真实 vault；断言只认 `KBError.code` 与可观察文件效果，
不认文案。三条不足按 PRD/裁决变红的用例，docstring 里写了判据来源（它们就是
Round 1 的失败面：junction 越界 / 90-模板 未禁写 / 空路径 open 泄漏 OSError）。
"""
import datetime
import hashlib
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
AS_OF = datetime.datetime(2026, 10, 26, 0, 0, 0, tzinfo=TZ8)


def _mk_junction(link, target) -> bool:
    """目录联接（junction）：`mklink /J`，无需管理员；失败返回 False。"""
    try:
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and Path(link).exists()


def _mk_dirlink(link, target) -> bool:
    """目录符号链接：`mklink /D`（可能需要开发者模式/提权）；失败返回 False。"""
    try:
        proc = subprocess.run(["cmd", "/c", "mklink", "/D", str(link), str(target)],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and Path(link).exists()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _VaultCase(unittest.TestCase):
    """公共夹具：临时 data + 临时 vault（rw）+ 一条 rw / 一条 ro 注册。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qa-b1b2b3-"))
        self.root = self.tmp / "data"
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "10-笔记").mkdir(parents=True)
        (self.vault_dir / "10-笔记" / "目标.md").write_text(
            "# 目标\n\nQA 对抗性正文。\n", encoding="utf-8")
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.vault_dir / "90-模板").mkdir()
        (self.vault_dir / "90-模板" / "模板.md").write_text("# 模板\n", encoding="utf-8")
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault = fish.kb_vault_add(self.conn, name="QA库", path=str(self.vault_dir),
                                       mode="rw", is_default=True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def code(self, fn):
        try:
            fn()
        except fish.KBError as exc:
            return exc.code
        return "NO_ERROR"

    def write(self, rel, action, *, content=None, base=None, frontmatter=None):
        return fish.kb_write(self.conn, self.root, self.vault, rel, action=action,
                             content=content, base_sha256=base, frontmatter=frontmatter)


class QAKBPathTest(_VaultCase):
    """KB-11 的对抗面：别名、编码、假前缀、设备名、超长路径、junction 越界。"""

    def test_case_variant_reads_same_file(self):
        """Windows 大小写不敏感：`10-笔记/目标.MD` 应解析到同一文件且仍在库内。"""
        res = fish.kb_open(self.conn, self.vault, "10-笔记/目标.MD")
        self.assertEqual(res["sha256"],
                         _sha((self.vault_dir / "10-笔记" / "目标.md").read_bytes()))
        self.assertTrue(Path(res["abs"]).is_relative_to(self.vault_dir.resolve()))

    def test_trailing_dot_or_space_never_escapes_vault(self):
        """尾随点/空格是 Windows 的别名面：要么成功且仍在库内，要么给 KBError。"""
        for rel in ("10-笔记/目标.md.", "10-笔记/目标.md ", "10-笔记/ 目标.md"):
            with self.subTest(rel=rel):
                try:
                    res = fish.kb_open(self.conn, self.vault, rel)
                except fish.KBError:
                    continue
                self.assertTrue(Path(res["abs"]).is_relative_to(self.vault_dir.resolve()),
                                f"{rel} 解析到了库外：{res['abs']}")

    def test_vault_evil_fake_prefix_is_rejected(self):
        """假前缀：`…\\vault-evil` 与 `…\\vault` 只是字符串前缀（禁止 startswith）。"""
        evil = self.tmp / "vault-evil"
        evil.mkdir()
        (evil / "x.md").write_text("evil\n", encoding="utf-8")
        link = self.vault_dir / "evil-link"
        if not _mk_junction(link, evil):
            self.skipTest("mklink /J 不可用（环境限制）")
        self.assertEqual(self.code(lambda: fish.kb_open(self.conn, self.vault,
                                                        "evil-link/x.md")),
                         "outside_vault")

    def test_percent_encoded_traversal_is_not_decoded(self):
        """`..%2f` 不decoded：应被当成普通文件名（not_found），绝不穿越。"""
        self.assertIn(self.code(lambda: fish.kb_open(
            self.conn, self.vault, "..%2f..%2fetc%2fpasswd")),
            ("not_found", "path_invalid"))

    def test_very_long_path_gives_domain_error(self):
        """超长相对路径：给 KBError，不许抛 OSError/WinError 或挂住。"""
        rel = "/".join(["d" * 40] * 8) + "/x.md"       # 约 330 字符
        self.assertIn(self.code(lambda: fish.kb_open(self.conn, self.vault, rel)),
                      ("not_found", "path_invalid"))

    def test_device_names_are_rejected(self):
        """设备名（CON/NUL/COM1）：必须是 KBError，绝不打到设备上。"""
        for rel in ("CON", "NUL", "COM1", "CON.md", "NUL.md"):
            with self.subTest(rel=rel):
                self.assertIn(self.code(lambda rel=rel: fish.kb_open(self.conn, self.vault, rel)),
                              ("not_found", "path_invalid", "media"))

    def test_empty_path_open_is_domain_error(self):
        """PRD KB-11 把"空路径"列为必须拒绝且错误可判的一种。

        读路径空串在 tree 里等于根（偏离记录 B3.9），但 `open` 的目标是**文件**：
        它必须给 `KBError`（not_found / path_invalid / bad_usage），而不是让
        `Path.read_bytes()` 抛 `PermissionError` 穿透到调用方（CLI 会打 Traceback）。
        """
        self.assertRaises(fish.KBError, fish.kb_open, self.conn, self.vault, "")

    def test_tree_and_search_do_not_descend_junction(self):
        """PRD §7.1：vault 内 junction/符号链接"命中即拒绝"；§7.5 的边界是
        "知识库内容不得离开库"。`kb_resolve_target` 已拒绝显式 junction 路径，
        但 `tree`/`search` 的遍历若顺着 junction 走，就会把库外文件读进回执——
        等于同一棵树上有两套边界。这条断言：库外内容既不出现在 tree，也不被 search 命中。
        """
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "库外秘密.md").write_text("OUTSIDE-SECRET-QA\n", encoding="utf-8")
        link = self.vault_dir / "jump"
        if not _mk_junction(link, outside):
            self.skipTest("mklink /J 不可用（环境限制）")
        tree = fish.kb_tree(self.conn, self.vault, rel="", depth=3)
        self.assertNotIn("库外秘密.md", tree["text"], "tree 顺着 junction 列出了库外文件")
        search = fish.kb_search(self.conn, self.vault, "OUTSIDE-SECRET-QA")
        self.assertEqual(search["total"], 0, "search 顺着 junction 读到了库外内容")


class QAKBWriteTest(_VaultCase):
    """写三档的对抗面：乐观锁、空正文、坏 frontmatter、超大正文、模板目录。"""

    def test_concurrent_update_second_is_rejected(self):
        """并发两次 update：后写必须被乐观锁拒绝（不能静默覆盖别人的改动）。"""
        self.write("10-笔记/并发.md", "create", content="v1\n")
        base = _sha((self.vault_dir / "10-笔记" / "并发.md").read_bytes())
        self.write("10-笔记/并发.md", "update", content="v2\n", base=base)
        self.assertEqual(self.code(lambda: self.write(
            "10-笔记/并发.md", "update", content="v3\n", base=base)), "stale_base")
        self.assertEqual((self.vault_dir / "10-笔记" / "并发.md").read_text(
            encoding="utf-8"), "v2\n", "后写被拒后文件内容不该变")

    def test_empty_content_is_explicit(self):
        """空正文：要么明确拒绝（bad_usage），要么真的写出一份空文件；不许"报成功但没文件"。"""
        try:
            res = self.write("10-笔记/空.md", "create", content="")
        except fish.KBError as exc:
            self.assertIn(exc.code, ("bad_usage", "bad_frontmatter", "content_too_large"))
            return
        path = self.vault_dir / "10-笔记" / "空.md"
        self.assertTrue(path.exists(), f"回执说成功但文件不存在：{res}")
        self.assertEqual(path.stat().st_size, res["bytes_after"])

    def test_bad_frontmatter_in_body_is_rejected(self):
        """正文自带 frontmatter（`---` 开头）→ 会写出两个文件头，必须拒绝。"""
        self.assertEqual(self.code(lambda: self.write(
            "10-笔记/双头.md", "create", content="---\ntitle: x\n---\n正文\n")),
            "bad_frontmatter")

    def test_unterminated_existing_frontmatter_is_rejected(self):
        """既有文件 frontmatter 没有收尾 `---` → update 不许把它当正文重写。"""
        p = self.vault_dir / "10-笔记" / "坏头.md"
        p.write_text("---\ntitle: 没有收尾\n正文在这里\n", encoding="utf-8")
        self.assertEqual(self.code(lambda: self.write(
            "10-笔记/坏头.md", "update", content="新正文\n",
            base=_sha(p.read_bytes()))), "bad_frontmatter")

    def test_oversized_content_is_rejected(self):
        """单次写入上限 512 KiB（§6.3）：超 1 字节即拒。"""
        big = "x" * (fish.KB_MAX_CONTENT_BYTES + 1)
        self.assertEqual(self.code(lambda: self.write(
            "10-笔记/超大.md", "create", content=big)), "content_too_large")

    def test_write_into_template_dir_is_forbidden(self):
        """裁决 Q13（本批必须遵守）：**禁止 AI 写 `90-模板/`**。

        模板决定人以后所有产出，风险高于普通笔记。当前实现默认忽略规则只有
        `.obsidian/**` 与 `.trash/**`，所以这条现在是红的（Round 1 记录）。
        """
        code = self.code(lambda: self.write("90-模板/新模板.md", "create", content="# t\n"))
        self.assertNotEqual(code, "NO_ERROR",
                            "90-模板/ 被写成功了：裁决 Q13 要求禁止 AI 写模板目录")


class QAKBTreeTest(_VaultCase):
    """tree 的退化形态：空库、单文件库、目录符号链接。"""

    def test_empty_vault_tree(self):
        empty = self.tmp / "empty-vault"
        empty.mkdir()
        vault = fish.kb_vault_add(self.conn, name="空库", path=str(empty), mode="ro")
        res = fish.kb_tree(self.conn, vault, rel="", depth=2)
        self.assertEqual(res["scan"]["files"], 0)
        self.assertEqual(res["chars"], len(res["text"]))
        self.assertLessEqual(res["chars"], 8000)
        self.assertIn("0 篇笔记", res["text"])

    def test_single_file_vault(self):
        one = self.tmp / "one-vault"
        one.mkdir()
        (one / "唯一.md").write_text("独一份\n", encoding="utf-8")
        vault = fish.kb_vault_add(self.conn, name="单文件库", path=str(one), mode="ro")
        res = fish.kb_tree(self.conn, vault, rel="", depth=1)
        self.assertEqual(res["scan"]["files"], 1)
        self.assertEqual(res["summary"]["notes"], 1)
        self.assertEqual(fish.kb_search(self.conn, vault, "独一份")["total"], 1)

    def test_dir_symlink_is_not_followed(self):
        """目录符号链接（非 junction）：不下降；显式路径读到它 → reparse_point。"""
        outside = self.tmp / "outside-sym"
        outside.mkdir()
        (outside / "秘密.md").write_text("SYMLINK-SECRET\n", encoding="utf-8")
        link = self.vault_dir / "symdir"
        if not _mk_dirlink(link, outside):
            self.skipTest("mklink /D 不可用（需要开发者模式/提权）")
        tree = fish.kb_tree(self.conn, self.vault, rel="", depth=3)
        self.assertNotIn("秘密.md", tree["text"], "tree 顺着目录符号链接列出了库外文件")
        self.assertEqual(fish.kb_search(self.conn, self.vault, "SYMLINK-SECRET")["total"], 0)
        # 顺序按设计 §5.1：④ commonpath 先判、⑤ reparse 后判；指向库外的链接由前者拒绝。
        self.assertIn(self.code(lambda: fish.kb_open(self.conn, self.vault,
                                                     "symdir/秘密.md")),
                      ("outside_vault", "reparse_point"))


class QAStatusTableTest(unittest.TestCase):
    """B2 状态表的空/全退役/无证据形态（不写盘：只读 `project_status_rows`）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qa-pst-"))
        self.root = self.tmp / "data"
        self.home = self.tmp / "fish-home"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        return fish.project_status_rows(self.conn, self.root, as_of=AS_OF,
                                        home_root=self.home)

    def test_empty_registry(self):
        res = self.rows()
        self.assertEqual(res["rows"], [])
        self.assertEqual(res["retired_total"], 0)
        out = fish.render_project_status(self.conn, self.root, self.home, as_of=AS_OF)
        text = (self.home / fish.LAYOUT_STATUS_TABLE).read_text(encoding="utf-8")
        self.assertIn("—（registry 里没有项目）", text)
        self.assertNotIn("<details>", text, "没有 retired 时不该出现折叠区")
        self.assertEqual(out["rows"], 0)

    def test_all_retired(self):
        for prefix in ("R1", "R2"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status)"
                " VALUES (?,?,NULL,0,'retired')", (prefix, prefix + " 项目"))
        self.conn.commit()
        res = self.rows()
        self.assertEqual(res["rows"], [], "retired 默认不进主表")
        self.assertEqual(res["retired_total"], 2)
        text = ""
        fish.render_project_status(self.conn, self.root, self.home, as_of=AS_OF)
        text = (self.home / fish.LAYOUT_STATUS_TABLE).read_text(encoding="utf-8")
        self.assertIn("已退役（2）", text)
        self.assertNotIn("| R1 项目（`R1`） | 已退役", text.split("<details>")[0])

    def test_project_without_stint_or_memory_shows_three_dashes(self):
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status)"
            " VALUES ('N1','没有证据的项目',NULL,0,'active')")
        self.conn.commit()
        res = self.rows()
        self.assertEqual(len(res["rows"]), 1)
        row = res["rows"][0]
        self.assertEqual(row["last_active"], "—（无记忆）")
        self.assertEqual(row["board_entry"], "—（今日无看板）")
        self.assertEqual(row["owners"], "—（无开档期）")
        self.assertEqual(row["open_loops"], 0)
        self.assertNotIn("None", repr(row))
        self.assertEqual(len(row["sources"]), 5, "每列都要有来源（status 也有一格）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
