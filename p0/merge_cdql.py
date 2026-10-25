#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `cdql` 真正合并进 `CDQL`（2026-10-25）。

## 为什么必须做（不只是建别名）

Windows 文件系统大小写不敏感 → `projects/cdql/` 与 `projects/CDQL/` 是**同一个目录**，
`cdql-261024-01.md` 与 `CDQL-261024-01.md` 是**同一个文件**。

后果已经发生了一次：`projects/CDQL/cdql-261024-01.md` 里装的是 **CDQL-261024-01** 的正文，
而 `cdql-261024-01`（「审查A-Day1：部署链路三条修正」）自己的正文**被覆盖丢失**，
且**无法从事件恢复**（`memory.recorded` 的 payload 不存正文）。

**只要两个项目名只差大小写，这个事故随时会再来一次。**

## 怎么合

1. 9 条 `cdql-*` 迁到 `CDQL-*`；`CDQL-261024-01` 已占用，所以从 `-02` 起顺延
2. 正文文件改名 + 内容里的编码同步替换
3. **唯一一条正文错位的**（`cdql-261024-01`）写占位说明，如实标注正文丢失
4. `cdql` 前缀 → **retired**（不再接收新写入；AD
   -003：编码不回收）
5. 追加 `code.renamed.batch` 事件（rebuild 才能还原）
6. 别名 `cdql → CDQL` **保留**（旧代码里的 `project=cdql` 仍能检索）

用法：
    python merge_cdql.py --dry-run
    python merge_cdql.py --apply
    python merge_cdql.py --apply --data-root <副本>
"""
import argparse
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P0)
import fish  # noqa: E402

DATA = os.path.join(P0, "data")
CANON = "CDQL"
OLD = "cdql"

# 正文已确认错位的那条（正文其实是 CDQL-261024-01 的副本，自己的丢了）
BODY_LOST = {"cdql-261024-01"}

PLACEHOLDER = """# {title}

> 编码：{code} ｜ 类型：{mem_type} ｜ 记录于 {recorded}

⚠️ **正文已丢失**（历史事故，2026-10-25 查清）

## 发生了什么

2026-10-24 之前，`cdql` 与 `CDQL` 两个项目的正文路径**只差大小写**
（`projects/cdql/cdql-261024-01.md` vs `projects/CDQL/CDQL-261024-01.md`），
而 Windows 文件系统大小写不敏感 → 它们是**同一个文件**，后写的覆盖了先写的。

这条记忆原本的标题是「{title}」，正文被 `CDQL-261024-01`（「项目整体送审报告交付」）覆盖。

## 为什么找不回来

`memory.recorded` 事件的 payload **不存正文**（只存 `body_ref` 路径），
所以正文无法从事件重建。要找原始内容，只能去
`anyi-studio-AI工作空间/artifacts/` 下检索当时的审查文档。

## 以后怎么避免

