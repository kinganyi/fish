#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把正文文件名从「纯编码」改成「编码 + 标题缩写」（2026-10-25）。

    anyi-studio-261022-05.md
      → anyi-studio-261022-05__协作铁律·行为层（CR-2-5-6-7-11_+.md

设计（用户 2026-10-25 拍板）：
  · 标题截 **24 字符**  —— 人用自己的文件管理工具搜文件名时够用，又不会长到难读
  · 标题是**记录时的快照** —— `memory.revised` 改标题后**文件名不变**
    （跟着变会让外部引用全断：人写的文档、别的 AI 的笔记都记着旧文件名）
  · 路径统一由 `fish.body_relpath()` 算 —— 与 `record_memory` / `rebuild_projection`
    **同一函数**，否则重建即断链

用法：
    python rename_bodies_to_slug.py --dry-run
    python rename_bodies_to_slug.py --apply
    python rename_bodies_to_slug.py --dry-run --data-root <副本>
"""
import argparse
import os
import shutil
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P0)
import fish  # noqa: E402

DATA = os.path.join(P0, "data")


def exact_exists(path):
    """**区分大小写**的存在性检查。

    Windows 的 `os.path.exists` 不区分大小写，于是 `projects/CDQL/CDQL-261024-01.md`
    会"存在"——它其实匹配到了 `cdql-261024-01.md`。
    这个假象掩盖了一处真损坏：`CDQL-261024-01` 的正文从来没落在自己的文件名下，
    它看到的是 `cdql-261024-01` 的正文（2026-10-25 实测发现）。
    """
    d, f = os.path.split(path)
    if not os.path.isdir(d):
        return False
    return f in os.listdir(d)


def case_insensitive_twin(path):
    """若 `path` 精确不存在但存在**只差大小写**的同名文件，返回那个真实路径。"""
    d, f = os.path.split(path)
    if not os.path.isdir(d):
        return None
    low = f.lower()
    for real in os.listdir(d):
        if real != f and real.lower() == low:
            return os.path.join(d, real)
    return None


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
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT code, project, title, file_path FROM code_index ORDER BY code").fetchall()

    plan, missing, conflict, noop, twins = [], [], [], [], []
    for code, project, title, fp in rows:
        want = fish.body_relpath(project, code, title)
        if not fp:
            missing.append((code, fp, want))
            continue
        old_abs = os.path.join(DATA, fp)
        new_abs = os.path.join(DATA, want)
        if not exact_exists(old_abs):
            # 精确文件名不存在：可能是历史断链，也可能是**大小写双胞胎**
            twin = case_insensitive_twin(old_abs)
            if twin:
                twins.append((code, fp, want, os.path.relpath(twin, DATA)))
            else:
                missing.append((code, fp, want))
            continue
        if os.path.normpath(old_abs) == os.path.normpath(new_abs):
            noop.append(code)
            continue
        if exact_exists(new_abs):
            conflict.append((code, fp, want))
            continue
        plan.append((code, fp, want, old_abs, new_abs))

    print("总 %d 条：待改名 %d ｜ 已符合 %d ｜ 旧文件缺失 %d ｜ 目标已存在 %d ｜ 大小写双胞胎 %d"
          % (len(rows), len(plan), len(noop), len(missing), len(conflict), len(twins)))
    if twins:
        print("\n⚠️ 大小写双胞胎（`file_path` 精确不存在，但有只差大小写的同名文件）：")
        print("   这类条目**正文归属存疑**——Windows 不区分大小写，两条记忆可能共用了一个物理文件。")
        for c, fp, want, real in twins:
            print("  %-26s file_path=%r" % (c, fp))
            print("    ↳ 实际匹配到 %r" % real)
        print("   处理：**复制**真实文件给这条（内容可能就是它的），原文件保持不动。")
    if missing:
        print("\n旧文件缺失（不动，交给 healthcheck）：")
        for c, fp, want in missing[:8]:
            print("  %-26s file_path=%r" % (c, fp))
        if len(missing) > 8:
            print("  ... 共 %d 条" % len(missing))
    if conflict:
        print("\n目标文件名已存在（跳过，需人工判断）：")
        for c, fp, want in conflict[:8]:
            print("  %-26s %s" % (c, want))

    print("\n改名样例（前 6 条）：")
    for c, old, new, _oa, _na in plan[:6]:
        print("  %s" % os.path.basename(old))
        print("    → %s" % os.path.basename(new))

    if dry:
        print("\n(dry-run：未改动任何文件)")
        con.close()
        return 0

    # ---- 执行 ----
    # **顺序很重要**：先处理双胞胎（复制），再改名。
    # 反过来的话，`plan` 会把 `cdql-261024-01.md` 改走，双胞胎再去复制源文件就没了
    # （2026-10-25 副本演练踩到：打印说"复制 0 个"，因为它去复制一个已经被改名的文件）。
    done, copied = 0, 0
    for code, fp, want, real in twins:
        src = os.path.join(DATA, real)
        dst = os.path.join(DATA, want)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not exact_exists(dst) and os.path.exists(src):
            shutil.copy2(src, dst)
            con.execute("UPDATE code_index SET file_path=? WHERE code=?", (want, code))
            copied += 1
    for code, old, new, old_abs, new_abs in plan:
        os.makedirs(os.path.dirname(new_abs), exist_ok=True)
        os.rename(old_abs, new_abs)
        con.execute("UPDATE code_index SET file_path=? WHERE code=?", (new, code))
        done += 1
    con.commit()

    # FTS 重建（正文路径变了，索引里的 body_idx 要跟着重灌）
    n_fts = fish.reindex_fts(con, DATA)
    con.close()
    print("\n已改名 %d 个文件，双胞胎复制 %d 个，FTS 重建 %d 条" % (done, copied, n_fts))

    # ---- 自检 ----
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    broken = [c for c, fp in con.execute("SELECT code,file_path FROM code_index")
              if fp and not exact_exists(os.path.join(DATA, fp))]
    total = con.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
    con.close()
    print("自检：记忆 %d 条，断链 %d 条 %s" % (total, len(broken),
                                           "PASS" if not broken else "FAIL: " + str(broken[:3])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
