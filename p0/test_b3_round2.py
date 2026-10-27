#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3/B1 第二轮修复的判据（R1–R21 + N2 · 2026-10-26）。

这份文件**逐条**对应 `docs/主理人裁决-第二轮修复.md` 的"必须修"清单：
每一条修复至少一条断言，且每条断言都能被一条变异打红（见
`docs/变异自证-B1B2B3-v0.1.md` 的 M-R* 段与 `mutation_b2b3_selfcheck.py`）。

判据纪律（与 `test_layout.py` / `test_kb_*` 同构）：
  · 全部 `tempfile.mkdtemp()` 隔离；**不**指向 `p0/data`，**不**指向真实 vault；
  · 只认 `KBError.code`、审计行、可观察文件效果与固定文案片段，不认实现细节；
  · 环境限制（`mklink /J` 不可用等）→ `skipTest` 并明说，绝不把判据改成"看起来过了"。
"""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

# 拆包（2026-10-27 · kb 拆包 R1）：kb 的实现整段搬进了 `fish_kb`。
# `mock.patch.object(fish, "<kb 内部函数>")` 从此打不到调用点——函数体在 `fish_kb` 的
# globals 里解析名字，`fish` 上那个只是同一个对象的别名。所以这几处 patch 目标改指
# `fish_kb`。**只改目标，不改既有断言**（唯一例外见 R21：那里补了一条断言，
# 把"patch 打空导致静默变弱"变成响亮的红）。
import fish_kb  # noqa: E402 —— 与 fish.py 同一个路径下，测试用 cwd 导入

HERE = Path(__file__).resolve().parent


def _mk_junction(link, target) -> bool:
    """目录联接（`mklink /J`，无需管理员）；不可用返回 False（调用方 skip）。"""
    try:
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and Path(link).exists()


def _sha(path) -> str:
    return fish.kb_sha256_file(path)


class _Round2Case(unittest.TestCase):
    """公共夹具：临时 data + 临时 vault（rw，含敏感文件/模板目录/附件）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-r2-"))
        self.root = self.tmp / "data"
        self.home = self.tmp / "fish-home"
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "00-首页").mkdir(parents=True)
        (self.vault_dir / "00-首页" / "欢迎.md").write_text(
            "# 欢迎\n\n这是第一篇。\n", encoding="utf-8")
        (self.vault_dir / "10-项目").mkdir()
        (self.vault_dir / "10-项目" / "笔记.md").write_text(
            "# 笔记\n\n普通正文。\n", encoding="utf-8")
        (self.vault_dir / "attachments").mkdir()
        (self.vault_dir / "attachments" / "图.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.vault_dir / "90-模板").mkdir()
        (self.vault_dir / "90-模板" / "旧模板.md").write_text("# 模板\n", encoding="utf-8")
        (self.vault_dir / "80-资料库").mkdir()
        (self.vault_dir / "80-资料库" / "凭证管理.md").write_text(
            "# 凭证\n\n数据库密码：P@ss-唯一探针-7\n", encoding="utf-8")
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault = fish.kb_vault_add(self.conn, name="第二轮库",
                                       path=str(self.vault_dir), mode="rw",
                                       is_default=True)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 工具
    def code(self, fn) -> str:
        try:
            fn()
        except fish.KBError as exc:
            return exc.code
        return "NO_ERROR"

    def write(self, rel, action, *, content=None, base=None, frontmatter=None):
        return fish.kb_write(self.conn, self.root, self.vault, rel, action=action,
                             content=content, base_sha256=base,
                             frontmatter=frontmatter)

    def access_rows(self, action=None):
        sql = "SELECT path,result,meta FROM kb_access"
        params = ()
        if action:
            sql += " WHERE action=?"
            params = (action,)
        return self.conn.execute(sql, params).fetchall()

    def last_meta(self, action="reject"):
        row = self.conn.execute(
            "SELECT meta FROM kb_access WHERE action=? ORDER BY id DESC LIMIT 1",
            (action,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}


# ==================================================================== R1
class R1SearchPrivacyTest(_Round2Case):

    def test_failed_search_never_stores_query_plaintext(self):
        """R1（评审 🔴-1 / 安全 F2）：失败的 search 只留 `query_hash`，`path` 恒为空串。"""
        probe = "私密探针词-银行卡密码-1234567"
        # ① 在线但 limit 非法 → bad_usage
        self.assertEqual(self.code(lambda: fish.kb_search(
            self.conn, self.vault, probe, limit="abc")), "bad_usage")
        # ② 离线（vault 目录改名）→ offline
        moved = self.tmp / "vault-moved"
        self.vault_dir.rename(moved)
        self.assertEqual(self.code(lambda: fish.kb_search(
            self.conn, self.vault, probe)), "offline")

        rows = self.access_rows("reject")
        self.assertGreaterEqual(len(rows), 2, "失败路径必须留 reject 审计")
        for path, _result, meta in rows:
            self.assertEqual(path, "", f"reject 行的 path 必须为空串，实际 {path!r}")
            self.assertNotIn(probe, json.dumps(meta or "", ensure_ascii=False))
        dumped = json.dumps([list(r) for r in self.conn.execute(
            "SELECT path,meta,sha256,backup_path FROM kb_access").fetchall()],
            ensure_ascii=False)
        self.assertNotIn(probe, dumped, "query 原文漏进了 kb_access（任何列都不行）")
        metas = [json.loads(r[2]) for r in rows if r[2]]
        self.assertTrue(any(m.get("query_hash") for m in metas),
                        "失败行至少应留下 query_hash（否则无法按检索词聚合）")

    def test_success_path_still_keeps_query_out_of_audit(self):
        fish.kb_search(self.conn, self.vault, "普通正文")
        dumped = json.dumps([list(r) for r in self.conn.execute(
            "SELECT path,meta FROM kb_access").fetchall()], ensure_ascii=False)
        self.assertNotIn("普通正文", dumped)


# ==================================================================== R2
class R2BadEncodingWriteTest(_Round2Case):

    def setUp(self):
        super().setUp()
        self.gbk = self.vault_dir / "10-项目" / "旧编码.md"
        self.gbk.write_bytes("# 中文标题\r\n\r\n这是 GBK 编码的正文。\r\n".encode("gb18030"))
        self.before = self.gbk.read_bytes()

    def test_append_on_non_utf8_is_rejected_and_bytes_unchanged(self):
        """R2（评审 🟠-1）：GB18030 的 append 过去会写出"混合编码"且校验通过。"""
        code = self.code(lambda: self.write("10-项目/旧编码.md", "append",
                                            content="追加段\n"))
        self.assertEqual(code, "bad_encoding")
        self.assertEqual(self.gbk.read_bytes(), self.before, "被拒绝的写动了文件字节")

    def test_update_on_non_utf8_is_rejected_and_bytes_unchanged(self):
        code = self.code(lambda: self.write(
            "10-项目/旧编码.md", "update", content="新正文\n",
            base=fish.kb_sha256_bytes(self.before)))
        self.assertEqual(code, "bad_encoding")
        self.assertEqual(self.gbk.read_bytes(), self.before)
        # 读路径不受影响（拒绝要有边界：GB18030 仍然可读）
        res = fish.kb_open(self.conn, self.vault, "10-项目/旧编码.md")
        self.assertEqual(res["encoding"], "gb18030")


# ==================================================================== R3
class R3TreeDepthTest(_Round2Case):

    def setUp(self):
        super().setUp()
        deep = self.vault_dir / "一级L1" / "二级L2" / "三级L3"
        deep.mkdir(parents=True)
        (deep / "四级L4.md").write_text("# L4\n\n最深处。\n", encoding="utf-8")
        (self.vault_dir / "一级L1" / "自己.md").write_text("# L1\n", encoding="utf-8")

    def _count_scandir(self, depth):
        calls = []
        real = os.scandir

        def counting(path):
            calls.append(str(path))
            return real(path)

        with mock.patch("os.scandir", counting):
            res = fish.kb_tree(self.conn, self.vault, rel="", depth=depth,
                               max_chars=100000, max_tokens=100000)
        return res, calls

    def test_depth_really_crops_and_scandir_is_bounded(self):
        """R3：`depth=1` 不渲染第 3 层，且 scandir 次数 = 根 + 一级目录数。"""
        res1, calls1 = self._count_scandir(1)
        text1 = res1["text"]
        self.assertNotIn("二级L2", text1, "depth=1 不该出现二级目录")
        self.assertNotIn("三级L3", text1, "depth=1 不该出现三级目录")
        self.assertNotIn("四级L4.md", text1, "depth=1 不该出现深层文件名")
        self.assertIn("一级L1", text1)
        top_dirs = [p for p in self.vault_dir.iterdir()
                    if p.is_dir()
                    and not fish._kb_ignored(p.name + "/", self.vault["ignore_globs"])]
        self.assertLessEqual(len(calls1), 1 + len(top_dirs),
                             f"depth=1 的 scandir 次数必须受控：{calls1}")
        self.assertTrue(res1["scan"]["partial"], "深层未展开必须如实标注 partial")
        self.assertGreater(res1["scan"]["hidden_dirs"], 0)

        res4, calls4 = self._count_scandir(4)
        self.assertIn("四级L4.md", res4["text"], "depth=4 应能看到最深处")
        self.assertGreater(len(calls4), len(calls1), "更深必须真的多走目录")
        self.assertLess(res1["chars"], res4["chars"], "depth=1 的输出应明显更短")

    def test_root_node_uses_vault_name(self):
        """R3：根节点渲染成 vault 名，不是 `//`（评审 🟠-2 的具体缺陷）。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=2)
        self.assertIn("- 第二轮库/", res["text"])
        self.assertNotIn("- //", res["text"])

    def test_single_traversal_summary_matches_tree(self):
        """R3：`scan` 与 `summary` 来自同一次遍历（不再走两遍，两个数字不许互相打脸）。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=4,
                           max_chars=100000, max_tokens=100000)
        self.assertEqual(res["scan"]["files"], res["summary"]["files"])
        self.assertEqual(res["scan"]["bytes"], res["summary"]["bytes"])


# ==================================================================== R4
class R4RollbackWriteIdTest(_Round2Case):

    def test_wrong_suffix_is_not_found_and_file_survives(self):
        """R4（评审 🟠-3 / 安全 F11）：只匹配前缀的 write_id 过去能撤销、还能删文件。"""
        res = self.write("10-项目/新建.md", "create", content="# 新建\n")
        target = self.vault_dir / "10-项目" / "新建.md"
        self.assertTrue(target.exists())
        access_id = str(res["write_id"]).split("-", 1)[0]
        wrong = f"{access_id}-{'0' * 8}"
        self.assertNotEqual(wrong, res["write_id"])
        self.assertEqual(self.code(lambda: fish.kb_rollback(
            self.conn, self.root, wrong)), "not_found")
        self.assertTrue(target.exists(), "错后缀的回滚不许动文件")
        back = fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertTrue(back["removed"], "正确 write_id 必须能撤")


# ==================================================================== R5
class R5TemplateDirTest(_Round2Case):

    def test_90_template_is_in_default_ignore_and_not_writable(self):
        """R5（裁决 Q13 / 安全 F9）：`90-模板/**` 是不可删的默认忽略项。"""
        self.assertIn("90-模板/**", fish.KB_DEFAULT_IGNORE_GLOBS)
        self.assertEqual(self.code(lambda: self.write(
            "90-模板/新模板.md", "create", content="# t\n")), "ignored")
        self.assertFalse((self.vault_dir / "90-模板" / "新模板.md").exists())
        self.assertEqual(self.code(lambda: fish.kb_open(
            self.conn, self.vault, "90-模板/旧模板.md")), "ignored")
        # 用户重登记（覆盖 ignore_globs）也不能把它删掉
        fish.kb_vault_add(self.conn, name="第二轮库", path=str(self.vault_dir),
                          mode="rw", ignore=["**/_media/**"])
        vault = fish.kb_vault_get(self.conn, "第二轮库")
        self.assertIn("90-模板/**", vault["ignore_globs"])


# ==================================================================== R6
class R6TraversalLinkTest(_Round2Case):

    def test_tree_and_search_never_descend_junction(self):
        """R6（QA F-QA-2）：遍历面遇 junction 不下钻、不列、不命中。"""
        outside = self.tmp / "库外"
        outside.mkdir()
        (outside / "库外秘密.md").write_text("OUTSIDE-R6-SECRET\n", encoding="utf-8")
        link = self.vault_dir / "jump"
        if not _mk_junction(link, outside):
            self.skipTest("mklink /J 不可用（环境限制）：junction 判据未验证")
        tree = fish.kb_tree(self.conn, self.vault, rel="", depth=4)
        self.assertNotIn("库外秘密.md", tree["text"], "tree 顺着 junction 列出了库外文件")
        self.assertNotIn("jump", tree["text"], "链接本身也不该被列成节点")
        self.assertEqual(fish.kb_search(self.conn, self.vault,
                                        "OUTSIDE-R6-SECRET")["total"], 0)
        self.assertEqual(tree["scan"]["files"], 4,
                         "文件数只能来自库内（.obsidian/90-模板 被忽略）")


# ==================================================================== R7
class R7BadPathDomainErrorTest(_Round2Case):

    def test_empty_and_directory_open_are_domain_errors(self):
        """R7（QA F-QA-3 / 安全 F7）：空路径与目录都给域错误码，且各落一条 reject。"""
        self.assertEqual(self.code(lambda: fish.kb_open(
            self.conn, self.vault, "")), "bad_usage")
        self.assertEqual(self.code(lambda: fish.kb_open(
            self.conn, self.vault, "10-项目")), "bad_usage")
        rejects = self.access_rows("reject")
        self.assertGreaterEqual(len(rejects), 2)
        self.assertEqual(self.last_meta()["reason_code"], "bad_usage")

    def test_device_names_nul_and_ads_are_rejected(self):
        """R7：设备名 / NUL 字符 / ADS 形态都不许落到 OS 层（错误码可判 + 有审计）。"""
        for rel in ("NUL", "CON.md", "00-首页/欢迎.md:hidden"):
            with self.subTest(rel=rel):
                self.assertEqual(self.code(lambda rel=rel: fish.kb_open(
                    self.conn, self.vault, rel)), "path_invalid")
                self.assertEqual(self.last_meta()["reason_code"], "path_invalid")
        self.assertEqual(self.code(lambda: fish.kb_open(
            self.conn, self.vault, "00-首页/a\x00b.md")), "path_invalid")
        # 审计里绝不许出现 NUL 字符本身（它是坏输入，不是事实）
        self.assertNotIn("\x00", json.dumps(
            [list(r) for r in self.conn.execute(
                "SELECT path,meta FROM kb_access").fetchall()], ensure_ascii=False))


# ==================================================================== R8
class R8SearchPathAndAttachmentsTest(_Round2Case):

    def test_search_lines_carry_full_path(self):
        """R8（QA F-QA-4 / PRD KB-4）：每条命中给库内**全路径** + 相对路径。"""
        res = fish.kb_search(self.conn, self.vault, "普通正文")
        self.assertEqual(res["total"], 1)
        self.assertIn(str(self.vault_dir / "10-项目" / "笔记.md"), res["text"])
        self.assertIn("10-项目/笔记.md", res["text"])

    def test_tree_counts_attachments_without_listing_names(self):
        """R8（QA F-QA-5 / PRD §7.3）：附件只给计数，不列文件名。"""
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=3)
        self.assertNotIn("图.png", res["text"])
        self.assertIn("附件", res["text"])
        self.assertEqual(res["summary"]["attachments"], 1)


