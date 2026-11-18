#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 · 知识库写路径（create / append / update / rollback）判据（T04）。

被测定义（KB-13 / KB-15 + 裁决 C4 + §四.4）：
  三档写必须**显式选一**；update 必须带 `base_sha256`（乐观锁）；
  写前备份**只备份被写的那一个文件**；原子写；**写后重读校验 sha256**；
  frontmatter 只增改不重建（未知键保留）；`[[双链]]` 不改写；禁删/禁移/禁改名
  （唯一例外：`create` 的回滚 = 删掉自己刚建且此后无人改的文件）。

## 变异自证（X-1）

变异 A —— 对应 `test_update_requires_base_sha256`：
  把 `base_sha256` 校验删掉 → 不带锁也能覆盖 → 断言红（"覆盖别人的改动"是这批最贵的错）。

变异 B —— 对应 `test_every_write_has_backup_before_change`：
  把 `kb_backup(...)` 移到 `kb_atomic_write` 之后（或删掉）→ 备份目录里是**新**内容
  （或没有备份）→ 断言"备份 payload 等于写入前字节"红。

变异 C —— 对应 `test_frontmatter_keeps_unknown_keys`：
  把 `_kb_merge_frontmatter` 换成"用新键重建整个 head" → 未知键 `aliases` 与注释消失 → 红。

