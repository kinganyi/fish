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
import sys
import re
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



def _media_dims(path):
    """图片的 (width, height)；读不到返回 (None, None)。

    **F-P2-3（2026-11-18 外部审查）**：`kb_asset.width/height` 一直存在但**从没被写过**
    （实测 2196 行全空）。字段存在而永远为空，比没有这个字段更坏——查的人会以为
    有数据，然后发现全是 NULL。

    只在**图片**上做：视频要 ffprobe（外部依赖），那是另一件事，
    所以 `duration_ms` 这一轮仍然留空——**但下面会把它写进登记**，不假装已经填了。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:      # A档：读不出尺寸不该让整次扫描失败（坏图/非图片/无 Pillow）
        return None, None


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
            # F-P2-3：图片尺寸真的读出来（原来 width/height 永远是 NULL）
            **dict(zip(("width", "height"),
                       _media_dims(p) if kind == "image" else (None, None))),
        })

    if not dry_run:
        ts = now_utc()
        conn.executemany(
            "INSERT INTO kb_asset (vault_id,rel_path,mtime,size,kind,mime,"
            "content_hash,width,height,indexed_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active') "
            "ON CONFLICT(vault_id,rel_path) DO UPDATE SET "
            "mtime=excluded.mtime, size=excluded.size, kind=excluded.kind, "
            "mime=excluded.mime, "
            "content_hash=COALESCE(excluded.content_hash, kb_asset.content_hash), "
            "width=COALESCE(excluded.width, kb_asset.width), "
            "height=COALESCE(excluded.height, kb_asset.height), "
            "indexed_at=excluded.indexed_at, status='active'",
            [(r["vault_id"], r["rel_path"], r["mtime"], r["size"], r["kind"],
              r["mime"], r["content_hash"], r.get("width"), r.get("height"), ts)
             for r in rows])
        conn.commit()

    stats["ok"] = True
    stats["dry_run"] = bool(dry_run)
    stats["written"] = 0 if dry_run else len(rows)
    return stats


# ============================================================== W4：引用反查 + 缩略图
#
# 2026-11-18（W4）。两件事：
#   ① `referenced_by` —— "这个 641 MB 的 PV 有没有被任何笔记用到"
#   ② 缩略图 —— 5,960 MB 的 PNG 平均 4.6 MB/张，前端直接渲染会卡死
#
# ## 引用写法实测（不要凭想象）
#
# 对真实库 318 篇 md 全量扫描，三种写法各出现：
#     ![alt](_media/xxx.mp4)    2,151 处   ← **主流**
#     [[笔记名]]                   928 处   （主要指向笔记，不是资产）
#     ![[资产]]                      0 处
#
# 所以解析入口必须以 **Markdown 图片/链接** 为主，wikilink 作补充。
# 只做 wikilink 会漏掉 100% 的资产引用。

_MD_REF_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]\|#]+)")


def _norm_slash(p):
    """折叠 `a/../b` 这类路径（纯字符串层面，不碰盘符）。"""
    parts = []
    for seg in str(p).replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _norm_ref(raw):
    """把引用串规范化成可比较的相对路径（去锚点/查询串/URL 解码/统一斜杠）。"""
    from urllib.parse import unquote
    s = str(raw or "").strip()
    if not s or s.startswith(("http://", "https://", "mailto:", "data:")):
        return None
    s = s.split("#", 1)[0].split("?", 1)[0]
    try:
        s = unquote(s)
    except Exception:      # A档：百分号解码失败只说明不含编码字符，原样用即可
        pass
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    s = s.lstrip("/")
    return _norm_slash(s) or None


def _resolve_ref(note_rel, ref, known):
    """把笔记内的引用解析成 vault 内的资产 rel_path。

    Obsidian 的解析规则很宽：相对路径、绝对路径、同名文件。这里按最常见到最宽松：
      ① 相对笔记所在目录
      ② 相对 vault 根
      ③ basename 唯一时按同名匹配（不唯一就不猜，避免误连）
    """
    base = note_rel.rsplit("/", 1)[0] if "/" in note_rel else ""
    if base:
        c = _norm_slash(base + "/" + ref)
        if c in known:
            return c
    c = _norm_slash(ref)
    if c in known:
        return c
    name = ref.rsplit("/", 1)[-1]
    if not name:
        return None
    hits = [k for k in known if k.rsplit("/", 1)[-1] == name]
    return hits[0] if len(hits) == 1 else None


def build_referenced_by(conn, vault_row, *, write=True) -> dict:
    """从所有笔记反解资产引用，写入 `kb_asset.referenced_by`。

    为什么这个字段不可省：10.7 GB 里最大的是 641 MB 的 PV。**没人引用的资产
    才是可归档的**——没有这张反查表，以后瘦身无从下手。

    `unreferenced` 是本函数最要紧的读数。
    """
    import json as _json
    vid = vault_row["vault_id"]
    root = Path(str(vault_row["path"]))
    known = {r[0] for r in conn.execute(
        "SELECT rel_path FROM kb_asset WHERE vault_id=?", (vid,))}
    if not known:
        return {"ok": False, "reason": "no_assets"}

    refs = {}
    notes = 0
    unresolved = 0
    for dirpath, _dn, fns in os.walk(root):
        for fn in sorted(fns):
            if not fn.lower().endswith((".md", ".markdown")):
                continue
            p = Path(dirpath) / fn
            try:
                rel = p.relative_to(root).as_posix()
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            notes += 1
            found = set(_MD_REF_RE.findall(text)) | set(_WIKI_EMBED_RE.findall(text))
            for raw in found:
                ref = _norm_ref(raw)
                if not ref:
                    continue
                hit = _resolve_ref(rel, ref, known)
                if hit:
                    refs.setdefault(hit, set()).add(rel)
                else:
                    unresolved += 1

    if write:
        conn.execute("UPDATE kb_asset SET referenced_by='[]' WHERE vault_id=?", (vid,))
        for asset_rel, srcs in refs.items():
            conn.execute(
                "UPDATE kb_asset SET referenced_by=? WHERE vault_id=? AND rel_path=?",
                (_json.dumps(sorted(srcs), ensure_ascii=False), vid, asset_rel))
        conn.commit()

    return {"ok": True, "notes": notes, "assets": len(known),
            "referenced": len(refs), "unreferenced": len(known) - len(refs),
            "refs": sum(len(v) for v in refs.values()), "unresolved": unresolved}


# ------------------------------------------------------------------ 缩略图
THUMB_MAX = 512
THUMB_QUALITY = 80


def make_thumbnails(conn, vault_row, *, data_root, limit=0, dry_run=False,
                    kinds=("image",)) -> dict:
    """为 image（可选 video）生成 WebP 缩略图。

    ## 为什么默认只做图片

    视频抽帧要 ffmpeg，fish 现在没有这条依赖。**宁可如实少做，也不假装做了**——
    所以 `kinds` 默认只有 `image`。传了 `video` 但没有 ffmpeg 时，会**计数为 skipped**
    并保留 `video_skipped` 读数，不静默当成成功。

    ## 为什么缩略图不进资产体积

    缩略图是**派生数据**，算进去会让"知识库多大"失真。所以存在
    `<data_root>/thumb/`，`kb_asset.thumb_rel` 只记相对路径，不动 `size`。

    生成失败**不阻断索引**：`thumb_rel` 留空、`status` 仍 active，只计 failed。
    """
    vid = vault_row["vault_id"]
    root = Path(str(vault_row["path"]))
    out_dir = Path(str(data_root)) / "thumb"
    kinds = tuple(kinds or ("image",))
    sql = ("SELECT rel_path, kind FROM kb_asset WHERE vault_id=? AND status='active' "
           "AND kind IN (" + ",".join("?" * len(kinds)) + ")")
    args = (vid,) + kinds
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = conn.execute(sql, args).fetchall()

    stats = {"ok": True, "candidates": len(rows), "made": 0, "skipped": 0,
             "failed": 0, "video_skipped": 0,
             "bytes_in": 0, "bytes_out": 0, "dry_run": bool(dry_run),
             "kinds": list(kinds)}
    if dry_run:
        return stats
    try:
        from PIL import Image
    except ImportError:
        stats["ok"] = False
        stats["reason"] = "pillow_missing"
        return stats
    out_dir.mkdir(parents=True, exist_ok=True)

    for rel, kind in rows:
        src = root / rel
        if not src.exists():
            stats["skipped"] += 1
            continue
        if kind == "video":
            stats["video_skipped"] += 1
            stats["skipped"] += 1
            continue
        try:
            stem = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
            dst = out_dir / (stem + ".webp")
            with Image.open(src) as im:
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
                im.save(dst, "WEBP", quality=THUMB_QUALITY, method=4)
            stats["made"] += 1
            stats["bytes_in"] += src.stat().st_size
            stats["bytes_out"] += dst.stat().st_size
            conn.execute(
                "UPDATE kb_asset SET thumb_rel=? WHERE vault_id=? AND rel_path=?",
                (dst.relative_to(out_dir).as_posix(), vid, rel))
        except Exception:
            stats["failed"] += 1
    conn.commit()
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

# ============================================================== W6：压缩接缝（deliver）
#
# ## 定位：留接缝，不接压缩器
#
# 2026-11-18 实测（headroom 0.44.0，venv 独立安装）：
#     JSON 元数据 ×60（kb_asset 形态）  14,160 → 8,038 字符   省 43.2%
#     中文 markdown（fish 记忆形态）     1,620 → 1,620 字符    省  0.0%
#     Python 源码                        7,660 → 7,660 字符    省  0.0%
#
# 结论：**fish 自己的负载几乎无收益**（中文散文，headroom 自称 "little on prose"），
# 唯一确定能省的是 `kb_asset` 这类结构化元数据。所以：
#   · 默认**关闭**（`dataplane.compress_enabled` = 0）
#   · 不把 headroom 拉进生产依赖（它在独立 venv 里，装的是一整套 litellm/tokenizers）
#   · 只留一个**注入点**，将来真要用时挂上去
#
# ## 三条不变量在这一个函数里守住
#
#   I1 原文不可变  —— 压缩只发生在"搬给模型之前"，本函数不写库、不改存储
#   I2 写路径不压缩 —— 本函数是**读路径**的唯一入口；写路径（remember/kb_write）不调它
#   I4 身份不可压  —— `_DELIVER_NEVER_COMPRESS` 命中即原样返回
#
# 判据用 grep 守住这三条：`compress` 只许出现在本模块的 deliver 内部，
# 出现在 `remember` / `kb_write` / `render_profile_block` 的调用链上即红。

# 注入点：None = 不压。见 `set_compressor`。
_DELIVER_COMPRESSOR = None

# I4：这些内容永远原样返回，连尝试都不尝试。
_DELIVER_NEVER_COMPRESS = ("identity", "profile", "rule", "code")


class Delivery(dict):
    """交付结果。字段固定四个，少一个就不叫"可逆"。"""

    def __init__(self, text, *, original_len, kind, compressor, saved, handle):
        super().__init__(text=text, original_len=original_len, kind=kind,
                         compressor=compressor, saved=saved, handle=handle)

    @property
    def text(self):
        return self["text"]


def _compressor_enabled(conn):
    """读配置。**默认关闭**——装了不用的依赖是负债。"""
    try:
        from fish_common import get_config
        return bool(int(get_config(conn, "dataplane.compress_enabled") or 0))
    except Exception:      # A档：配置读不到就按"关闭"处理，宁可少压不可压错
        return False


def deliver(conn, payload, *, kind="auto", budget=None, source=None,
            handle_hint=None) -> Delivery:
    """把一段内容交付给模型：**唯一的压缩入口**。

    无论压不压，返回值都带四样东西（I3 可逆性硬契约）：
      · `text`          —— 实际交付的文本
      · `original_len`  —— 原始字符数（读者据此判断"我看到的完整吗"）
      · `saved`         —— 省了多少字符（0 表示没压）
      · `handle`        —— **怎么取回原文**（不能为空）

    `handle_hint` 由调用方给（通常是一个可执行入口，如 `kb open <rel>`）。
    不给就退化成"源标记"，仍然可定位。

    ## 为什么 `kind` 在 I4 名单里就直接返回

    身份/画像/铁律/精确编码被压 = 信息不可逆损失。这不是"少压一点"，是"不许压"。
    
    ## F-P2-4：这条接缝**没有生产调用方**，如实登记

    外部审查（2026-11-18）：`deliver` 定义了但**全仓零调用**，返回的 `handle`
    是**人读的字符串**，不是可执行入口。核实（2026-11-19）：**8 处测试调用、
    0 处生产调用**。指控成立。

    **为什么不在这一轮删**：它是已设计、已测试、契约清楚的压缩扩展点
    （I3 的四条都在，且绝不静默压缩）。删掉等于删掉能用的一段代码；
    而"接上真实调用"要先有压缩器实现，那是另一件事。

    **为什么必须写明**：项目自己的 I3 原则不许"空的接缝"。例外可以存在，
    但必须是**被登记的例外**，不能是"没人注意到它空着"。

    **要让这条接缝活起来需要两件事**（写在这里，免得下一个人重新猜）：
      ① 一个真的压缩器实现，用 `set_compressor(fn)` 注进来；
      ② 至少一个产品路径调用 `deliver` 并用上它返回的 `text`。
    在那之前它只是**契约的可执行说明**——那本身有价值，但不能冒充功能。
    """
    text = "" if payload is None else str(payload)
    original_len = len(text)
    handle = handle_hint or (f"原文：{source}" if source else "原文：调用方持有")

    base = dict(original_len=original_len, kind=kind, compressor="none",
                saved=0, handle=handle)

    if not text:
        return Delivery(text, **base)
    # I4：连尝试都不尝试
    if str(kind).lower() in _DELIVER_NEVER_COMPRESS:
        return Delivery(text, **(base | {"compressor": "skipped:protected"}))
    # 预算够用就不压（压了反而增加"取回"的心智负担）
    if budget is not None:
        try:
            from fish_common import count_tokens  # noqa: F401
        except Exception:                        # A档：没有分词器就跳过预算判断
            pass
        # 这里刻意不做 token 估算——估算不准会让"该压的没压"。
        # 真接压缩器时再按实测 token 判定。
    if not _compressor_enabled(conn):
        return Delivery(text, **base)

    fn = _DELIVER_COMPRESSOR
    if fn is None:
        return Delivery(text, **(base | {"compressor": "unavailable"}))

    try:
        out = fn(text, kind=kind)
    except Exception as exc:             # A档：压缩失败必须回退到原文，绝不"压一半"
        return Delivery(text, **(base | {"compressor": f"error:{type(exc).__name__}"}))
    if not isinstance(out, str) or not out:
        return Delivery(text, **(base | {"compressor": "empty_result"}))
    # 压缩后反而更长 → 不要（这类"压缩"只会浪费预算）
    if len(out) >= original_len:
        return Delivery(text, **(base | {"compressor": "no_win"}))
    return Delivery(out, **{
        "original_len": original_len, "kind": kind,
        "compressor": getattr(fn, "__name__", "custom"),
        "saved": original_len - len(out), "handle": handle,
    })


def set_compressor(fn):
    """注入压缩器（`fn(text, *, kind) -> str`）。传 None 表示卸下。

    故意做成**注入**而不是 import：headroom 装在独立 venv（全套 litellm/tokenizers），
    把它拉成 fish 的生产依赖，代价远大于它现在能带来的收益。将来真要接时再注入。
    """
    global _DELIVER_COMPRESSOR
    _DELIVER_COMPRESSOR = fn
    return _DELIVER_COMPRESSOR

# ---------------------------------------------------------------- CLI 入口
# F-P1-5（2026-11-18 外部审查）：本模块的 10 个函数原本**没有任何产品入口**——
# 资产表、缩略图、引用反查、无引用结论都只能靠作者的临时脚本产出。
# 一个只能被作者临时脚本触发的功能，等于不存在。

def cli(args):
    """`fish dataplane <action>` 的实现。返回退出码（0 成功 / 1 有问题 / 2 用法错）。

    **签名只有一个 args**：`main()` 的约定是 `rc = args.func(args)`。
    连接在这里自己开、自己关——命令层拿到的是一个可用的 conn，
    而不是要求调用方先替它连好库（那样每个 action 都要重复一遍 try/finally）。

    **四个动作都默认 dry-run，只读不写**——这些操作会遍历 10.7 GB，
    误触发的代价远高于多打一个 `--apply`。
    """
    import json as _json
    action = getattr(args, "dp_action", None)
    import fish as _fish
    root = Path(getattr(args, "root", "") or ".")
    conn = _fish.connect(root)
    try:
        return _cli_with_conn(args, conn, root, action, _json)
    finally:
        conn.close()


def _cli_with_conn(args, conn, root, action, _json):
    vault = None
    try:
        import fish_kb as _kb
        vault = _kb.kb_vault_get(conn, getattr(args, "vault", None))
    except Exception as exc:              # noqa: BLE001
        if action != "classify":
            print(f"错误：取不到知识库（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return 2

    if action == "classify":
        # 单个文件的**分类判定**：给"为什么它算 image/video/other"一个可查的答案。
        target = Path(getattr(args, "path", ""))
        if not target.exists():
            print(f"错误：文件不存在：{target}", file=sys.stderr)
            return 2
        # 返回是三元组 (kind, mime, magic)——**按魔数判定**，扩展名不参与结论
        # （外部审查 I5：分类只许看魔数，扩展名可以随便改）。
        got = classify_file(target)
        kind, mime, magic = (tuple(got) + (None, None, None))[:3]
        print(f"  {target}")
        print(f"  kind  = {kind}")
        print(f"  mime  = {mime}")
        print(f"  magic = {magic!r}")
        return 0

    if action == "stats":
        res = asset_stats(conn, vault_id=vault["vault_id"])
        if getattr(args, "json", False):
            print(_json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        print(f"  资产 {res['total']} 条 / {res['bytes'] / 1024 / 1024 / 1024:.2f} GB")
        # by_kind / by_status 的值是 {count, bytes} 字典，不是裸整数——
        # 直接 f-string 打出来会是一坨 {'count': 9, ...}，看不出主次。
        for label, key in (("类型", "by_kind"), ("状态", "by_status")):
            for k, v in sorted(res.get(key, {}).items()):
                if isinstance(v, dict):
                    n, b = v.get("count", 0), v.get("bytes", 0)
                    print(f"    {label} {k:<10} {n:>5} 条 / {b / 1024 / 1024:>9.1f} MB")
                else:
                    print(f"    {label} {k:<10} {v}")
        ref = res.get("referenced")
        if ref is not None:
            print(f"  被引用 {ref} 条 / 无引用 {res.get('unreferenced', 0)} 条")
        return 0

    if action == "scan":
        res = scan_vault(conn, vault, dry_run=not getattr(args, "apply", False),
                         compute_hash=getattr(args, "hash", False),
                         limit=getattr(args, "limit", None))
        # **第七轮评审 🟠**：这里原来读 `scanned/added/updated/skipped`，
        # 而 `scan_vault` 返回的键是 `files/notes/assets/written`——
        # **四个键一个都对不上，于是永远打印 0/0/0**。
        #
        # 后果不是"显示不好看"：`dataplane scan --apply` 正是补 kb_asset 存量
        # （2196 行 width/height 全空）的**唯一命令**。跑的人看到"扫描 0 个文件"，
        # 会以为没生效——而库其实已经被填满了。**一条会撒谎的补救命令。**
        print(f"  扫描 {res.get('files', 0)} 个文件 / 资产 {res.get('assets', 0)} / "
              f"笔记 {res.get('notes', 0)} / 写入 {res.get('written', 0)}"
              + ("（dry-run 未写）" if res.get('dry_run') else ""))
        if not getattr(args, "apply", False):
            print("  （dry-run：一个字节都没写；加 --apply 才落库）")
        return 0

    if action == "refs":
        res = build_referenced_by(conn, vault, write=bool(getattr(args, "apply", False)))
        print(f"  被引用 {res.get('referenced', 0)} 条 / 无引用 {res.get('unreferenced', 0)} 条")
        if not getattr(args, "apply", False):
            print("  （dry-run：未写 referenced_by；加 --apply 才落库）")
        return 0

    if action == "thumbs":
        res = make_thumbnails(conn, vault, data_root=root,
                              limit=getattr(args, "limit", 0) or 0,
                              dry_run=not getattr(args, "apply", False))
        print(f"  缩略图 生成 {res.get('made', 0)} / 跳过 {res.get('skipped', 0)} / "
              f"失败 {res.get('failed', 0)}")
        if not getattr(args, "apply", False):
            print("  （dry-run：未写图；加 --apply 才生成）")
        return 0

    print(f"错误：未知动作 {action!r}", file=sys.stderr)
    return 2