# ==================================================================== R9
class R9MutationScriptEncodingTest(unittest.TestCase):

    def test_mutation_scripts_force_utf8_stdout(self):
        """R9（QA F-QA-6）：两个变异脚本启动即把 stdout 设成 UTF-8（cp936 下不再崩/假红）。"""
        for name in ("mutation_b2b3_selfcheck.py", "mutation_t05_selfcheck.py"):
            with self.subTest(script=name):
                src = (HERE / name).read_text(encoding="utf-8")
                self.assertIn('sys.stdout.reconfigure(encoding="utf-8"', src,
                              f"{name} 没有强制 UTF-8 stdout")
                self.assertIn('encoding="utf-8", errors="replace"', src,
                              f"{name} 的子进程捕获没有统一编码")
                self.assertIn('PYTHONIOENCODING="utf-8"', src,
                              f"{name} 没给子进程注入 UTF-8（cp936 下 expect 必然假红）")


# ==================================================================== R10
class R10IgnoreNormalizationTest(_Round2Case):

    def test_case_variants_and_trailing_dot_space_are_ignored(self):
        """R10（安全 F1）：`.OBSIDIAN/`、`.obsidian /`、`.TRASH/` 都必须命中忽略。"""
        (self.vault_dir / ".trash").mkdir()
        (self.vault_dir / ".trash" / "x.md").write_text("x\n", encoding="utf-8")
        for rel in (".OBSIDIAN/app.json", ".obsidian /app.json",
                    ".TRASH/x.md", ".trash./x.md"):
            with self.subTest(rel=rel):
                self.assertEqual(
                    self.code(lambda rel=rel: fish.kb_open(
                        self.conn, self.vault, rel)), "ignored",
                    f"大小写/尾随点空格变体绕过了忽略规则：{rel}")
        self.assertEqual(self.code(lambda: self.write(
            ".OBSIDIAN/坏插件.json", "create", content="{}")), "ignored")
        self.assertFalse((self.vault_dir / ".obsidian" / "坏插件.json").exists())

    def test_traversal_never_lists_hidden_dirs(self):
        (self.vault_dir / ".trash").mkdir()
        (self.vault_dir / ".trash" / "隐藏.md").write_text("x\n", encoding="utf-8")
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=3)
        self.assertNotIn("app.json", res["text"])
        self.assertNotIn("隐藏.md", res["text"])
        self.assertNotIn(".trash", res["text"].split("生效忽略规则")[1].split("\n", 1)[1])

    def test_rule_case_differs_from_disk_case_still_ignored(self):
        """R10 的第二层：**规则串**也要归一（磁盘真实大小写与规则不同时照样挡住）。

        为什么需要这一条（第四轮补）：`.OBSIDIAN/` 这类**字面**变体在 Windows 上会被
        `realpath` 归一成磁盘真实大小写，于是 S1 的"解析后复判"也接得住——只摘字面层
        的变异不再变红（那不是回归，是纵深防御）。规则串与磁盘大小写不同时，
        realpath 帮不上忙（落点就是 `Media/x.md`），只有 R10 的逐段归一是唯一防线。
        """
        media = self.vault_dir / "Media"
        media.mkdir()
        (media / "x.md").write_text("# x\n", encoding="utf-8")
        fish.kb_vault_add(self.conn, name="第二轮库", path=str(self.vault_dir),
                          mode="rw", ignore=["media/**"])
        vault = fish.kb_vault_get(self.conn, "第二轮库")
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, vault, "Media/x.md")),
            "ignored", "规则串大小写不同也必须命中")


