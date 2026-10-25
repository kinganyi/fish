#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为每个项目目录生成 `INDEX.md`（记忆一览）。

## 为什么还需要它（文件名已经带标题了）

用户 2026-10-25 拍板：**文件名里的标题是"记录时的快照"**——`memory.revised` 改了标题，
文件名**不变**（跟着变会让所有外部引用失效：人写的文档、别的 AI 的笔记都记着旧名字）。

于是需要一个"永远最新"的视图，这就是 INDEX.md：
  · 标题变了 → INDEX 更新，文件名不动
  · 想看某条是什么 → 打开 INDEX，比逐个文件看快得多
  · 每条都带 `read_body <编码>` 的提示，避免"翻目录代替调工具"（绕路）

用法：
    python gen_index_md.py            # 全部项目
    python gen_index_md.py --project anyi-studio
    python gen_index_md.py --check    # 只检查是否过期（不进 CI，供人工跑）
"""
import argparse
import io
import os
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P0)
import fish  # noqa: E402

DATA = os.path.join(P0, "data")


def render(conn, project, items):
    act = sum(1 for r in items if r[3] == "active")
    lines = [
        f"# {project} · 记忆索引",
        "",
        f"> 共 {len(items)} 条（active {act}）｜ 本文件由 `gen_index_md.py` 生成，可随时重建。",
        ">",
        "> **查正文用 `read_body <编码>`**——直接读文件会绕过衰减/访问记录/关联维护。",
        ">",
        "> 文件名里的标题是**记录时的快照**（改标题不会改文件名）；本表才是最新标题。",
        "",
        "| 编码 | 类型 | 状态 | 记录时间 | 标题 |",
        "|---|---|---|---|---|",
    ]
    for code, title, mem_type, status, rec in items:
        t = (title or "").replace("|", "\\|")
        stamp = (rec or "")[:10]
        mark = "active" if status == "active" else f"**{status}**"
        lines.append(f"| `{code}` | {mem_type} | {mark} | {stamp} | {t} |")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    ap.add_argument("--check", action="store_true", help="只报告哪些 INDEX 过期，不写")
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()

    global DATA
    if a.data_root:
        DATA = os.path.abspath(a.data_root)
        print("⚠️  使用外部数据目录：%s" % DATA)

    db = os.path.join(DATA, "index.sqlite")
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    q = ("SELECT project, code, title, mem_type, status, recorded_at FROM code_index "
         "WHERE project IS NOT NULL AND project <> ''")
    par = ()
    if a.project:
        q += " AND project=?"
        par = (a.project,)
    q += " ORDER BY project, code"

    by_proj = {}
    for proj, code, title, mt, st, rec in con.execute(q, par):
        by_proj.setdefault(proj, []).append((code, title, mt, st, rec))

    # **按别名合并**：`cdql` 与 `CDQL` 是同一个项目（`name_aliases` 里登记着），
    # 而 Windows 大小写不敏感 → 两者写的是同一个 `INDEX.md`，不合并就会互相覆盖
    # （2026-10-25 实测：`--check` 永远报 CDQL 过期，因为它每次都被 cdql 覆盖）。
    merged = {}
    for proj, items in by_proj.items():
        names = fish.resolve_project_names(con, proj)
        canon = names[0] if names else proj
        merged.setdefault(canon, []).extend(items)
    for canon in merged:
        merged[canon].sort(key=lambda r: r[0])      # 稳定排序，保证幂等
    con.close()
    by_proj = merged

    written, stale = 0, []
    for proj, items in sorted(by_proj.items()):
        d = os.path.join(DATA, "projects", proj)
        if not os.path.isdir(d):
            continue          # 已归档项目（_retired-*）不生成
        text = render(None, proj, items)
        p = os.path.join(d, "INDEX.md")
        old = io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        if text != old:
            stale.append(proj)
            if not a.check:
                io.open(p, "w", encoding="utf-8").write(text)
                written += 1

    if a.check:
        print("过期 %d 个项目的 INDEX%s" % (len(stale), ("：" + "、".join(stale)) if stale else "（全部最新）"))
    else:
        print("生成 %d 个项目的 INDEX.md（共 %d 个项目）" % (written, len(by_proj)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
