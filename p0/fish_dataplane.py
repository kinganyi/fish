#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 数据面 · 资产层（W1，2026-11-18）。

## 它解决什么

`kb_index` 只装**笔记**——它的列全是 `headings` / `summary` / `excerpt` / `word_count`
这种笔记语义，二进制没有位置可放。实测后果：

    安逸知识库实际   2,526 文件 / 10,965 MB
    kb_index           296 行，全是 .md，合计 3.04 MB
    差额             2,230 个文件 / 10,962 MB 对 fish 完全不可见

没有这一层，10.7 GB 既不能迁移（不知道搬什么）、不能引用（不知道哪些被笔记用到）、
也不能去重。

## 分层约束（J2）

本模块只依赖底层（`fish_common` / `fish_schema` / `fish_db`），**不 import `fish`**——
和 `fish_profile` / `fish_kb` 同样的约束。

## 一条硬约束：分类只看魔术字节

实测库里 **93 个文件的扩展名是 `.bin` / `.pn`，实际都是 PNG**
（即梦导出时扩展名被截断，头部是 `89 50 4E 47 0D 0A 1A 0A`）：

    090_未命名.bin          头: 89 50 4E 47 0D 0A 1A 0A
    025_jimeng-2025-...pn   头: 89 50 4E 47 0D 0A 1A 0A