# ==================================================================== R12
class R12FrontmatterInjectionTest(_Round2Case):

    def test_newline_in_value_is_rejected(self):
        """R12（安全 F8）：值里的换行/控制字符不许落盘成额外顶层属性。"""
        for value in ("正常值\nadmin: true\nsecret_note: 注入",
                      "x\n---\n正文提前闭合",
                      "bad\x07bell"):
            with self.subTest(value=value):
                self.assertEqual(self.code(lambda value=value: self.write(
                    "10-项目/注入.md", "create", content="正文\n",
                    frontmatter={"tags": value})), "bad_frontmatter")
                self.assertFalse((self.vault_dir / "10-项目" / "注入.md").exists())

    def test_written_frontmatter_is_single_line(self):
        self.write("10-项目/正常.md", "create", content="正文\n",
                   frontmatter={"tags": "认知", "status": "draft"})
        text = (self.vault_dir / "10-项目" / "正常.md").read_text(encoding="utf-8")
        head = text.split("---")[1]
        for line in [ln for ln in head.splitlines() if ln.strip()]:
            self.assertNotIn("\r", line)


# ==================================================================== R13
class R13WriteSizeTest(_Round2Case):

    def test_oversized_existing_file_is_not_written(self):
        """R13（安全 F5）：写路径与读路径同口径——超上限的既有文件拒绝改写。"""
        fish.kb_vault_add(self.conn, name="第二轮库", path=str(self.vault_dir),
                          mode="rw", max_open_bytes=64)
        self.vault = fish.kb_vault_get(self.conn, "第二轮库")
        big = self.vault_dir / "10-项目" / "大文件.md"
        big.write_text("x" * 200, encoding="utf-8")
        before = big.read_bytes()
        self.assertEqual(self.code(lambda: self.write(
            "10-项目/大文件.md", "append", content="y\n")), "too_large")
        self.assertEqual(big.read_bytes(), before)
        self.assertEqual(self.code(lambda: fish.kb_backup(
            self.root, self.vault, "10-项目/大文件.md", b"z" * 200, {})), "too_large")


# ==================================================================== R14
class R14HomeLocationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-r2-home-"))
        self.root = self.tmp / "data"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_drive_root_windir_and_user_home_are_rejected(self):
        """R14（安全 F13）：盘根 / %WINDIR% / 用户家目录根一律拒绝。"""
        drive_root = Path(self.root.anchor)
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, override=str(drive_root),
                                   allow_external_home=True)
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if windir:
            with self.assertRaises(RuntimeError):
                fish.resolve_home_root(self.root,
                                       override=str(Path(windir) / "fish-home"),
                                       allow_external_home=True)
        with self.assertRaises(RuntimeError):
            fish.resolve_home_root(self.root, override=str(Path.home()),
                                   allow_external_home=True)

    def test_external_location_requires_explicit_flag(self):
        external = Path(tempfile.mkdtemp(prefix="fish-r2-ext-"))
        try:
            with self.assertRaises(RuntimeError):
                fish.resolve_home_root(self.root, override=str(external))
            home = fish.resolve_home_root(self.root, override=str(external),
                                          allow_external_home=True)
            self.assertEqual(home, external.resolve())
        finally:
            shutil.rmtree(external, ignore_errors=True)

    def test_standard_sibling_needs_no_flag(self):
        """反向判据：合理位置（项目目录内）不该被迫加开关。"""
        sibling = self.tmp / "custom-home"
        self.assertEqual(fish.resolve_home_root(self.root, override=str(sibling)),
                         sibling.resolve())


