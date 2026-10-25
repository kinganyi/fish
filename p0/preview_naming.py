#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可发现性方案预览（2026-10-25）——**只生成样例，不改任何真实数据**

给用户看两件事：
  方案 1：只生成 `INDEX.md`（文件名完全不动）
  方案 2：文件名加标题缩写 + INDEX.md（需要改文件名）

用法：
    python preview_naming.py            # 打印两种效果（不写盘）
    python preview_naming.py --write    # 真的写到 %TEMP% 下的预览目录
"""
import io
import os
import re
import sqlite3
import sys
import tempfile

P0 = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(P0, "data", "index.sqlite")

# Windows 非法字符 → 安全替换。用全角或短横，保持可读。
_ILLEGAL = str.maketrans({
    "/": "-", "\\": "-", ":": "-", "*": "-", "?": "-",
    '"': "", "|": "-", "<": "(", ">": ")", "\n": " ", "\r": " ",
    " ": "_",
})
_SLUG_MAX = 24          # 用户说的"标题缩写"→ 取前 24 字符（留余量给编码与目录）


def slug(title, limit=_SLUG_MAX):
    """标题 → 文件名安全片段（缩写）。

    为什么要 `maketrans` 逐字替换而不是正则：Windows 的非法字符是**定长集合**，
    逐字替换能保留原有语义顺序（`A / B` → `A_-_B` 比 `A_B` 更能看出原文有分隔）。
    """
    t = (title or "").strip()
    t = re.sub(r"\s+", " ", t)
    t = t.translate(_ILLEGAL)
    t = t.strip("._-")
    return t[:limit] or "untitled"


def safe_name(title, limit=_SLUG_MAX):
    return slug(title, limit)


def load_rows():
    con = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    rows = con.execute(
        "SELECT code, project, title, mem_type, status, recorded_at FROM code_index "
        "ORDER BY project, code").fetchall()
    con.close()
    return rows


def render_index(project, items):
    """INDEX.md 内容（方案 1 与方案 2 共用）。"""
    act = sum(1 for r in items if r[4] == "active")
    lines = [
        f"# {project} · 记忆索引",
        "",
        f"> {len(items)} 条（active {act}）。本文件由 `fish index-md` 生成，可随时重建。",
        "> **查正文用 `read_body <编码>`**——直接读文件会绕过衰减/访问记录/关联维护。",
        "",
        "| 编码 | 类型 | 状态 | 记录时间 | 标题 |",
        "|---|---|---|---|---|",
    ]
    for code, _proj, title, mt, st, rec in items:
        t = (title or "").replace("|", "\\|")
        stamp = (rec or "")[:10]
        mark = "" if st == "active" else f"**{st}**"
        lines.append(f"| `{code}` | {mt} | {mark or 'active'} | {stamp} | {t} |")
    lines.append("")
    return "\n".join(lines)


def main():
    write = "--write" in sys.argv
    rows = load_rows()
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r[1] or "(无项目)", []).append(r)

    out = []
    out.append("=" * 78)
    out.append("预览：anyi-studio 项目（真实数据，共 %d 条）" % len(by_proj.get("anyi-studio", [])))
    out.append("=" * 78)
    items = by_proj.get("anyi-studio", [])
    sample = items[:12]

    out.append("")
    out.append("── 方案 1：只加 INDEX.md（文件名完全不动） ──")
    out.append("")
    out.append("目录长这样：")
    out.append("  projects/anyi-studio/")
    out.append("  ├── INDEX.md                    ← 新增")
    for r in sample[:9]:
        out.append("  ├── %s.md" % r[0])
    out.append("  └── ...（共 %d 个）" % len(items))
    out.append("")
    out.append("INDEX.md 的内容（前 10 行）：")
    idx = render_index("anyi-studio", items)
    for ln in idx.split("\n")[:12]:
        out.append("  " + ln)
    out.append("")

    out.append("── 方案 2：文件名加标题缩写 + INDEX.md ──")
    out.append("")
    out.append("目录长这样（同一个项目，文件名变了）：")
    out.append("  projects/anyi-studio/")
    out.append("  ├── INDEX.md")
    for r in sample[:9]:
        out.append("  ├── %s__%s.md" % (r[0], safe_name(r[2])))
    out.append("  └── ...（共 %d 个）" % len(items))
    out.append("")
    out.append("同一个目录，两种方案的文件名对比（前 8 条）：")
    out.append("  %-34s | %s" % ("方案 1（编码）", "方案 2（编码+标题缩写）"))
    out.append("  " + "-" * 100)
    for r in sample[:8]:
        out.append("  %-34s | %s__%s.md" % (r[0] + ".md", r[0], safe_name(r[2])))
    out.append("")

    # 非法字符处理效果
    bad = [r for r in rows if any(c in (r[2] or "") for c in '<>:"/\\|?*')]
    out.append("── 非法字符处理效果（%d 条标题含非法字符） ──" % len(bad))
    for r in bad[:6]:
        out.append("  原标题: %s" % (r[2] or ""))
        out.append("  → 文件名片段: %s" % safe_name(r[2]))
        out.append("")

    # 长度统计
    names2 = [safe_name(r[2]) for r in rows]
    over = [n for n in names2 if len(n) > _SLUG_MAX]
    out.append("── 长度检查 ──")
    out.append("  缩写后最长: %d 字符（上限 %d）" % (max(len(n) for n in names2), _SLUG_MAX))
    out.append("  超限的: %d 条" % len(over))
    longest_path = max(len("projects/%s/%s__%s.md" % (r[1] or "x", r[0], safe_name(r[2]))) for r in rows)
    out.append("  最长完整相对路径: %d 字符（Windows 传统上限 260）" % longest_path)
    out.append("")

    if write:
        base = os.path.join(tempfile.gettempdir(), "fish_naming_preview")
        out.append("── 写盘位置 ──")
        for mode in ("plan1", "plan2"):
            d = os.path.join(base, mode, "anyi-studio")
            os.makedirs(d, exist_ok=True)
            io.open(os.path.join(d, "INDEX.md"), "w", encoding="utf-8").write(
                render_index("anyi-studio", items))
            for r in items[:20]:
                fn = (r[0] + ".md") if mode == "plan1" else (f"{r[0]}__{safe_name(r[2])}.md")
                io.open(os.path.join(d, fn), "w", encoding="utf-8").write(
                    "# %s\n\n（预览占位，真实正文未复制）\n" % (r[2] or ""))
            out.append("  %s" % d)

    print("\n".join(out))


if __name__ == "__main__":
    main()