按扩展名分类会把它们判成 `other`，缩略图 / 尺寸提取 / 引用解析全部失效。
所以 `classify_bytes()` 是唯一判定入口，扩展名只在**魔术字节判不出来时**作兜底提示。
"""
from __future__ import annotations

import fnmatch
import hashlib
import mimetypes
import os
from pathlib import Path

from fish_common import now_utc, ulid

# ============================================================== 魔术字节表
#
# 顺序有意义：先匹配到的优先。每一行是 (偏移, 魔数, kind, mime)。
# 只覆盖这个库真实出现的类型 + 常见办公格式；其余落 other，不猜。
_MAGIC = (
    (0, b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (0, b"\xff\xd8\xff", "image", "image/jpeg"),
    (0, b"GIF87a", "image", "image/gif"),
    (0, b"GIF89a", "image", "image/gif"),
    (0, b"BM", "image", "image/bmp"),
    (0, b"8BPS", "image", "image/vnd.adobe.photoshop"),      # .psd
    (0, b"%PDF", "doc", "application/pdf"),
    (0, b"PK\x03\x04", "archive", "application/zip"),        # zip / docx / xlsx / pptx
    (0, b"PK\x05\x06", "archive", "application/zip"),        # 空 zip
    (0, b"PK\x07\x08", "archive", "application/zip"),
    (0, b"\x1f\x8b", "archive", "application/gzip"),
    (0, b"7z\xbc\xaf\x27\x1c", "archive", "application/x-7z-compressed"),
    (0, b"Rar!\x1a\x07", "archive", "application/vnd.rar"),
    (0, b"ID3", "audio", "audio/mpeg"),
    (0, b"OggS", "audio", "audio/ogg"),
    (0, b"fLaC", "audio", "audio/flac"),
)

# 需要额外结构检查的容器格式（RIFF / ftyp）
_RIFF_KINDS = {b"WEBP": ("image", "image/webp"),
               b"WAVE": ("audio", "audio/wav"),
               b"AVI ": ("video", "video/x-msvideo")}

# 扩展名兜底（**只在魔术字节判不出来时用**）
_EXT_FALLBACK = {
    ".md": ("doc", "text/markdown"), ".markdown": ("doc", "text/markdown"),
    ".txt": ("doc", "text/plain"), ".canvas": ("doc", "application/json"),
    ".mp4": ("video", "video/mp4"), ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"), ".mkv": ("video", "video/x-matroska"),
    ".webm": ("video", "video/webm"),
    ".mp3": ("audio", "audio/mpeg"), ".wav": ("audio", "audio/wav"),
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"), ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"), ".psd": ("image", "image/vnd.adobe.photoshop"),
    ".pdf": ("doc", "application/pdf"),
    ".zip": ("archive", "application/zip"),
    # 文本/结构化数据：魔术字节判不出来（没有魔数），但它们是**内容**不是二进制。
    # 实测库里 78 个 .json 全都落到 other —— 那不是"其它二进制"，是 Obsidian 的
    # 插件数据与清单文件，归 doc 才对（以后要能读、能索引）。
    ".json": ("doc", "application/json"),
    ".yaml": ("doc", "application/yaml"),
    ".yml": ("doc", "application/yaml"),
    ".csv": ("doc", "text/csv"),
    ".tsv": ("doc", "text/tab-separated-values"),
    ".html": ("doc", "text/html"),
    ".htm": ("doc", "text/html"),
    ".xml": ("doc", "application/xml"),
    ".css": ("doc", "text/css"),
    ".js": ("doc", "text/javascript"),
    ".py": ("doc", "text/x-python"),
    ".sh": ("doc", "text/x-shellscript"),
    ".log": ("doc", "text/plain"),
    ".rtf": ("doc", "application/rtf"),
    ".doc": ("doc", "application/msword"),
    ".xls": ("doc", "application/vnd.ms-excel"),
    ".ppt": ("doc", "application/vnd.ms-powerpoint"),
    ".docx": ("doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": ("doc", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": ("doc", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}

HEAD_BYTES = 64          # 判类型只需要前 64 字节


def classify_bytes(head: bytes, name: str = "") -> tuple:
    """**唯一判定入口**：返回 (kind, mime, how)。

    `how` 取值 `magic` / `ext` / `none`，用于审计"这条判断靠什么下的"。
    魔术字节优先——见模块 docstring 里那 93 个 `.bin`/`.pn` 的实测。
    """
    h = head or b""
    for off, sig, kind, mime in _MAGIC:
        if h[off:off + len(sig)] == sig:
            # zip 容器需要细分：docx/xlsx/pptx 也是 zip
            if sig == b"PK\x03\x04":
                sub = _zip_subtype(h)
                if sub:
                    return sub[0], sub[1], "magic"
            return kind, mime, "magic"

    # RIFF 容器：WEBP / WAVE / AVI
    if h[:4] == b"RIFF" and len(h) >= 12:
        tag = h[8:12]
        if tag in _RIFF_KINDS:
            return _RIFF_KINDS[tag][0], _RIFF_KINDS[tag][1], "magic"

    # ISO BMFF（mp4/m4v/mov）：偏移 4 处是 'ftyp'
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"qt  ",):
            return "video", "video/quicktime", "magic"
        return "video", "video/mp4", "magic"

    # MP3 帧同步（无 ID3 头的老文件）
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0:
        return "audio", "audio/mpeg", "magic"

    # 魔术字节判不出来才看扩展名
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _EXT_FALLBACK:
        return _EXT_FALLBACK[ext][0], _EXT_FALLBACK[ext][1], "ext"

    mime = mimetypes.guess_type(name or "")[0]
    if mime:
        if mime.startswith("image/"):
            return "image", mime, "ext"
        if mime.startswith("video/"):
            return "video", mime, "ext"
        if mime.startswith("audio/"):
            return "audio", mime, "ext"
    return "other", mime or "application/octet-stream", "none"


def _zip_subtype(head: bytes):
    """区分 docx/xlsx/pptx 与普通 zip（只看头部里的文件名线索）。"""
    low = head.lower()
    if b"word/" in low:
        return "doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if b"xl/" in low:
        return "doc", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if b"ppt/" in low:
        return "doc", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return None


def classify_file(path) -> tuple:
    """读文件头判类型。读不到就返回 ('other','application/octet-stream','none')。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return "other", "application/octet-stream", "none"
    return classify_bytes(head, os.path.basename(str(path)))