# ==================================================================== R15
class R15SensitiveTest(_Round2Case):

    def test_sensitive_open_requires_explicit_flag(self):
        """R15（安全 F4）：凭证文件默认 `sensitive`，`allow_sensitive=True` 才给正文。"""
        rel = "80-资料库/凭证管理.md"
        err = None
        code = "NO_ERROR"
        try:
            fish.kb_open(self.conn, self.vault, rel)
        except fish.KBError as exc:
            err, code = exc, exc.code
        self.assertEqual(code, "sensitive", f"敏感文件没有拒读，code={code}")
        self.assertIn("凭证管理.md", err.message)
        self.assertIn("Obsidian", err.message, "拒读说明要给出可操作建议")
        self.assertIn("B", err.message, "要给出文件大小")
        self.assertNotIn("P@ss-唯一探针-7", err.message, "拒读说明里不许带正文")

        ok = fish.kb_open(self.conn, self.vault, rel, allow_sensitive=True)
        self.assertIn("P@ss-唯一探针-7", ok["text"])
        self.assertTrue(ok["sensitive"])
        self.assertTrue(any("显式授权" in s for s in ok["statements"]))

    def test_sensitive_names_are_built_in(self):
        for name in ("凭证.md", "credential.txt", "my-secret.json", "密码本.md",
                     "a.pem", "b.key", "id_rsa", ".env.local"):
            with self.subTest(name=name):
                self.assertTrue(fish._kb_is_sensitive_name(name), f"{name} 未被识别")

    def test_search_never_snippets_sensitive_files(self):
        """R15：`search` 对敏感文件只计数、不进片段。"""
        res = fish.kb_search(self.conn, self.vault, "P@ss-唯一探针-7")
        self.assertEqual(res["total"], 0)
        self.assertGreaterEqual(res["scanned"]["skipped_sensitive"], 1)
        self.assertNotIn("P@ss-唯一探针-7", res["text"])
        self.assertNotIn("凭证管理.md", res["text"])


# ==================================================================== R16
class R16LayoutWriteDataRootTest(unittest.TestCase):

    def test_data_root_is_mandatory(self):
        """R16（评审 🟡-1）：`_layout_write` 缺 `data_root` 必须当场失败，不许悄悄少一道闸。"""
        tmp = Path(tempfile.mkdtemp(prefix="fish-r2-lw-"))
        try:
            home = tmp / "fish-home"
            with self.assertRaises(TypeError):
                fish._layout_write(home, "x.md", "x")   # 缺 data_root
            with self.assertRaises(RuntimeError):
                fish._layout_write(home, "x.md", "x", data_root=home)
            out = fish._layout_write(home, "x.md", "x", data_root=tmp / "data")
            self.assertEqual((home / out["path"]).read_text(encoding="utf-8"), "x\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ==================================================================== R17
class R17UnknownBoardScopeTest(_Round2Case):

    def test_unknown_scope_is_skipped_and_reported(self):
        """R17（评审 🟡-2）：未知 scope 不再打断 `migrate()` 全链，而是降级 + 跳过。"""
        self.conn.execute(
            "INSERT INTO boards (scope,scope_key,period_key,path,status,created_at) "
            "VALUES ('weird','K','261026','x/看板-261026.md','open',?)",
            (fish.now_utc(),))
        self.conn.commit()
        degraded = []
        changed = fish._ensure_boards_home_paths(self.conn, degraded=degraded)
        self.assertEqual(changed, [])
        self.assertEqual(len(degraded), 1, "未知 scope 必须进 degraded（看得见）")
        self.assertIn("weird", degraded[0]["reason"])
        row = self.conn.execute(
            "SELECT path FROM boards WHERE scope='weird'").fetchone()[0]
        self.assertEqual(row, "x/看板-261026.md", "该行必须原样不动")
        fish.migrate(self.conn)          # 全链不被脏数据打断


# ==================================================================== R18
class R18ReasonCodeTest(_Round2Case):

    def test_reject_rows_keep_reason_code(self):
        """R18（评审 🟡-3）：`meta.reason_code` 存错误码，`reason` 存截断中文。"""
        fish.kb_vault_set_mode(self.conn, "第二轮库", "ro")
        self.vault = fish.kb_vault_get(self.conn, "第二轮库")
        self.assertEqual(self.code(lambda: self.write(
            "10-项目/只读.md", "create", content="x\n")), "mode_ro")
        meta = self.last_meta()
        self.assertEqual(meta["reason_code"], "mode_ro")
        self.assertIn("mode=ro", meta["reason"])
        self.assertLessEqual(len(meta["reason"]), 160)


# ==================================================================== R19
class R19RootBoardRefreshTest(_Round2Case):

    def test_root_board_failure_is_visible_on_stderr(self):
        """R19（评审 🟡-4）：根板刷新失败不许被 `except: pass` 吞掉。"""
        board = fish.board_ensure(self.conn, "project", "P9")
        err = io.StringIO()
        with mock.patch.object(fish, "board_render_root",
                               side_effect=RuntimeError("根板炸了")), \
                contextlib.redirect_stderr(err):
            path, _size, _split = fish.board_render(self.conn, self.root, board,
                                                    home_root=self.home)
        self.assertTrue((self.home / path).exists(), "子板必须照写")
        self.assertIn("警告", err.getvalue())
        self.assertIn("根板炸了", err.getvalue())


# ==================================================================== R20
class R20RenderLayoutDegradedTest(_Round2Case):

    def _render(self):
        return fish.render_layout(self.conn, self.root,
                                  as_of=datetime.datetime.now().astimezone(),
                                  home_root=self.home)

    def test_sub_renderer_degraded_is_aggregated(self):
        """R20（评审 🟡-5）：子渲染器自己标 degraded → 汇总进 `degraded`。"""
        fake = {"path": "20-记忆库/索引-假.md", "bytes": 1, "sha256": "0" * 64,
                "degraded": True}
        with mock.patch.object(fish, "render_memory_index", return_value=fake):
            res = self._render()
        self.assertIn("20-记忆库/索引-假.md",
                      [it["source"] for it in res["degraded"]])

    def test_other_exception_degrades_source_instead_of_escaping(self):
        with mock.patch.object(fish, "render_memory_recent",
                               side_effect=ValueError("数据源坏了")):
            res = self._render()
        self.assertTrue([it for it in res["degraded"]
                         if it["source"] == "20-记忆库/最近.md"])
        self.assertEqual(res["failed"], [], "非 OSError 不该记成写盘失败")

    def test_sqlite_error_is_failed_not_escaped(self):
        with mock.patch.object(fish, "_render_vaults",
                               side_effect=sqlite3.Error("db gone")):
            res = self._render()
        self.assertEqual([it["path"] for it in res["failed"]],
                         ["30-知识库/vaults.md"])


# ==================================================================== R21
class _FrozenDatetime(datetime.datetime):
    """`datetime.datetime` 的子类：`now()` 钉在 2026-01-02T03:04:05Z（只用于 R21）。"""

    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)


