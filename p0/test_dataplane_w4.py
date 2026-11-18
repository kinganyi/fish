#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4 判据：引用反查（`referenced_by`）与缩略图。

## 守什么

**R1 引用写法必须覆盖主流形态。** 实测真实库三种写法：`![alt](path)` 2,151 处、
`[[名]]` 928 处、`![[资产]]` 0 处。**只做 wikilink 会漏掉 100% 的资产引用**——
所以判据同时钉 Markdown 图片语法。

**R2 相对路径必须能解析。** 笔记里的 `_media/x.mp4` 是**相对笔记目录**的，
不是相对 vault 根。解析错会让整张反查表变成空的。

**R3 不唯一就不猜。** 同名文件有多个时按名字匹配会误连——宁可解析失败，
也不制造假引用。假引用比没引用更危险（会让人以为某资产有用）。

**R4 缩略图必须真的变小，且不算进资产体积。**
12.87 MB → 61.2 KB 是实测；如果哪天变成"和原图一样大"，说明缩放没生效。

**R5 视频没有 ffmpeg 时必须如实跳过。** 不许静默当成成功。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish_schema          # noqa: E402
import fish_dataplane as DP  # noqa: E402

PNG = None


def _png_bytes(w=64, h=48, color=(200, 30, 30)):
    """造一张真 PNG（用 Pillow；没有就跳过相关判据）。"""
    from PIL import Image
    import io
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class RefTest(unittest.TestCase):
    """R1 / R2 / R3。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "10-漫剧" / "_media").mkdir(parents=True)
        (self.root / "_media").mkdir()
        (self.root / "10-漫剧" / "_media" / "视频.mp4").write_bytes(b"\x00" * 32)
        (self.root / "10-漫剧" / "_media" / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        (self.root / "_media" / "根图.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 32)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(fish_schema.SCHEMA)
        self.vault = {"vault_id": "V1", "path": str(self.root),
                      "ignore_globs": [".obsidian/**"]}
        DP.scan_vault(self.conn, self.vault, dry_run=False)

    def tearDown(self):
        self.tmp.cleanup()

    def _note(self, rel, body):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    def _refs(self, asset_rel):
        r = self.conn.execute(
            "SELECT referenced_by FROM kb_asset WHERE vault_id='V1' AND rel_path=?",
            (asset_rel,)).fetchone()
        return json.loads(r[0]) if r else None

    def test_markdown_image_syntax_is_parsed(self):
        """**R1**：`![](相对路径)` 必须被解析——实测这是主流写法（2,151 处）。

        只做 wikilink 的实现在这条上必红。
        """
        self._note("10-漫剧/剧本.md", "看图：\n\n![](./_media/图.png)\n")
        DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(self._refs("10-漫剧/_media/图.png"), ["10-漫剧/剧本.md"],
                         "Markdown 图片语法没被解析")

    def test_relative_to_note_dir_not_vault_root(self):
        """**R2**：`_media/x.mp4` 是相对**笔记目录**的。

        如果错解成相对 vault 根，`10-漫剧/剧本.md` 里的 `_media/图.png`
        会指向根下的 `_media/`，反查表就是错的。
        """
        self._note("10-漫剧/剧本.md", "![](./_media/图.png)")
        DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(self._refs("10-漫剧/_media/图.png"), ["10-漫剧/剧本.md"])
        # 根下那个同名不同目录的资产不该被误连
        self.assertEqual(self._refs("_media/根图.jpg"), [],
                         "根目录资产被误连了")

    def test_wikilink_embed_also_parsed(self):
        """`![[资产]]` 实测 0 处，但支持成本极低，留着防以后。"""
        self._note("10-漫剧/剧本.md", "![[10-漫剧/_media/图.png]]")
        DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(self._refs("10-漫剧/_media/图.png"), ["10-漫剧/剧本.md"])

    def test_duplicate_basename_is_not_guessed(self):
        """**R3**：同名文件有多个时**不猜**。

        假引用比没引用更危险——它会让人以为某资产有用，从而不敢归档。
        """
        (self.root / "10-漫剧" / "_media" / "dup.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x01" * 8)
        (self.root / "_media" / "dup.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x02" * 8)
        DP.scan_vault(self.conn, self.vault, dry_run=False)
        # 只写裸文件名，不写路径 → 同名两处，应解析失败而不是随便挑一个
        self._note("笔记.md", "![](dup.png)")
        r = DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(r["referenced"], 0,
                         f"同名文件被猜了：{r}")
        self.assertGreaterEqual(r["unresolved"], 1, "应计入 unresolved")

    def test_http_urls_are_ignored(self):
        self._note("笔记.md", "![](https://example.com/x.png)")
        r = DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(r["referenced"], 0, "外链被当成资产了")

    def test_unreferenced_count_is_reported(self):
        """这个读数是 W4 的核心产出：多少资产没有任何笔记引用。"""
        self._note("笔记.md", "![](10-漫剧/_media/图.png)")
        r = DP.build_referenced_by(self.conn, self.vault)
        self.assertEqual(r["assets"], 3)
        self.assertEqual(r["referenced"], 1)
        self.assertEqual(r["unreferenced"], 2)


class ThumbTest(unittest.TestCase):
    """R4 / R5。"""

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow——判不到，不是通过")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "vault"
        (self.root / "_media").mkdir(parents=True)
        (self.root / "_media" / "大图.png").write_bytes(_png_bytes(1200, 900))
        (self.root / "_media" / "视频.mp4").write_bytes(b"\x00" * 16)
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(fish_schema.SCHEMA)
        self.vault = {"vault_id": "V1", "path": str(self.root), "ignore_globs": []}
        DP.scan_vault(self.conn, self.vault, dry_run=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_thumbnail_is_actually_smaller(self):
        """**R4**：缩略图必须真的变小。和原图一样大说明缩放没生效。"""
        r = DP.make_thumbnails(self.conn, self.vault, data_root=self.tmp.name)
        self.assertEqual(r["made"], 1, f"没生成：{r}")
        self.assertEqual(r["failed"], 0)
        self.assertLess(r["bytes_out"], r["bytes_in"] * 0.2,
                        f"缩略图没变小：{r['bytes_in']} → {r['bytes_out']}")
        tr = self.conn.execute(
            "SELECT thumb_rel FROM kb_asset WHERE rel_path LIKE '%大图.png'").fetchone()[0]
        self.assertTrue(tr and tr.endswith(".webp"), f"thumb_rel 不对：{tr}")
        self.assertTrue((Path(self.tmp.name) / "thumb" / tr).is_file(), "缩略图文件不存在")

    def test_thumbnail_not_counted_in_asset_size(self):
        """**R4 续**：缩略图是派生数据，不许改 `size`（否则"库多大"失真）。"""
        before = self.conn.execute(
            "SELECT size FROM kb_asset WHERE rel_path LIKE '%大图.png'").fetchone()[0]
        DP.make_thumbnails(self.conn, self.vault, data_root=self.tmp.name)
        after = self.conn.execute(
            "SELECT size FROM kb_asset WHERE rel_path LIKE '%大图.png'").fetchone()[0]
        self.assertEqual(before, after, "生成缩略图后 size 被改了")

    def test_video_is_skipped_honestly(self):
        """**R5**：没有 ffmpeg，视频必须计入 skipped，不许静默成功。"""
        r = DP.make_thumbnails(self.conn, self.vault, data_root=self.tmp.name,
                               kinds=("image", "video"))
        self.assertGreaterEqual(r["video_skipped"], 1,
                                f"视频没被如实计数：{r}")
        self.assertEqual(r["made"], 1, "图片仍应正常生成")

    def test_dry_run_makes_nothing(self):
        r = DP.make_thumbnails(self.conn, self.vault, data_root=self.tmp.name, dry_run=True)
        self.assertTrue(r["dry_run"])
        self.assertEqual(r["made"], 0)
        self.assertFalse((Path(self.tmp.name) / "thumb").exists(), "dry_run 建了目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