def sha256_file(path, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ============================================================== 扫描
def _ignored(rel, globs) -> bool:
    """按 vault 的 ignore_globs 判断（与 fish_kb 同一套语义：`**` 匹配任意层）。"""
    r = rel.replace("\\", "/")
    for g in globs or ():
        g = str(g or "").replace("\\", "/")
        if not g:
            continue
        if fnmatch.fnmatch(r, g) or fnmatch.fnmatch(r, g.rstrip("/")):
            return True
        if g.endswith("/**") and (r == g[:-3] or r.startswith(g[:-2])):
            return True
    return False


def walk_vault(root, globs=()) -> list:
    """列出 vault 下所有文件（相对路径，posix 风格，已按 ignore_globs 过滤）。"""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if _ignored(rel, globs):
                continue
            out.append(rel)
    return out


def scan_vault(conn, vault_row, *, dry_run=True, compute_hash=False,
               limit=None, kinds=None) -> dict:
    """扫描 vault 并写 `kb_asset`（`kb_index` 那 296 条笔记不重复入资产表）。

    ## 为什么跳过 .md

    `.md` 已经由 `kb_index` 索引（全文 + 标题 + 摘要）。资产层的职责是
    "让二进制可见"，重复登记笔记只会让两张表打架。

    但**统计上要算清楚**：返回里 `notes` 与 `assets` 分开报，两者相加才是全树。

    ## `compute_hash`

    默认 False——10.7 GB 全量哈希很慢，扫描阶段只需要"有什么、多大、什么类型"。
    迁移校验时才必须算（那是 I3/迁移判据的硬证据）。
    """
    root = Path(vault_row["path"])
    if not root.is_dir():
        return {"ok": False, "reason": "vault_missing", "path": str(root)}

    globs = vault_row.get("ignore_globs") or []
    if isinstance(globs, str):
        import json as _json
        try:
            globs = _json.loads(globs)
        except Exception:
            globs = []
    vid = vault_row["vault_id"]

    rels = walk_vault(root, globs)
    if limit:
        rels = rels[:int(limit)]

    stats = {"files": 0, "notes": 0, "assets": 0, "bytes": 0,
             "by_kind": {}, "by_how": {}, "errors": 0}
    rows = []
    for rel in rels:
        p = root / rel
        try:
            st = p.stat()
        except OSError:
            stats["errors"] += 1
            continue
        stats["files"] += 1
        stats["bytes"] += st.st_size
        if rel.lower().endswith((".md", ".markdown")):
            stats["notes"] += 1
            continue
        kind, mime, how = classify_file(p)
        if kinds and kind not in kinds:
            continue
        stats["assets"] += 1
        stats["by_kind"][kind] = stats["by_kind"].get(kind, 0) + 1
        stats["by_how"][how] = stats["by_how"].get(how, 0) + 1
        rows.append({
            "vault_id": vid, "rel_path": rel, "mtime": st.st_mtime,
            "size": st.st_size, "kind": kind, "mime": mime,
            "content_hash": sha256_file(p) if compute_hash else None,
            "how": how,
        })

    if not dry_run:
        ts = now_utc()
        conn.executemany(
            "INSERT INTO kb_asset (vault_id,rel_path,mtime,size,kind,mime,"
            "content_hash,indexed_at,status) VALUES (?,?,?,?,?,?,?,?,'active') "
            "ON CONFLICT(vault_id,rel_path) DO UPDATE SET "
            "mtime=excluded.mtime, size=excluded.size, kind=excluded.kind, "
            "mime=excluded.mime, "
            "content_hash=COALESCE(excluded.content_hash, kb_asset.content_hash), "
            "indexed_at=excluded.indexed_at, status='active'",
            [(r["vault_id"], r["rel_path"], r["mtime"], r["size"], r["kind"],
              r["mime"], r["content_hash"], ts) for r in rows])
        conn.commit()

    stats["ok"] = True
    stats["dry_run"] = bool(dry_run)
    stats["written"] = 0 if dry_run else len(rows)
    return stats


def asset_stats(conn, vault_id=None) -> dict:
    """资产层读数（按 kind 与 status 聚合）。"""
    where, args = "", ()
    if vault_id:
        where, args = " WHERE vault_id=?", (vault_id,)
    out = {"total": 0, "bytes": 0, "by_kind": {}, "by_status": {}}
    for kind, n, b in conn.execute(
            "SELECT kind, COUNT(*), COALESCE(SUM(size),0) FROM kb_asset"
            + where + " GROUP BY kind", args):
        out["by_kind"][kind] = {"count": n, "bytes": b}
        out["total"] += n
        out["bytes"] += b
    for st, n in conn.execute(
            "SELECT status, COUNT(*) FROM kb_asset" + where + " GROUP BY status", args):
        out["by_status"][st] = n
    return out
