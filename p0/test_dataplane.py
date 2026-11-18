#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1 判据：资产层（kb_asset）+ 魔术字节分类器 + 扫描器。

## 这批判据守什么

`kb_index` 只装笔记（296 行全 `.md`），而库里实际 2,526 文件 / 10,965 MB →
**2,230 个文件对 fish 完全不可见**。没有资产层，10.7 GB 既不能迁（不知道搬什么）
也不能引用（不知道哪些被笔记用到）。

## 两个必须能失败的判据

**D1（魔术字节）**：实测库里有 **93 个文件的扩展名是 `.bin` / `.pn`，实际都是 PNG**。
按扩展名分类会把它们判成 `other`。判据直接钉死"这 93 个必须是 image，
且判定依据必须是 magic 而不是 ext"。

**D2（覆盖）**：扫描器数出来的文件数必须与文件系统读到的**同口径**一致。
少一个就是漏扫——漏扫会让迁移丢文件，而丢文件是不可逆的。
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

import fish_schema                                   # noqa: E402
import fish_dataplane as DP                          # noqa: E402

REAL_VAULT = Path(r"E:\Obsidian\安逸知识库")

# 真 PNG 头 + 一点体积，用来造"扩展名撒谎"的样本
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF"
MP4_HEAD = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"
ZIP_HEAD = b"PK\x03\x04" + b"\x00" * 24


def _mk_schema_conn():
    c = sqlite3.connect(":memory:")
    c.executescript(fish_schema.SCHEMA)
    return c