纪律（K12）：vault 在 `tempfile.mkdtemp()` 里造；**绝不写真实 vault**。
"""
import contextlib
import importlib.util
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

# 拆包（2026-10-27 · kb 拆包 R1）：kb 的实现整段搬进了 `fish_kb`。
# `mock.patch.object(fish, "kb_sha256_file")` 从此打不到调用点（写后校验在 `fish_kb`
# 的 globals 里解析这个名字），所以这一处 patch 目标改指 `fish_kb`；断言一个都没动。
import fish_kb  # noqa: E402 —— 与 fish.py 同目录，测试用 cwd 导入

WRITE_ID_RE = re.compile(r"^\d+-[0-9a-f]{8}$")


class KBWriteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbw-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "20-PPT").mkdir(parents=True)
        (self.vault_dir / "00-首页").mkdir()
        (self.vault_dir / "00-首页" / "保留.md").write_text(
            "# 保留\n\n这段内容不许被任何一次写碰到。\n", encoding="utf-8")
        self.vault = fish.kb_vault_add(self.conn, name="写测试库",
                                       path=str(self.vault_dir), mode="rw")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 工具
    def _write(self, **kw):
        kw.setdefault("rel", "20-PPT/新笔记.md")
        kw.setdefault("action", "create")
        kw.setdefault("content", "# 新笔记\n\n正文。\n")
        return fish.kb_write(self.conn, self.root, self.vault, **kw)

    def _backups(self):
        base = self.root / "_backups" / "kb" / self.vault["vault_id"]
        return sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []

    def _sha(self, rel):
        return fish.kb_sha256_file(self.vault_dir / rel)

    def _run_main(self, argv, content_file=None):
        """跑一次 `fish.main()`；`content_file` 仅为旧调用形态保留（不参与执行）。"""
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

    # ============================================================ create
    def test_create_writes_file_with_receipt(self):
        res = self._write()
        target = self.vault_dir / "20-PPT" / "新笔记.md"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "# 新笔记\n\n正文。\n")
        self.assertEqual(res["action"], "create")
        self.assertEqual(res["rel"], "20-PPT/新笔记.md")
        self.assertIsNone(res["bytes_before"])
        self.assertEqual(res["bytes_after"], target.stat().st_size)
        self.assertEqual(res["sha256_after"], fish.kb_sha256_file(target))
        self.assertTrue(res["verified"], "写后必须重读校验")
        self.assertEqual(res["eol"], "lf")
        self.assertTrue(WRITE_ID_RE.match(res["write_id"]), res["write_id"])
        self.assertIn(f"fish kb rollback {res['write_id']}", res["undo"])
        self.assertEqual(res["same_name_warnings"], [])

    def test_create_refuses_existing_file(self):
        """变异 C（设计里的）：create 改成直接覆盖 → 本测试红。"""
        self._write()
        with self.assertRaises(fish.KBError) as ctx:
            self._write()
        self.assertEqual(ctx.exception.code, "exists")

    def test_create_with_frontmatter_and_wikilinks(self):
        res = self._write(rel="20-PPT/带头部.md",
                          content="见 [[另一篇笔记]] 与 [[目标|别名]]。\n",
                          frontmatter={"tags": "认知", "status": "draft"})
        text = (self.vault_dir / "20-PPT" / "带头部.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "frontmatter 必须写在文件头")
        self.assertIn("tags: 认知", text)
        self.assertIn("status: draft", text)
        self.assertIn("[[另一篇笔记]]", text, "[[双链]] 不许被转义或改写")
        self.assertIn("[[目标|别名]]", text)
        self.assertTrue(text.endswith("。\n"))
        self.assertEqual(res["action"], "create")

    def test_create_reports_same_name_warnings(self):
        (self.vault_dir / "40-教程").mkdir()
        (self.vault_dir / "40-教程" / "示例.md").write_text("旧的\n", encoding="utf-8")
        res = self._write(rel="20-PPT/示例.md")
        self.assertEqual([w["rel"] for w in res["same_name_warnings"]],
                         ["40-教程/示例.md"], "同名笔记必须提示（但不阻断）")
        self.assertTrue((self.vault_dir / "20-PPT" / "示例.md").exists())
        self.assertEqual((self.vault_dir / "40-教程" / "示例.md").read_text(
            encoding="utf-8"), "旧的\n", "同名文件不许被覆盖")

    def test_content_starting_with_frontmatter_is_rejected(self):
        with self.assertRaises(fish.KBError) as ctx:
            self._write(content="---\ntags: x\n---\n\n正文\n")
        self.assertEqual(ctx.exception.code, "bad_frontmatter")

    # ============================================================ W0 · frontmatter 列表支持
    #
    # 2026-11-18：契约变更（用户授权技术决策，选方案 B）。
    # 旧契约是"frontmatter 只接受标量，列表一律拒绝"——那条规则在实测面前站不住：
    # 安逸知识库 318 篇里 **287 篇（90%）** 用 YAML 列表写 tags，拒绝它们等于
    # "安全地什么也做不了"，而更糟的是旧实现**不拒绝而是写坏**（只换首行，留下孤儿列表项）。

    def test_nested_frontmatter_is_still_rejected(self):
        """嵌套结构仍然拒绝——支持列表不等于支持任意 YAML。

        不引入 PyYAML 的代价是"只覆盖两种形态"，这个边界必须被钉住，
        否则以后有人以为"既然支持列表了，那字典也行"。
        """
        for bad in ({"a": 1}, [["x"], ["y"]]):
            with self.assertRaises(fish.KBError) as ctx:
                self._write(frontmatter={"tags": bad})
            self.assertEqual(ctx.exception.code, "bad_frontmatter", f"{bad!r} 应被拒绝")

    def _fm_of(self, rel):
        """读回落盘文件的 frontmatter 段（含 --- 定界），用真 YAML 解析验证合法性。"""
        import yaml
        text = (self.vault_dir / rel).read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"), f"文件不以 frontmatter 开头：{text[:40]!r}")
        parts = text.split("---", 2)
        self.assertGreaterEqual(len(parts), 3, "frontmatter 未闭合")
        return yaml.safe_load(parts[1])

    def test_frontmatter_list_roundtrip_is_parseable(self):
        """写入列表后，**真 YAML 解析器**必须能解析，且值仍是 list。

        为什么必须用真解析器而不是正则：旧实现"看着像对的"——
        `tags: 新标签` 单独看完全正常，错的是它后面的孤儿项。正则抓不到这种错。
        """
        self._write(frontmatter={"tags": ["入口", "自我"]})
        fm = self._fm_of("20-PPT/新笔记.md")
        self.assertEqual(fm.get("tags"), ["入口", "自我"],
                         f"tags 不是列表（可能被写成字符串）：{fm.get('tags')!r}")

    def test_frontmatter_list_to_scalar_leaves_no_orphan(self):
        """**这条是 2026-11-18 那个缺陷的直接探针。**

        旧实现：对 `tags:` 列表写标量 → 只替换 `tags:` 那一行，后面 `- 入口`
        变成孤儿，YAML 解析报错。实测影响 287/318 篇（90%）。

        现在：块感知替换，连列表项一起换掉。
        """
        import yaml
        target = self.vault_dir / "20-PPT" / "新笔记.md"
        target.write_text("---\ntags:\n  - 入口\n  - 自我\n---\n\n正文\n", encoding="utf-8")
        head = ["---", "tags:", "  - 入口", "  - 自我", "---"]
        out = fish._kb_merge_frontmatter(head, {"tags": "单一"})
        text = "\n".join(out)
        parts = text.split("---", 2)
        fm = yaml.safe_load(parts[1])   # 有孤儿项时这里会抛 YAMLError
        self.assertEqual(fm.get("tags"), "单一",
                         f"标量替换后 tags 应为 '单一'，实际 {fm.get('tags')!r}")
        self.assertNotIn("- 入口", text, "旧列表项没被清掉（孤儿）")

    def test_frontmatter_unknown_keys_untouched(self):
        """R7 边界不变：未知键、注释、空行一字不动。"""
        head = ["---", "# 注释", "tags:", "  - 入口", "cssclasses:", "  - wide",
                "", "custom: 保留我", "---"]
        out = fish._kb_merge_frontmatter(head, {"tags": ["新"]})
        self.assertIn("# 注释", out)
        self.assertIn("", out)
        self.assertIn("custom: 保留我", out)
        self.assertIn("  - wide", out)
        self.assertIn("  - 新", out)
        self.assertNotIn("  - 入口", out)

    def test_content_too_large_is_rejected(self):
        big = "字" * (fish.KB_MAX_CONTENT_BYTES // 3 + 10)
        with self.assertRaises(fish.KBError) as ctx:
            self._write(content=big)
        self.assertEqual(ctx.exception.code, "content_too_large")
        self.assertFalse((self.vault_dir / "20-PPT" / "新笔记.md").exists())

    # ============================================================ append
    def test_append_keeps_original_as_prefix(self):
        """验收 ⑥：append 后**原字节是新字节的前缀**。"""
        path = self.vault_dir / "20-PPT" / "追加.md"
        path.write_bytes("# 原有\n\n第一段。\n".encode("utf-8"))
        before = path.read_bytes()
        res = self._write(rel="20-PPT/追加.md", action="append",
                          content="第二段。\n")
        after = path.read_bytes()
        self.assertTrue(after.startswith(before), "append 把原有内容改动了")
        self.assertIn("第二段".encode("utf-8"), after)
        self.assertEqual(res["bytes_before"], len(before))
        self.assertEqual(res["sha256_before"], fish.kb_sha256_bytes(before))

    def test_append_preserves_crlf(self):
        path = self.vault_dir / "20-PPT" / "crlf.md"
        path.write_bytes("第一行\r\n第二行\r\n".encode("utf-8"))
        self._write(rel="20-PPT/crlf.md", action="append", content="第三行\n")
        raw = path.read_bytes()
        self.assertEqual(fish.kb_detect_eol(raw), "crlf", "CRLF 文件被改成 LF 会造成假 diff")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_append_missing_file_is_not_found(self):
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="20-PPT/没有.md", action="append", content="x\n")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_append_rejects_frontmatter_arg(self):
        path = self.vault_dir / "20-PPT" / "a.md"
        path.write_text("x\n", encoding="utf-8")
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="20-PPT/a.md", action="append", content="y\n",
                        frontmatter={"tags": "z"})
        self.assertEqual(ctx.exception.code, "bad_usage")

    # ============================================================ update
    def test_update_requires_base_sha256(self):
        """**变异 A**：没有乐观锁就覆盖 → 本测试红。"""
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_text("旧内容\n", encoding="utf-8")
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="20-PPT/改.md", action="update", content="新内容\n")
        self.assertEqual(ctx.exception.code, "stale_base")
        self.assertEqual(path.read_text(encoding="utf-8"), "旧内容\n")

    def test_update_rejects_stale_base(self):
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_text("旧内容\n", encoding="utf-8")
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="20-PPT/改.md", action="update", content="新内容\n",
                        base_sha256="0" * 64)
        self.assertEqual(ctx.exception.code, "stale_base")

    def test_update_replaces_body_and_keeps_unknown_frontmatter(self):
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_text("---\ntags: 旧\n# 人写的注释\naliases:\n  - 别名甲\n"
                        "custom: keep-me\n---\n\n旧正文\n", encoding="utf-8")
        sha = fish.kb_sha256_file(path)
        before_bytes = path.read_bytes()   # 字符串长度会差：write_text 按平台换行落盘
        res = self._write(rel="20-PPT/改.md", action="update",
                          content="新正文一\n新正文二\n",
                          frontmatter={"tags": "新"}, base_sha256=sha)
        text = path.read_text(encoding="utf-8")
        self.assertIn("tags: 新", text)
        self.assertNotIn("tags: 旧", text)
        self.assertIn("# 人写的注释", text, "注释必须保留")
        self.assertIn("aliases:", text, "未知键必须保留（含它的缩进子行）")
        self.assertIn("- 别名甲", text)
        self.assertIn("custom: keep-me", text, "未知标量键必须原样保留")
        self.assertNotIn("旧正文", text, "update 是显式替换正文")
        self.assertIn("新正文一", text)
        self.assertEqual(res["bytes_before"], len(before_bytes))
        self.assertEqual(res["sha256_before"], sha)
        self.assertTrue(res["verified"])

    def test_update_rejects_broken_existing_frontmatter(self):
        path = self.vault_dir / "20-PPT" / "坏头.md"
        path.write_text("---\ntags: x\n\n正文（没有收尾 ---）\n", encoding="utf-8")
        sha = fish.kb_sha256_file(path)
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="20-PPT/坏头.md", action="update", content="新\n",
                        base_sha256=sha)
        self.assertEqual(ctx.exception.code, "bad_frontmatter")

    # ============================================================ 安全 / 备份
    def test_ro_vault_refuses_write(self):
        fish.kb_vault_set_mode(self.conn, "写测试库", "ro")
        self.vault = fish.kb_vault_get(self.conn, "写测试库")
        with self.assertRaises(fish.KBError) as ctx:
            self._write()
        self.assertEqual(ctx.exception.code, "mode_ro")
        self.assertFalse((self.vault_dir / "20-PPT" / "新笔记.md").exists())

    def test_every_write_has_backup_before_change(self):
        """**变异 B**：备份必须在写之前，且 payload 是**写入前的字节**。"""
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_bytes("旧字节\n".encode("utf-8"))
        before = path.read_bytes()
        res = self._write(rel="20-PPT/改.md", action="update", content="新字节\n",
                          base_sha256=fish.kb_sha256_bytes(before))
        backup = Path(res["backup_path"])
        self.assertTrue(backup.is_dir())
        payload = (backup / "payload").read_bytes()
        self.assertEqual(payload, before, "备份 payload 不是写入前的字节")
        meta = json.loads((backup / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["action"], "update")
        self.assertEqual(meta["sha256_before"], fish.kb_sha256_bytes(before))
        self.assertEqual(meta["write_id"], res["write_id"])

    def test_create_backup_has_meta_without_payload(self):
        res = self._write()
        backup = Path(res["backup_path"])
        self.assertTrue((backup / "meta.json").exists())
        self.assertFalse((backup / "payload").exists(),
                         "create 没有旧内容可备份，不该造空 payload")

    def test_backup_covers_only_the_written_file(self):
        """裁决 §四.4：只备份被写的那一个文件（整库 10.96 GB，绝不整库复制）。"""
        keep_sha = self._sha("00-首页/保留.md")
        res = self._write()
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(sorted(p.name for p in backups[0].iterdir()),
                         ["meta.json"])
        self.assertEqual(self._sha("00-首页/保留.md"), keep_sha, "别的文件被碰了")

    def test_write_verification_catches_truncation(self):
        """KB-15：写后重读校验不是装饰——校验值不对必须报错。"""
        with mock.patch.object(fish_kb, "kb_sha256_file", return_value="0" * 64):
            with self.assertRaises(fish.KBError) as ctx:
                self._write()
        self.assertEqual(ctx.exception.code, "not_writable")

    def test_write_path_traversal_and_ignored_are_rejected(self):
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel="../逃逸.md")
        self.assertEqual(ctx.exception.code, "path_invalid")
        (self.vault_dir / ".obsidian").mkdir(exist_ok=True)
        with self.assertRaises(fish.KBError) as ctx:
            self._write(rel=".obsidian/插件设置.md")
        self.assertEqual(ctx.exception.code, "ignored")

    # ============================================================ rollback
    def test_rollback_update_restores_original_bytes(self):
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_bytes("原始内容\n".encode("utf-8"))
        before = path.read_bytes()
        res = self._write(rel="20-PPT/改.md", action="update", content="改后\n",
                          base_sha256=fish.kb_sha256_bytes(before))
        back = fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertTrue(back["restored"])
        self.assertFalse(back["removed"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(back["sha256"], fish.kb_sha256_bytes(before))

    def test_rollback_create_removes_the_new_file(self):
        res = self._write()
        target = self.vault_dir / "20-PPT" / "新笔记.md"
        self.assertTrue(target.exists())
        back = fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertTrue(back["removed"])
        self.assertFalse(target.exists(), "create 的回滚就是删掉自己刚建的文件")

    def test_rollback_append_restores(self):
        path = self.vault_dir / "20-PPT" / "追加.md"
        path.write_bytes("原有\n".encode("utf-8"))
        before = path.read_bytes()
        res = self._write(rel="20-PPT/追加.md", action="append", content="追加段\n")
        fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertEqual(path.read_bytes(), before)

    def test_rollback_refuses_when_target_changed_then_force(self):
        path = self.vault_dir / "20-PPT" / "改.md"
        path.write_bytes("原始\n".encode("utf-8"))
        res = self._write(rel="20-PPT/改.md", action="update", content="改后\n",
                          base_sha256=fish.kb_sha256_bytes("原始\n".encode("utf-8")))
        path.write_bytes("别人又改了\n".encode("utf-8"))
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertEqual(ctx.exception.code, "stale_base")
        back = fish.kb_rollback(self.conn, self.root, res["write_id"], force=True)
        self.assertTrue(back["restored"])
        self.assertTrue(back["forced_backup"], "--force 必须先把当前内容另存")
        self.assertTrue(Path(back["forced_backup"]).is_dir())
        self.assertEqual((Path(back["forced_backup"]) / "payload").read_bytes(),
                         "别人又改了\n".encode("utf-8"))
        self.assertEqual(path.read_bytes(), "原始\n".encode("utf-8"))

    def test_rollback_missing_backup_fails_loudly(self):
        res = self._write()
        shutil.rmtree(res["backup_path"])
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertEqual(ctx.exception.code, "backup_missing")
        self.assertIn("备份", str(ctx.exception))

    def test_rollback_bad_write_id(self):
        for bad in ("abc", "17", "x-12345678", ""):
            with self.subTest(write_id=bad):
                with self.assertRaises(fish.KBError) as ctx:
                    fish.kb_rollback(self.conn, self.root, bad)
                self.assertEqual(ctx.exception.code, "bad_usage")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_rollback(self.conn, self.root, "999999-deadbeef")
        self.assertEqual(ctx.exception.code, "not_found")

    def test_rollback_after_vault_removed(self):
        res = self._write()
        fish.kb_vault_remove(self.conn, "写测试库")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_rollback(self.conn, self.root, res["write_id"])
        self.assertEqual(ctx.exception.code, "vault_missing")

    def test_rollback_is_logged(self):
        res = self._write()
        fish.kb_rollback(self.conn, self.root, res["write_id"], citizen_id="CIT7")
        row = self.conn.execute(
            "SELECT citizen_id, action, result, meta FROM kb_access "
            "WHERE action='rollback'").fetchone()
        self.assertEqual(row[0], "CIT7")
        self.assertEqual(row[2], "ok")
        self.assertEqual(json.loads(row[3])["write_id"], res["write_id"])

    # ============================================================ CLI
    def test_cli_create_prints_three_sections(self):
        content = self.tmp / "content.md"
        content.write_text("# 命令行\n\n正文\n", encoding="utf-8")
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "write", "20-PPT/命令行.md", "--create",
             "--content-file", str(content), "--vault", "写测试库",
             "--frontmatter", "tags=命令行"], content)
        self.assertIsNone(code, f"write 应 exit 0；stderr={err}")
        self.assertIn("①", out)
        self.assertIn("②", out)
        self.assertIn("③", out)
        self.assertIn("fish kb rollback", out)
        text = (self.vault_dir / "20-PPT" / "命令行.md").read_text(encoding="utf-8")
        self.assertIn("tags: 命令行", text)
        self.assertIn("# 命令行", text)

    def test_cli_create_existing_exits_1(self):
        content = self.tmp / "c.md"
        content.write_text("x\n", encoding="utf-8")
        self._run_main(["--root", str(self.root), "kb", "write", "20-PPT/已存在.md",
                        "--create", "--content-file", str(content),
                        "--vault", "写测试库"], content)
        code, _out, err = self._run_main(
            ["--root", str(self.root), "kb", "write", "20-PPT/已存在.md", "--create",
             "--content-file", str(content), "--vault", "写测试库"], content)
        self.assertEqual(code, 1, f"已存在再 create 应 exit 1；stderr={err}")
        self.assertIn("exists", err)

    def test_cli_append_without_frontmatter_works(self):
        """CLI 侧 `--append` 不传 `--frontmatter` 必须 exit 0（空 dict 不等于"传了"）。"""
        path = self.vault_dir / "20-PPT" / "命令行追加.md"
        path.write_text("原有\n", encoding="utf-8")
        content = self.tmp / "append.md"
        content.write_text("追加\n", encoding="utf-8")
        code, out, err = self._run_main(
            ["--root", str(self.root), "kb", "write", "20-PPT/命令行追加.md",
             "--append", "--content-file", str(content), "--vault", "写测试库"])
        self.assertIsNone(code, f"append 应 exit 0；stderr={err}")
        self.assertIn("①", out)
        self.assertTrue(path.read_text(encoding="utf-8").endswith("追加\n"))

    def test_cli_update_without_base_exits_1(self):
        content = self.tmp / "c2.md"
        content.write_text("新\n", encoding="utf-8")
        path = self.vault_dir / "20-PPT" / "有内容.md"
        path.write_text("旧\n", encoding="utf-8")
        code, _out, err = self._run_main(
            ["--root", str(self.root), "kb", "write", "20-PPT/有内容.md", "--update",
             "--content-file", str(content), "--vault", "写测试库"], content)
        self.assertEqual(code, 1, f"缺 base_sha256 应 exit 1；stderr={err}")
        self.assertIn("stale_base", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