_FIXED_DT_MODULE = types.SimpleNamespace(
    datetime=_FrozenDatetime, timezone=datetime.timezone,
    timedelta=datetime.timedelta, date=datetime.date)


class R21BackupCollisionTest(_Round2Case):

    def test_fifty_occupied_slots_get_a_fresh_directory(self):
        """R21（评审 ⚪-2）：同秒同路径候选位耗尽后**绝不复用**已存在目录。"""
        ts = "20260102T030405Z"
        base = self.root / "_backups" / "kb" / self.vault["vault_id"] / \
            f"{ts}-{fish._kb_backup_slug('10-项目/笔记.md')}"
        base.mkdir(parents=True)
        (base / "meta.json").write_text('{"ancient": true}', encoding="utf-8")
        for idx in range(2, 50):
            (base.with_name(f"{base.name}-{idx}")).mkdir()
        with mock.patch.object(fish_kb, "datetime", _FIXED_DT_MODULE):
            res = fish.kb_backup(self.root, self.vault, "10-项目/笔记.md",
                                 b"payload-bytes", {"action": "append"})
        out = Path(res["backup_path"])
        self.assertTrue(out.is_dir())
        # 新增（拆包 J-4）：**证明冻结时间真的生效**。
        # 原来 patch 打在 `fish.datetime` 上；搬迁后 `kb_backup` 读的是 `fish_kb.datetime`，
        # 于是时间冻结失效、目录名变成"现在"——而下面三条断言（不复用、不在候选名单、
        # payload 落盘）**依然全绿**，测试静默变成什么都没验证。这条断言把那种情况变红。
        self.assertTrue(
            out.name.startswith(f"{ts}-"),
            f"备份目录名的时间戳不是冻结的那一秒（{out.name}）："
            "说明 mock 没打到 kb_backup 真正读的模块")
        self.assertNotEqual(out, base, "不许复用已存在的备份目录")
        self.assertNotIn(out.name, [f"{base.name}-{i}" for i in range(2, 50)])
        self.assertEqual((out / "payload").read_bytes(), b"payload-bytes")
        self.assertTrue(json.loads((base / "meta.json").read_text("utf-8"))["ancient"],
                        "旧备份的 meta 必须原样保留")


# ==================================================================== N2
class N2BackupRetentionTest(_Round2Case):

    def test_doctor_reports_backup_size_and_prune_is_explicit(self):
        """N2（安全 F10）：doctor 给备份体积/最旧最新；`prune` 是显式命令、默认不自动删。"""
        res = self.write("10-项目/新.md", "create", content="# 新\n")
        backup_dir = Path(res["backup_path"])
        # 造一份"很旧"的备份：只改 meta.ts，不碰 payload
        old = backup_dir.with_name(backup_dir.name + "-old")
        shutil.copytree(backup_dir, old)
        meta = json.loads((old / "meta.json").read_text(encoding="utf-8"))
        meta["ts"] = "20200101T000000Z"
        (old / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                       encoding="utf-8")

        doc = fish.kb_doctor(self.conn, data_root=self.root)
        item = doc["vaults"][0]
        self.assertTrue(item["backups"]["available"])
        self.assertEqual(item["backups"]["dirs"], 2)
        self.assertGreater(item["backups"]["bytes"], 0)
        self.assertIn("备份：", doc["text"])
        self.assertIn("永不自动清理", doc["text"])

        with self.assertRaises(fish.KBError):
            fish.kb_backup_prune(self.conn, self.root, keep_days=0)

        dry = fish.kb_backup_prune(self.conn, self.root, keep_days=1, dry_run=True)
        self.assertEqual(dry["removed"], 1)
        self.assertTrue(old.is_dir(), "--dry-run 不许真删")

        real = fish.kb_backup_prune(self.conn, self.root, keep_days=1)
        self.assertEqual(real["removed"], 1)
        self.assertFalse(old.is_dir())
        self.assertTrue(backup_dir.is_dir(), "新备份必须留下")

    def test_prune_cli_is_registered_and_dry_run_works(self):
        self.write("10-项目/新2.md", "create", content="# 新2\n")
        old_argv = sys.argv
        sys.argv = ["fish.py", "--root", str(self.root), "kb", "backup", "prune",
                    "--keep-days", "1", "--dry-run"]
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
        self.assertIsNone(code, f"prune --dry-run 应 exit 0；stderr={err.getvalue()}")
        self.assertIn("保留 1 天", out.getvalue())


# ==================================================================== 第四轮（S1–S9）
#
# 第四轮定点修复的判据（2026-10-26）：只覆盖这一轮改的 9 项，不重复 R1–R21/ N2 的判据。
# 变异对应关系见 `mutation_b2b3_selfcheck.py` 的 M-S1/M-S2/M-S5/M-S6 与
# `docs/变异自证-B1B2B3-v0.1.md` §7。