`cdql` 项目已于 2026-10-25 合并进 `CDQL`，`cdql` 前缀 retired。
**同一个项目不许再用两个只差大小写的名字。**
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    global DATA
    if a.data_root:
        DATA = os.path.abspath(a.data_root)
        print("⚠️  使用外部数据目录：%s" % DATA)
    dry = not a.apply
    if not a.apply and not a.dry_run:
        print("必须指定 --dry-run 或 --apply")
        return 1

    db = os.path.join(DATA, "index.sqlite")
    projects_dir = os.path.join(DATA, "projects")
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=OFF")

    # ---- 1. 算映射：cdql-* → CDQL-*（撞号顺延） ----
    rows = con.execute(
        "SELECT code, title, mem_type, recorded_at, file_path FROM code_index "
        "WHERE project=? ORDER BY code", (OLD,)).fetchall()
    if not rows:
        print("没有需要合并的 cdql 记忆")
        con.close()
        return 0

    existing = {r[0] for r in con.execute("SELECT code FROM code_index")}
    mapping = {}
    used = set(existing)
    for code, title, mt, rec, fp in rows:
        parts = code.rsplit("-", 2)
        date_part = parts[1]
        seq = int(parts[2]) + 1          # 从 -02 起（-01 已被 CDQL-261024-01 占用）
        while f"{CANON}-{date_part}-{seq:02d}" in used:
            seq += 1
        new_code = f"{CANON}-{date_part}-{seq:02d}"
        used.add(new_code)
        mapping[code] = new_code

    print("合并计划（%d 条 %s → %s）：" % (len(mapping), OLD, CANON))
    for code, title, mt, rec, fp in rows:
        flag = "  ⚠️ 正文丢失（写占位）" if code in BODY_LOST else ""
        print("  %-18s → %-20s %s%s" % (code, mapping[code], (title or "")[:30], flag))

    if dry:
        print("\n(dry-run：未改动)")
        con.close()
        return 0

    # ---- 2. 数据迁移 ----
    moved, placeholders = 0, 0
    for code, title, mt, rec, fp in rows:
        new_code = mapping[code]
        new_fp = fish.body_relpath(CANON, new_code, title)
        con.execute("UPDATE code_index SET code=?, project=?, file_path=? WHERE code=?",
                    (new_code, CANON, new_fp, code))
        con.execute("UPDATE links SET src=? WHERE src=?", (new_code, code))
        con.execute("UPDATE links SET dst=? WHERE dst=?", (new_code, code))
        con.execute("UPDATE memory_access_log SET code=? WHERE code=?", (new_code, code))
        con.execute("UPDATE push_log SET code=? WHERE code=?", (new_code, code))

        # ---- 3. 正文 ----
        src = os.path.join(DATA, fp) if fp else ""
        dst = os.path.join(DATA, new_fp)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if code in BODY_LOST:
            io.open(dst, "w", encoding="utf-8").write(PLACEHOLDER.format(
                title=title or "", code=new_code, mem_type=mt or "fact",
                recorded=rec or ""))
            placeholders += 1
            # 旧的错位文件（其实是 CDQL-261024-01 的副本）删掉，避免混淆
            if src and os.path.exists(src):
                os.remove(src)
        elif src and os.path.exists(src):
            txt = io.open(src, encoding="utf-8").read()
            txt = txt.replace(code, new_code)        # 正文头部的「编码：xxx」同步
            io.open(dst, "w", encoding="utf-8").write(txt)
            if os.path.abspath(src) != os.path.abspath(dst):
                os.remove(src)
        moved += 1
    con.commit()

    # ---- 4. cdql 前缀 retire ----
    n = con.execute("UPDATE code_registry SET status='retired' WHERE prefix=?",
                    (OLD,)).rowcount
    print("\n  cdql 前缀 → retired（影响 %d 行）" % n)

    # ---- 5. 追加映射事件（rebuild 靠它还原） ----
    import hashlib
    h = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False)
                       .encode("utf-8")).hexdigest()
    eid = "EVT-CODE-RENAMED-MERGE-" + h[:16]
    if not con.execute("SELECT 1 FROM events WHERE event_id=?", (eid,)).fetchone():
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, seq, ts, "code.renamed.batch",
             '{"citizen_id": "fish-主AI-2", "evidence_level": "signed"}',
             '{"kind": "project", "id": "cdql"}',
             json.dumps({"mapping": mapping, "count": len(mapping),
                         "note": "cdql 合并进 CDQL（Windows 大小写不敏感导致正文互相覆盖）"},
                        ensure_ascii=False),
             "2026-10-25 合并只差大小写的两个项目", "evt@1"))
        print("  已追加映射事件 %s（%d 条）" % (eid, len(mapping)))
    con.commit()

    # ---- 6. FTS ----
    n_fts = fish.reindex_fts(con, DATA)
    print("  FTS 重建 %d 条" % n_fts)
    print("\n已迁移 %d 条（其中正文占位 %d 条）" % (moved, placeholders))

    # ---- 7. 校验 ----
    print("\n校验：")
    left = con.execute("SELECT COUNT(*) FROM code_index WHERE project=?", (OLD,)).fetchone()[0]
    print("  project='cdql' 残留: %d %s" % (left, "PASS" if left == 0 else "FAIL"))
    total = con.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    print("  code_index 总数: %d（合并前 216）" % total)
    broken = []
    for code, fp in con.execute("SELECT code, file_path FROM code_index"):
        if not fp:
            continue
        d, f = os.path.split(os.path.join(DATA, fp))
        if not (os.path.isdir(d) and f in os.listdir(d)):
            broken.append(code)
    print("  精确断链: %d %s" % (len(broken), "PASS" if not broken else "FAIL " + str(broken[:3])))
    # 单目录检查：projects 下不应该再有 cdql 目录（大小写不同会重复列出）
    dirs = [d for d in os.listdir(projects_dir) if d.lower() == "cdql"]
    print("  projects 下 cdql/CDQL 目录数: %d %s（Windows 上应只有 1 个）"
          % (len(dirs), "PASS" if len(dirs) <= 1 else "FAIL"))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