class MagicByteTest(unittest.TestCase):
    """D1：分类只看魔术字节。"""

    def test_png_behind_lying_extension_is_image(self):
        """**这是 2026-11-18 实测缺陷的直接探针。**

        库里有 93 个 `.bin` / `.pn`，头部是 PNG 签名。按扩展名分类会判成 other。
        """
        for ext in (".bin", ".pn", ".dat", ""):
            kind, mime, how = DP.classify_bytes(PNG_HEAD, "x" + ext)
            self.assertEqual(kind, "image", f"扩展名 {ext!r} 的 PNG 被判成 {kind}")
            self.assertEqual(how, "magic", f"判定依据应是 magic，实际 {how}")
            self.assertEqual(mime, "image/png")

    def test_other_formats_by_magic(self):
        cases = [
            (JPEG_HEAD, "image", "image/jpeg"),
            (GIF_HEAD := b"GIF89a" + b"\x00" * 8, "image", "image/gif"),
            (MP4_HEAD, "video", "video/mp4"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image", "image/webp"),
            (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio", "audio/wav"),
            (b"%PDF-1.7\n", "doc", "application/pdf"),
            (ZIP_HEAD, "archive", "application/zip"),
            (b"ID3\x03\x00", "audio", "audio/mpeg"),
        ]
        for head, kind, mime in cases:
            got_kind, got_mime, how = DP.classify_bytes(head, "x.unknown")
            self.assertEqual(got_kind, kind, f"{head[:12]!r} → {got_kind}")
            self.assertEqual(got_mime, mime)
            self.assertEqual(how, "magic")

    def test_extension_is_only_fallback(self):
        """扩展名只在魔术字节判不出来时用，且 `how` 必须如实写 `ext`。"""
        kind, mime, how = DP.classify_bytes(b'{"a":1}', "x.json")
        self.assertEqual((kind, how), ("doc", "ext"),
                         "无魔数的 json 应落 doc/ext，而不是 other")

    def test_text_extensions_never_fall_to_other(self):
        """文本类文件不许落 other —— other 应该只留给真判不出来的。

        实测教训：第一版漏了 `.json`，78 个文件全落 other。那不是"其它二进制"，
        是 Obsidian 的插件数据与清单文件。
        """
        for name in ("a.json", "b.yaml", "c.csv", "d.html", "e.py", "f.css", "g.pptx"):
            kind, _mime, _how = DP.classify_bytes(b"plain text", name)
            self.assertNotEqual(kind, "other", f"{name} 不该落 other")


class ScanTest(unittest.TestCase):
    """D2：扫描覆盖 + 笔记/资产分离。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "10-漫剧" / "_media").mkdir(parents=True)
        (root / "30-自我").mkdir()
        (root / ".obsidian").mkdir()
        (root / "90-模板").mkdir()
        # 笔记
        (root / "10-漫剧" / "剧本.md").write_text("---\ntags:\n  - 漫剧\n---\n# 剧本\n", encoding="utf-8")
        (root / "30-自我" / "画像.md").write_text("# 画像\n", encoding="utf-8")
        # 资产（含 2 个撒谎扩展名）
        (root / "10-漫剧" / "_media" / "图.bin").write_bytes(PNG_HEAD * 10)
        (root / "10-漫剧" / "_media" / "图.pn").write_bytes(PNG_HEAD * 10)
        (root / "10-漫剧" / "_media" / "视频.mp4").write_bytes(MP4_HEAD * 10)
        # 应被忽略
        (root / ".obsidian" / "app.json").write_bytes(b'{"x":1}')
        (root / "90-模板" / "模板.md").write_text("# t\n", encoding="utf-8")
        self.root = root
        self.vault = {"vault_id": "V1", "path": str(root),
                      "ignore_globs": [".obsidian/**", "90-模板/**"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_counts_match_filesystem(self):
        conn = _mk_schema_conn()
        r = DP.scan_vault(conn, self.vault, dry_run=True)
        # 未被忽略的物理文件：剧本.md 画像.md 图.bin 图.pn 视频.mp4 = 5
        self.assertEqual(r["files"], 5, f"扫描数 {r['files']} 与文件系统 5 不符")
        self.assertEqual(r["notes"], 2, "笔记数应为 2")
        self.assertEqual(r["assets"], 3, "资产数应为 3")

    def test_md_never_enters_asset_table(self):
        """笔记归 kb_index，资产归 kb_asset —— 两张表不许打架。"""
        conn = _mk_schema_conn()
        DP.scan_vault(conn, self.vault, dry_run=False)
        rels = [r[0] for r in conn.execute("SELECT rel_path FROM kb_asset")]
        self.assertEqual(len(rels), 3, f"资产表应 3 行，实际 {rels}")
        for r in rels:
            self.assertFalse(r.endswith(".md"), f"笔记混进了资产表：{r}")

    def test_lying_extensions_are_image_in_db(self):
        """落地后仍必须是 image（不是扫描函数返回值对、入库就变）。"""
        conn = _mk_schema_conn()
        DP.scan_vault(conn, self.vault, dry_run=False)
        got = dict(conn.execute("SELECT rel_path, kind FROM kb_asset").fetchall())
        for rel in ("10-漫剧/_media/图.bin", "10-漫剧/_media/图.pn"):
            self.assertEqual(got.get(rel), "image", f"{rel} 入库后是 {got.get(rel)}")

    def test_ignored_dirs_are_skipped(self):
        conn = _mk_schema_conn()
        rels = set(DP.walk_vault(self.root, self.vault["ignore_globs"]))
        self.assertNotIn(".obsidian/app.json", rels)
        self.assertFalse(any(r.startswith("90-模板/") for r in rels),
                         f"90-模板 没被忽略：{rels}")

    def test_dry_run_writes_nothing(self):
        conn = _mk_schema_conn()
        DP.scan_vault(conn, self.vault, dry_run=True)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM kb_asset").fetchone()[0], 0,
                         "dry_run 不许写库")

    def test_hash_is_optional(self):
        """默认不算哈希（10.7 GB 全算很慢）；要求算时必须算出来且稳定。"""
        conn = _mk_schema_conn()
        DP.scan_vault(conn, self.vault, dry_run=False, compute_hash=False)
        h1 = conn.execute("SELECT content_hash FROM kb_asset LIMIT 1").fetchone()[0]
        self.assertIsNone(h1, "默认不该算哈希")
        conn.execute("DELETE FROM kb_asset")
        DP.scan_vault(conn, self.vault, dry_run=False, compute_hash=True)
        hs = [r[0] for r in conn.execute("SELECT content_hash FROM kb_asset")]
        self.assertTrue(all(h and len(h) == 64 for h in hs), f"哈希形态不对：{hs}")


@unittest.skipUnless(REAL_VAULT.is_dir(), "真实知识库不在这台机器上——判不到，不是通过")
class RealVaultTest(unittest.TestCase):
    """对真实 vault 的验收（本机才有）。"""

    GLOBS = [".obsidian/**", ".trash/**", "90-模板/**"]

    def test_93_lying_extensions_are_images(self):
        """**实测基线**：库里 93 个 `.bin`/`.pn` 全部是 PNG，必须全部判 image。"""
        rels = [r for r in DP.walk_vault(REAL_VAULT, self.GLOBS)
                if r.lower().endswith((".bin", ".pn"))]
        self.assertEqual(len(rels), 93, f"翘曲扩展名文件数变了：{len(rels)}（基线 93）")
        bad = []
        for rel in rels:
            kind, _mime, how = DP.classify_file(str(REAL_VAULT / rel))
            if kind != "image" or how != "magic":
                bad.append((rel, kind, how))
        self.assertEqual(bad, [], f"这些没被认成 image：{bad[:5]}")

    def test_no_file_falls_to_other(self):
        """真库里不该有文件落 other —— 落 other 说明分类表漏了类型。"""
        conn = _mk_schema_conn()
        v = {"vault_id": "V1", "path": str(REAL_VAULT), "ignore_globs": self.GLOBS}
        r = DP.scan_vault(conn, v, dry_run=True)
        self.assertEqual(r["by_kind"].get("other", 0), 0,
                         f"有 {r['by_kind'].get('other')} 个文件落 other："
                         f"{r['by_how']}")
        self.assertGreater(r["assets"], 2000, f"资产数异常少：{r['assets']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