def _short_path(path):
    """NTFS 8.3 短路径（`GetShortPathNameW`；不可用 / 未生成 → None）。

    为什么必须走系统 API：评审官第一版用 `dir /x` + GBK 解码读短名列，把中文短名解成
    乱码、于是误判"不含关键词"（见安全审计 R-5 的自我更正）。涉及编码的判据不得靠
    控制台输出猜。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(2048)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    except (AttributeError, OSError, ImportError):
        return None
    if not n or not buf.value:
        return None
    return buf.value


# ==================================================================== S1
class S1ResolvedPathTest(_Round2Case):
    """S1（安全 🟠 N1）：忽略/敏感判定必须在**解析后路径**上再跑一次。"""

    def test_junction_alias_to_ignored_dir_is_ignored_on_read_and_write(self):
        alias = self.vault_dir / "alias"
        if not _mk_junction(alias, self.vault_dir / ".obsidian"):
            self.skipTest("mklink /J 不可用（环境限制）：junction 别名判据未验证")
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, self.vault, "alias/app.json")),
            "ignored", "junction 别名读忽略目录必须 ignored")
        self.assertEqual(
            self.code(lambda: self.write("alias/坏插件.json", "create", content="{}")),
            "ignored", "junction 别名写忽略目录必须 ignored")
        self.assertFalse((self.vault_dir / ".obsidian" / "坏插件.json").exists(),
                         "被拒绝的别名写动了目标目录")
        # 反证：没有别名的同一个文件走原名仍然是 ignored（两种形态一个答案）
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, self.vault, ".obsidian/app.json")),
            "ignored")

    def test_short_name_alias_resolves_to_sensitive_file(self):
        """S1 + S5：8.3 短名把关键词截断（`CREDEN~1.MD`），解析后仍须命中敏感名单。"""
        target = self.vault_dir / "80-资料库" / "credentials-prod.md"
        target.write_text("# 凭证\n\nSECRET-PROBE-S1-9f2c\n", encoding="utf-8")
        short_file, short_root = _short_path(target), _short_path(self.vault_dir)
        if not short_file or not short_root:
            self.skipTest("该卷未生成 8.3 短名（环境限制）：短名别名判据未验证")
        try:
            short_rel = Path(short_file).relative_to(Path(short_root)).as_posix()
        except ValueError:
            self.skipTest("短名与长名不在同一根下（环境限制）")
        if short_rel.casefold() == "80-资料库/credentials-prod.md".casefold():
            self.skipTest("该文件没有生成 8.3 短名（环境限制）")
        if fish._kb_is_sensitive_name(short_rel):
            self.skipTest("该卷短名仍保留敏感关键词：这一形态不构成绕过")
        resolved = fish.kb_resolve_target(self.vault, short_rel)
        self.assertTrue(resolved["sensitive"], "解析后的敏感判定没有生效")
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, self.vault, short_rel)),
            "sensitive", "8.3 短名读敏感文件必须 sensitive")
        before = target.read_bytes()
        self.assertEqual(
            self.code(lambda: self.write(short_rel, "append", content="追加探针\n")),
            "sensitive", "8.3 短名写敏感文件必须 sensitive")
        self.assertEqual(target.read_bytes(), before, "被拒绝的别名写动了敏感文件字节")

    def test_normal_paths_are_not_over_rejected(self):
        """反证：解析后复判不能误伤正常路径（`resolved_rel == rel`）。"""
        resolved = fish.kb_resolve_target(self.vault, "00-首页/欢迎.md")
        self.assertEqual(resolved["resolved_rel"], "00-首页/欢迎.md")
        self.assertFalse(resolved["sensitive"])
        res = fish.kb_open(self.conn, self.vault, "00-首页/欢迎.md")
        self.assertIn("第一篇", res["text"])
        self.assertFalse(res["sensitive"])


# ==================================================================== S2
class S2MandatoryDenyTest(_Round2Case):
    """S2（评审 🟠 N1 / 安全 N5）：强制忽略项不依赖登记行，迁移回填幂等。"""

    def _downgrade_row(self):
        """把登记行改回 R5 之前的形态（只有两条 glob）。"""
        self.conn.execute(
            "UPDATE kb_vault SET ignore_globs=? WHERE vault_id=?",
            (json.dumps([".obsidian/**", ".trash/**"]), self.vault["vault_id"]))
        self.conn.commit()
        self.vault = fish.kb_vault_get(self.conn, "第二轮库")
        self.assertEqual(self.vault["ignore_globs"], [".obsidian/**", ".trash/**"],
                         "夹具没造出旧行形态，后面的断言不成立")

    def test_row_without_template_glob_still_blocks_template_writes(self):
        self._downgrade_row()
        self.assertEqual(
            self.code(lambda: self.write("90-模板/x.md", "create", content="# t\n")),
            "ignored", "只有两条 glob 的旧行也不能写 90-模板")
        self.assertFalse((self.vault_dir / "90-模板" / "x.md").exists())
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, self.vault, "90-模板/旧模板.md")),
            "ignored", "只有两条 glob 的旧行也不能读 90-模板")

    def test_migrate_backfills_missing_mandatory_globs_idempotently(self):
        self._downgrade_row()
        fish.migrate(self.conn)
        vault = fish.kb_vault_get(self.conn, "第二轮库")
        for glob in (".obsidian/**", ".trash/**", "90-模板/**"):
            self.assertIn(glob, vault["ignore_globs"], f"migrate 没回填 {glob}")
        snapshot = list(vault["ignore_globs"])
        fish.migrate(self.conn)           # 幂等：第二次不改行内规则
        self.assertEqual(fish.kb_vault_get(self.conn, "第二轮库")["ignore_globs"], snapshot)

    def test_defaults_and_schema_carry_three_globs(self):
        self.assertEqual(fish.KB_MANDATORY_DENY, (".obsidian/**", ".trash/**", "90-模板/**"))
        self.assertEqual(tuple(fish.KB_DEFAULT_IGNORE_GLOBS), fish.KB_MANDATORY_DENY)
        # SCHEMA 的列默认值同步（否则新库不靠 `vault add` 时又只有两条）
        self.assertIn("90-模板/**", fish._KB_VAULT_DDL)
        other = self.tmp / "vault2"
        other.mkdir()
        fresh = fish.kb_vault_add(self.conn, name="新登记库", path=str(other), mode="ro")
        for glob in fish.KB_MANDATORY_DENY:
            self.assertIn(glob, fresh["ignore_globs"], f"新登记的库缺 {glob}")


# ==================================================================== S3
class S3TreeMtimeTest(_Round2Case):
    """S3（评审 🟠 N3 / PRD KB-2）：每目录必须给"最近修改"（子树内 max mtime）。"""

    def setUp(self):
        super().setUp()
        self.dir = self.vault_dir / "40-归档"
        self.dir.mkdir()
        self.older = self.dir / "老.md"
        self.newer = self.dir / "新.md"
        self.older.write_text("# 老\n", encoding="utf-8")
        self.newer.write_text("# 新\n", encoding="utf-8")
        os.utime(self.older, (1_600_000_000, 1_600_000_000))
        os.utime(self.newer, (1_700_000_000, 1_700_000_000))
        (self.vault_dir / "50-空目录").mkdir()

    def test_dir_line_shows_newest_mtime_of_subtree(self):
        res = fish.kb_tree(self.conn, self.vault, rel="", depth=2,
                           max_chars=100000, max_tokens=100000)
        text = res["text"]
        newest = fish._kb_fmt_mtime(1_700_000_000)
        older = fish._kb_fmt_mtime(1_600_000_000)
        self.assertNotEqual(newest, older)
        lines = [ln for ln in text.splitlines()
                 if "40-归档/" in ln and "篇笔记" in ln]
        self.assertEqual(len(lines), 1, f"目录行没渲染出来：{text[:400]}")
        self.assertIn("最近修改=", lines[0])
        self.assertIn(newest, lines[0], "目录行必须显示子树内较新的那个 mtime")
        self.assertNotIn(older, lines[0], "目录行显示了较旧的那个 mtime")
        empty = [ln for ln in text.splitlines()
                 if "50-空目录/" in ln and "篇笔记" in ln]
        self.assertEqual(len(empty), 1)
        self.assertIn("最近修改=—", empty[0], "空目录必须显式给 `—` 而不是 1970 年")
        # JSON 读数与文本同源
        dirs = {d["rel"]: d for d in res["dirs"]}
        self.assertEqual(round(dirs["40-归档"]["mtime"]), 1_700_000_000)
        self.assertEqual(round(dirs["50-空目录"]["mtime"]), 0)
        self.assertIn("mtime", res["summary"], "summary 必须带 mtime")
        self.assertGreaterEqual(res["summary"]["mtime"], 1_700_000_000)

    def test_mtime_survives_depth_two(self):
        res = fish.kb_tree(self.conn, self.vault, rel="40-归档", depth=1,
                           max_chars=100000, max_tokens=100000)
        self.assertRegex(res["text"], r"最近修改=\d{4}-\d{2}-\d{2}T")


# ==================================================================== S5
class S5SensitivePathTest(_Round2Case):
    """S5（安全 🟡 N2）：敏感名单按**路径分量**判；`密码` 不误伤 `密码学`。"""

    def test_directory_name_participates(self):
        target = self.vault_dir / "凭证目录"
        target.mkdir()
        note = target / "账号清单.md"
        note.write_text("# 清单\n\n账号格式说明。\n", encoding="utf-8")
        self.assertTrue(fish._kb_is_sensitive_name("凭证目录/账号清单.md"),
                        "目录名命中敏感名单也必须 sensitive")
        self.assertEqual(
            self.code(lambda: fish.kb_open(self.conn, self.vault, "凭证目录/账号清单.md")),
            "sensitive")
        # search 也不许把它做成片段（同一个判定守两处）
        res = fish.kb_search(self.conn, self.vault, "账号格式说明")
        self.assertEqual(res["total"], 0)
        self.assertGreaterEqual(res["scanned"]["skipped_sensitive"], 1)

    def test_password_cryptography_is_not_sensitive(self):
        target = self.vault_dir / "20-学习"
        target.mkdir()
        note = target / "密码学笔记.md"
        note.write_text("# 密码学\n\n哈希与对称加密。\n", encoding="utf-8")
        self.assertFalse(fish._kb_is_sensitive_name("20-学习/密码学笔记.md"),
                         "`密码学笔记.md` 是正常笔记，不该被敏感名单误伤")
        res = fish.kb_open(self.conn, self.vault, "20-学习/密码学笔记.md")
        self.assertIn("哈希", res["text"])
        self.assertFalse(res["sensitive"])
        # 真凭证形态仍然命中
        for name in ("密码本.md", "密码.md", "account/密码-轮换记录.md",
                     "凭证.md", "my-secret.json", ".env.local", "id_rsa",
                     "x.pem", "y.key", "credentials-prod.md"):
            with self.subTest(name=name):
                self.assertTrue(fish._kb_is_sensitive_name(name), f"{name} 未被识别")


# ==================================================================== S6
class S6SensitiveWriteTest(_Round2Case):
    """S6（安全 🟡 N3）：写三档命中敏感 → `sensitive`（除非显式授权）。"""

    def test_append_to_credential_file_is_rejected_and_bytes_unchanged(self):
        target = self.vault_dir / "80-资料库" / "凭证管理.md"
        before = target.read_bytes()
        self.assertEqual(
            self.code(lambda: self.write("80-资料库/凭证管理.md", "append",
                                         content="追加探针\n")),
            "sensitive", "append 到凭证文件必须被拒")
        self.assertEqual(target.read_bytes(), before, "被拒绝的 append 动了文件字节")
        base = fish.kb_sha256_bytes(before)
        self.assertEqual(
            self.code(lambda: self.write("80-资料库/凭证管理.md", "update",
                                         content="替换探针\n", base=base)),
            "sensitive", "update 到凭证文件必须被拒")
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(
            self.code(lambda: self.write("80-资料库/.env", "create",
                                         content="PROBE=1\n")),
            "sensitive", "create 敏感文件名必须被拒")
        self.assertFalse((self.vault_dir / "80-资料库" / ".env").exists())

    def test_explicit_authorization_still_writes(self):
        """反证：开关是真的（否则"拒绝"只是把功能关掉）。"""
        target = self.vault_dir / "80-资料库" / "凭证管理.md"
        res = fish.kb_write(self.conn, self.root, self.vault, "80-资料库/凭证管理.md",
                            action="append", content="授权追加\n", allow_sensitive=True)
        self.assertTrue(res["verified"])
        self.assertIn("授权追加", target.read_text(encoding="utf-8"))


# ==================================================================== S7
class S7BomFrontmatterTest(_Round2Case):
    """S7（安全/评审 🟡 N4/N7）：BOM + frontmatter 的 update 不许丢头。"""

    def test_update_keeps_frontmatter_keys_and_bom(self):
        target = self.vault_dir / "10-项目" / "带BOM.md"
        original = ("\ufeff---\naliases: [a, b]\ntags: old\nstatus: draft\n---\n\n正文原样\n")
        target.write_bytes(original.encode("utf-8"))
        before = target.read_bytes()
        self.assertTrue(before.startswith(b"\xef\xbb\xbf"))
        res = fish.kb_write(self.conn, self.root, self.vault, "10-项目/带BOM.md",
                            action="update", content="新正文\n",
                            base_sha256=fish.kb_sha256_bytes(before),
                            frontmatter={"tags": "new"})
        self.assertTrue(res["verified"])
        after = target.read_bytes()
        self.assertTrue(after.startswith(b"\xef\xbb\xbf"), "update 把 BOM 弄丢了")
        text = after.decode("utf-8-sig")
        self.assertIn("新正文", text)
        self.assertNotIn("正文原样", text)
        self.assertIn("aliases: [a, b]", text, "update 丢掉了未知键 aliases（会断双链）")
        self.assertIn("status: draft", text)
        self.assertIn("tags: new", text)
        self.assertNotIn("tags: old", text)
        keys = {ln.split(":", 1)[0].strip()
                for ln in text.split("---")[1].splitlines() if ":" in ln}
        self.assertEqual(keys, {"aliases", "tags", "status"},
                         "frontmatter 键集合发生了变化（除被更新的键）")


# ==================================================================== S9
class S9MiscFixesTest(_Round2Case):
    """S9（评审 🟡 N4/N5/N6/N8）：四个小项各一条断言。"""

    def test_budget_args_must_be_integers(self):
        calls = (
            lambda: fish.kb_tree(self.conn, self.vault, rel="", max_chars="x"),
            lambda: fish.kb_tree(self.conn, self.vault, rel="", max_tokens="y"),
            lambda: fish.kb_search(self.conn, self.vault, "普通正文", max_chars="x"),
            lambda: fish.kb_search(self.conn, self.vault, "普通正文", max_tokens="z"),
        )
        for call in calls:
            with self.subTest(call=call):
                self.assertEqual(self.code(call), "bad_usage",
                                 "非整数预算必须给域错误，不许裸 ValueError")
        self.assertEqual(self.last_meta()["reason_code"], "bad_usage")

    def test_write_id_suffix_is_case_insensitive(self):
        # 2026-10-27：write_id 后缀是 8 位十六进制，**纯数字概率 ≈2.3%**——抽到纯数字时
        # `upper == write_id`，本判据会假红（实测 worktree 基线首跑就抽到一次）。
        # 修法：重抽到含字母的样本；判据本身不放宽（仍要求大写形态可 rollback）。
        name = None
        for i in range(10):
            name = f"10-项目/大小写-{i}.md"
            res = self.write(name, "create", content="# x\n")
            if str(res["write_id"]).upper() != str(res["write_id"]):
                break
        else:
            self.fail("10 次都没抽到含字母的 write_id——生成器或断言都坏了")
        upper = str(res["write_id"]).upper()
        self.assertNotEqual(upper, res["write_id"])
        back = fish.kb_rollback(self.conn, self.root, upper)
        self.assertTrue(back["removed"], "大写形态的合法 write_id 被误杀")
        self.assertFalse((self.vault_dir / name).exists())
        # 大小写不敏感 ≠ 内容不校验：错后缀仍然 not_found 且文件仍在
        res2 = self.write("10-项目/大小写2.md", "create", content="# y\n")
        access_id = str(res2["write_id"]).split("-", 1)[0]
        self.assertEqual(
            self.code(lambda: fish.kb_rollback(self.conn, self.root,
                                               f"{access_id}-DEADBEEF")), "not_found")
        self.assertTrue((self.vault_dir / "10-项目" / "大小写2.md").exists())

    def test_backup_finalize_failure_is_not_silent(self):
        backup_dir = Path(self.write("10-项目/备注.md", "create",
                                     content="# z\n")["backup_path"])
        meta_path = backup_dir / "meta.json"
        meta_path.unlink()
        meta_path.mkdir()                 # meta.json 变成目录 → write_text 抛 OSError
        self.assertFalse(fish._kb_backup_finalize(backup_dir, write_id="1-deadbeef"),
                         "finalize 失败必须返回 False（不许静默）")
        with mock.patch.object(fish_kb, "_kb_backup_finalize", return_value=False):
            res = self.write("10-项目/备注2.md", "create", content="# w\n")
        self.assertTrue(res["warnings"], "备份元数据写失败必须进回执 warning")
        self.assertIn("meta.json", res["warnings"][0])
        self.assertIn("手工恢复", res["undo"], "undo 不许再假装'可撤销'")

    def test_board_sweep_reports_root_refresh_failure(self):
        fish.board_ensure(self.conn, "project", "P9", period_key="250101")
        err = io.StringIO()
        with mock.patch.object(fish, "board_render_root",
                               side_effect=RuntimeError("根板炸了")), \
                contextlib.redirect_stderr(err):
            res = fish.board_sweep(self.conn, root=self.root, days=1)
        self.assertEqual(res["swept"], 1, "归档本身照做")
        self.assertIn("警告", err.getvalue(), "根板刷新失败被吞掉了")
        self.assertIn("根板炸了", err.getvalue())


# ==================================================================== N7 / N9
class N7StrictBoolTest(_Round2Case):
    """N7（安全 🟡）：`allow_sensitive` 的**严格布尔**解析——字符串 `"false"` 不是同意。

    真值必须独立于被测实现：这里逐条钉"哪些写法是同意的、哪些是拒绝的、哪些是**说不清**"，
    而不是拿 `fish._strict_bool` 自己算一遍再比（那样永远等于它自己）。

    授权旗的三类答案（安全审计 N7 的 fail-open 就是第 1、2 类被读成了"同意"）：

      · 明确真：`True` / `1` / `"true"` / `"TRUE"` / `" true "`；
      · 明确假：`False` / `0` / `"false"` / `"FALSE"` / `"0"`；
      · 说不清：`"yes"` / `"on"` / `"真"` / `""` / `2` / 浮点 / 列表 → `bad_usage`（不猜）。

    N9（安全 ⚪）：审计只在**真授权**时记 `allow_sensitive=true`，读/写两面一致——
    普通读写不长这个字段，也不许记别的隐私（正文、理由原文以外的授权细节）。
    """

    REL = "80-资料库/凭证管理.md"
    PROBE = "P@ss-唯一探针-7"

    def test_read_face_false_spellings_do_not_authorize(self):
        """`bool("false") is True` 就是这个 bug 的形态；四种假写法都必须仍 `sensitive`。"""
        for value in ("false", "FALSE", " false ", "0", 0, False):
            with self.subTest(value=value):
                err = None
                try:
                    fish.kb_open(self.conn, self.vault, self.REL, allow_sensitive=value)
                except fish.KBError as exc:
                    err = exc
                self.assertIsNotNone(
                    err, f"allow_sensitive={value!r} 竟然放行了敏感正文（fail-open）")
                self.assertEqual(err.code, "sensitive",
                                 f"value={value!r} 的码不对：{err.code}")
                self.assertNotIn(self.PROBE, err.message, "拒读说明里不许带正文")

    def test_read_face_true_spellings_authorize(self):
        """反证：授权开关是真的（否则"拒绝"只是把功能关掉，不是判据）。"""
        for value in ("true", "TRUE", " true ", "1", 1, True):
            with self.subTest(value=value):
                res = fish.kb_open(self.conn, self.vault, self.REL, allow_sensitive=value)
                self.assertIn(self.PROBE, res["text"], f"value={value!r} 没读到正文")
                self.assertTrue(res["sensitive"])
                self.assertTrue(any("显式授权" in s for s in res["statements"]))

    def test_ambiguous_spellings_are_bad_usage(self):
        """说不清就报错，不许"非空字符串即同意"——包括空串与数值越界。"""
        for value in ("yes", "on", "真", "", 2, -1, 1.0, ["true"]):
            with self.subTest(value=value):
                self.assertEqual(
                    self.code(lambda v=value: fish.kb_open(
                        self.conn, self.vault, self.REL, allow_sensitive=v)),
                    "bad_usage", f"allow_sensitive={value!r} 必须 bad_usage（不许猜）")
        self.assertEqual(self.last_meta()["reason_code"], "bad_usage",
                         "拒绝原因要能按错误码统计（R18 口径）")

    def test_write_face_shares_the_same_parser(self):
        """写面不许另立规则：`"false"` 拒、`"yes"` → bad_usage、`"true"` 才写。"""
        target = self.vault_dir / "80-资料库" / "凭证管理.md"
        before = target.read_bytes()
        self.assertEqual(
            self.code(lambda: fish.kb_write(
                self.conn, self.root, self.vault, self.REL, action="append",
                content="追加探针\n", allow_sensitive="false")),
            "sensitive", "写面把字符串 \"false\" 读成了同意")
        self.assertEqual(target.read_bytes(), before, "被拒绝的 append 动了文件字节")
        self.assertEqual(
            self.code(lambda: fish.kb_write(
                self.conn, self.root, self.vault, self.REL, action="append",
                content="追加探针\n", allow_sensitive="yes")),
            "bad_usage", "写面的模糊写法必须 bad_usage")
        self.assertEqual(target.read_bytes(), before, "bad_usage 也动了文件字节")
        ok = fish.kb_write(self.conn, self.root, self.vault, self.REL, action="append",
                           content="授权追加\n", allow_sensitive="true")
        self.assertTrue(ok["verified"])
        self.assertIn("授权追加", target.read_text(encoding="utf-8"))

    def test_audit_records_allow_sensitive_only_when_true(self):
        """N9：读/写两面都只在真授权时记 `true`；普通读写不长这个字段。"""
        fish.kb_open(self.conn, self.vault, "10-项目/笔记.md")
        self.assertNotIn("allow_sensitive", self.last_meta("open"),
                         "未授权的读不该长出 allow_sensitive 字段")
        fish.kb_open(self.conn, self.vault, self.REL, allow_sensitive=True)
        self.assertIs(self.last_meta("open")["allow_sensitive"], True,
                      "真授权必须留痕")

        res = self.write("10-项目/普通新建.md", "create", content="# 普通\n")
        self.assertNotIn("allow_sensitive", self.last_meta(res["action"]),
                         "普通写不该长出 allow_sensitive 字段")
        fish.kb_write(self.conn, self.root, self.vault, self.REL, action="append",
                      content="授权追加\n", allow_sensitive=True)
        meta = self.last_meta("append")
        self.assertIs(meta["allow_sensitive"], True, "授权写必须留痕（写面曾缺这个字段）")
        self.assertEqual(
            set(meta) - {"reason", "eol", "same_name_count", "undo_action",
                         "allow_sensitive"},
            set(), f"授权写的审计多记了别的东西：{sorted(meta)}")

    def test_missing_flag_is_denied_not_granted(self):
        """`None`（不带键）是**缺省即拒绝**——不是"未给就是同意"，也不是 bad_usage。"""
        err = None
        try:
            fish.kb_open(self.conn, self.vault, self.REL, allow_sensitive=None)
        except fish.KBError as exc:
            err = exc
        self.assertIsNotNone(err, "allow_sensitive=None 竟然放行了敏感正文")
        self.assertEqual(err.code, "sensitive")


if __name__ == "__main__":
    unittest.main(verbosity=2)