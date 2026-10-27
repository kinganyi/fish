#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B3 · 知识库安全判据（路径逃逸 / 类型 / 隐私 / 审计词表）（T04）。

被测定义（§5.1 / §5.2 / §5.3 / §5.6 / 裁决 §四.7）：
  ① 8 类坏路径全拒（`..` / 绝对 / 盘符 / UNC / `\\?\\` / `/etc` / 空 / junction 子路径）；
  ② 越界判定用 `realpath + commonpath`，**禁止 `str.startswith`**（假前缀漏洞）；
  ③ 媒体/二进制按扩展名 + 魔数双判；非 UTF-8 明说 `bad_encoding`；
  ④ `kb_access.meta` 只存 query_hash 前 12 位 + 长度 + 耗时——**不存 query 原文与正文**；
  ⑤ `citizen_id=None → 'unattributed'`；action/result/via 词表非法值直接报错。

## 变异自证（X-1，本文件承担 B3 的两条）

变异 A —— 对应 `test_fake_sibling_prefix_is_not_inside`：
  把 `kb_resolve_target` 的 `commonpath` 换成 `str(target).startswith(str(real_root))`
  （**不带分隔符**的朴素写法）→ `…\\vault-evil\\x.md` 以 `…\\vault` 为前缀 → 被判成"在库里"
  → 断言"必须拒绝"红。这条变异就是 PRD §7.1 那个真实漏洞。

变异 B —— 对应 `test_junction_inside_vault_is_rejected`：
  删掉 `_kb_first_reparse` 的逐级检查 → vault 内指向库外目录的 junction 不再被点名拒绝
  （若 commonpath 恰好也抓到，则换成就地 junction 的情形）→ 断言 red。

纪律（K12）：vault 在 `tempfile.mkdtemp()` 里造；junction 用 `cmd /c mklink /J`（无需管理员），
建不出来就 skip 并明说——**不许**因为环境限制把判据改成"看起来过了"。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


def _make_junction(link: Path, target: Path) -> bool:
    """建目录联接（`mklink /J`，无需管理员）。失败返回 False，由调用方 skip。"""
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and link.exists()


class KBSecurityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-kbs-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.vault_dir = self.tmp / "vault"
        (self.vault_dir / "00-首页").mkdir(parents=True)
        (self.vault_dir / "00-首页" / "a.md").write_text("# a\n正文\n", encoding="utf-8")
        (self.vault_dir / ".obsidian").mkdir()
        (self.vault_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        self.vault = fish.kb_vault_add(self.conn, name="安全库",
                                       path=str(self.vault_dir), mode="rw")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _code(self, rel, *, for_write=False):
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_resolve_target(self.vault, rel, for_write=for_write)
        return ctx.exception.code

    # ============================================================ 8 类坏路径
    def test_bad_paths_are_rejected(self):
        """8 类坏路径全部拒绝，且 `code` 与设计 §5.1 对齐（不认文案）。"""
        cases = [
            ("../windows/win.ini", "path_invalid", False),
            ("a/../../b.md", "path_invalid", False),
            ("C:\\Windows\\win.ini", "path_invalid", False),
            ("c:/Windows/win.ini", "path_invalid", False),
            ("\\\\server\\share\\x.md", "path_invalid", False),
            ("\\\\?\\C:\\x.md", "path_invalid", False),
            ("/etc/passwd", "path_invalid", False),
            ("", "path_invalid", True),          # 空路径：读=根（合法），写=非法
        ]
        for rel, want, for_write in cases:
            with self.subTest(rel=rel, for_write=for_write):
                self.assertEqual(self._code(rel, for_write=for_write), want)

    def test_empty_path_is_root_for_read(self):
        target = fish.kb_resolve_target(self.vault, "")
        self.assertEqual(target["rel"], "")
        self.assertEqual(target["abs"], Path(os.path.realpath(str(self.vault_dir))))

    def test_junction_inside_vault_is_rejected(self):
        """junction 把路径带到库外 → 必须拒绝（`outside_vault` 先于 reparse 判）。"""
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "秘密.md").write_text("库外内容\n", encoding="utf-8")
        link = self.vault_dir / "link"
        if not _make_junction(link, outside):
            self.skipTest("mklink /J 不可用（环境限制），junction 判据未验证")
        code = self._code("link/秘密.md")
        self.assertEqual(code, "outside_vault",
                         "先 realpath 再 commonpath：库外就是库外（不是靠文案猜）")
        with self.assertRaises(fish.KBError):
            fish.kb_open(self.conn, self.vault, "link/秘密.md")

    def test_junction_inside_vault_to_inside_dir_is_reparse_point(self):
        """**变异 B**：junction 指向库内目录时 commonpath 抓不到——必须靠逐级 reparse 检查。"""
        alias = self.vault_dir / "alias"
        if not _make_junction(alias, self.vault_dir / "00-首页"):
            self.skipTest("mklink /J 不可用（环境限制），reparse 判据未验证")
        code = self._code("alias/a.md")
        self.assertEqual(code, "reparse_point",
                         "库内 junction 只有逐级 reparse 能发现（commonpath 会放行）")

    def test_fake_sibling_prefix_is_not_inside(self):
        """**变异 A**：`…\\vault-evil` 与 `…\\vault` 只是前缀相同，不是子路径。"""
        evil = self.tmp / "vault-evil"
        evil.mkdir()
        (evil / "x.md").write_text("evil\n", encoding="utf-8")
        self.assertTrue(str(evil).startswith(str(self.vault_dir)),
                        "夹具前提：evil 目录必须与 vault 共享前缀")
        # ① 直接给带 .. 的相对路径：在"穿越"这一步就被拒（path_invalid）
        self.assertEqual(self._code("../vault-evil/x.md"), "path_invalid")
        # ② 真正的假前缀考验：vault 内造一条指向 vault-evil 的 junction。
        #    这里断言**具体 code = outside_vault**（而不是"两者之一"）：
        #    设计 §5.1 的顺序是 ④ commonpath 先、⑤ reparse 后，所以假前缀必须由
        #    commonpath 判死。若退回 startswith，`…\\vault-evil` 会被判"在库里"，
        #    直到 ⑤ 才被兜住 → code 变成 reparse_point → 本断言红（变异 A 的证明点）。
        link = self.vault_dir / "evil-link"
        if not _make_junction(link, evil):
            self.skipTest("mklink /J 不可用（环境限制），假前缀判据未验证")
        code = self._code("evil-link/x.md")
        self.assertEqual(code, "outside_vault",
                         f"假前导目录被判成'在库里'（startswith 漏洞），实际 code={code}")

    def test_ignored_paths_are_rejected_even_when_existing(self):
        """§5.2：忽略目录即使显式给路径也拒（它不可写、也不该被 AI 读）。"""
        self.assertEqual(self._code(".obsidian/app.json"), "ignored")
        fish.kb_vault_add(self.conn, name="安全库", path=str(self.vault_dir),
                          mode="rw", ignore=["99-草稿/**"])
        self.vault = fish.kb_vault_get(self.conn, "安全库")
        (self.vault_dir / "99-草稿").mkdir()
        (self.vault_dir / "99-草稿" / "x.md").write_text("草稿\n", encoding="utf-8")
        self.assertEqual(self._code("99-草稿/x.md"), "ignored")
        self.assertEqual(self._code("99-草稿"), "ignored", "目录本身也要按忽略处理")

    def test_nonexistent_and_missing_parent(self):
        self.assertEqual(self._code("没有这个.md"), "not_found")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_resolve_target(self.vault, "没有这个目录/x.md", for_write=True)
        self.assertEqual(ctx.exception.code, "not_found")

    # ============================================================ 类型（§5.3）
    def test_media_detection_by_ext_and_magic(self):
        for name, payload in (
                ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8),
                ("y.exe", b"MZ\x90\x00" + b"\x00" * 8),
                ("假PDF.md", b"%PDF-1.7\n"),
                ("假WEBP.md", b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)):
            with self.subTest(name=name):
                path = self.vault_dir / name
                path.write_bytes(payload)
                detected = fish.kb_detect_text(path.read_bytes(), ext=path.suffix)
                self.assertTrue(detected["media"], f"{name} 没被识别为媒体：{detected}")
                with self.assertRaises(fish.KBError) as ctx:
                    fish.kb_open(self.conn, self.vault, name)
                self.assertEqual(ctx.exception.code, "media")

    def test_null_byte_and_control_ratio_are_binary(self):
        (self.vault_dir / "含NUL.md").write_bytes(b"abc\x00def")
        detected = fish.kb_detect_text((self.vault_dir / "含NUL.md").read_bytes(),
                                       ext=".md")
        self.assertTrue(detected["media"])
        self.assertIn("nul", detected["reason"])
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "含NUL.md")
        self.assertEqual(ctx.exception.code, "media")

    def test_bad_encoding_is_explicit(self):
        (self.vault_dir / "乱码.md").write_bytes(b"abc\xff\xff\xff")
        detected = fish.kb_detect_text((self.vault_dir / "乱码.md").read_bytes(),
                                       ext=".md")
        self.assertFalse(detected["is_text"])
        self.assertFalse(detected["media"])
        self.assertEqual(detected["reason"], "bad_encoding")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_open(self.conn, self.vault, "乱码.md")
        self.assertEqual(ctx.exception.code, "bad_encoding")

    def test_gb18030_is_readable(self):
        """反向判据：gb18030 文本**不该**被当成坏编码（拒绝要有边界，不能见非 UTF-8 就拒）。"""
        (self.vault_dir / "gbk.md").write_bytes("中文内容\n".encode("gb18030"))
        res = fish.kb_open(self.conn, self.vault, "gbk.md")
        self.assertEqual(res["encoding"], "gb18030")
        self.assertIn("中文内容", res["text"])

    # ============================================================ 隐私（§5.6）
    def test_meta_never_contains_query_or_body(self):
        secret = "隐私探针词丁"
        (self.vault_dir / "00-首页" / "私.md").write_text(
            f"{secret} 出现在正文里\n", encoding="utf-8")
        fish.kb_search(self.conn, self.vault, secret, citizen_id="CIT1")
        fish.kb_open(self.conn, self.vault, "00-首页/私.md", citizen_id="CIT1")
        dumped = json.dumps(
            [list(r) for r in self.conn.execute(
                "SELECT path,meta,sha256,backup_path FROM kb_access").fetchall()],
            ensure_ascii=False)
        self.assertNotIn(secret, dumped, "query 原文或正文漏进了 kb_access")
        search_meta = json.loads(self.conn.execute(
            "SELECT meta FROM kb_access WHERE action='search'").fetchone()[0])
        self.assertEqual(set(search_meta) & {"query", "text", "body"}, set())
        self.assertEqual(len(search_meta["query_hash"]), 12)
        self.assertEqual(search_meta["qlen"], len(secret))

    def test_meta_whitelist_drops_unknown_keys(self):
        fish.kb_log_access(self.conn, self.vault, rel="00-首页/a.md", action="open",
                           citizen_id=None, via="cli:kb",
                           meta={"query": "明文", "body": "正文",
                                 "custom": "随便", "qlen": 2})
        meta = json.loads(self.conn.execute(
            "SELECT meta FROM kb_access").fetchone()[0])
        self.assertNotIn("query", meta)
        self.assertNotIn("body", meta)
        self.assertNotIn("custom", meta)
        self.assertEqual(meta["qlen"], 2)

    def test_meta_is_truncated_under_1k(self):
        fish.kb_log_access(self.conn, self.vault, rel="", action="reject",
                           result="denied", citizen_id=None, via="cli:kb",
                           meta={k: "x" * 500 for k in
                                 ("reason", "depth", "limit", "max_chars",
                                  "max_tokens", "scan_ms", "scanned_files",
                                  "scanned_bytes")})
        raw = self.conn.execute("SELECT meta FROM kb_access").fetchone()[0]
        self.assertLessEqual(len(raw.encode("utf-8")), 1024)
        self.assertTrue(json.loads(raw).get("meta_truncated"))

    def test_unattributed_mapping_once(self):
        self.assertEqual(fish.KB_UNATTRIBUTED, "unattributed")
        fish.kb_log_access(self.conn, self.vault, rel="", action="tree", via="cli:kb")
        row = self.conn.execute("SELECT citizen_id FROM kb_access").fetchone()
        self.assertEqual(row[0], "unattributed")
        fish.kb_log_access(self.conn, self.vault, rel="", action="tree",
                           citizen_id="01ABC", via="cli:kb")
        row = self.conn.execute(
            "SELECT citizen_id FROM kb_access ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "01ABC")

    def test_vocab_is_enforced(self):
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_log_access(self.conn, self.vault, rel="", action="nope",
                               via="cli:kb")
        self.assertEqual(ctx.exception.code, "bad_action")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_log_access(self.conn, self.vault, rel="", action="open",
                               result="nope", via="cli:kb")
        self.assertEqual(ctx.exception.code, "bad_action")
        with self.assertRaises(fish.KBError) as ctx:
            fish.kb_log_access(self.conn, self.vault, rel="", action="open",
                               via="browser")
        self.assertEqual(ctx.exception.code, "bad_usage")

    def test_rejected_write_is_recorded(self):
        """全事件流水：mode=ro 的写、越界路径都留痕（result 用词表内的值）。"""
        fish.kb_vault_set_mode(self.conn, "安全库", "ro")
        self.vault = fish.kb_vault_get(self.conn, "安全库")
        with self.assertRaises(fish.KBError):
            fish.kb_write(self.conn, self.root, self.vault, "00-首页/新.md",
                          action="create", content="x\n")
        row = self.conn.execute(
            "SELECT action, result FROM kb_access ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "reject")
        self.assertEqual(row[1], "denied", "mode_ro 不在 result 词表里 → 归到 denied")
        self.assertEqual("mode_ro" in fish.KB_RESULTS, False)

    def test_absolute_and_device_paths_never_touch_filesystem(self):
        for rel in ("C:\\Windows\\win.ini", "\\\\?\\C:\\x", "/etc/passwd"):
            with self.subTest(rel=rel):
                self.assertEqual(self._code(rel), "path_invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
