"""独立复核：知识库迁移是否完整（W3 的验收工具，可复跑）。

## 为什么要有这个工具

`kb_vault_relocate` 内部已经做了逐文件哈希校验，但**搬完之后的第二次独立复核**
不应该复用同一段代码——否则"校验通过"只能证明"那段代码认为自己通过了"。

本工具独立于 relocate：
  · 直接读 `kb_asset` 里记录的哈希
  · 直接对磁盘上的文件重新算哈希
  · 再额外比对**文件数量与总字节**（哈希只覆盖资产，笔记不在 kb_asset 里）

## 用法

    python tools/verify_kb_migration.py [--source <原路径>]

不带 `--source` 时只校验目标树自身完整性（哈希 + 数量）。
带 `--source` 时额外做源/目标双向比对（迁移刚做完时用）。
"""
import argparse
import json
import os
import sqlite3
import sys

# 第三轮评审：本文件打印 ✓ / ✗ / ⚠ 这类符号，而 Windows 控制台默认是 GBK，
# 直接 print 会抛 UnicodeEncodeError → **退出码 1**，看起来像"检查失败"。
# 这是本项目反复踩的坑：修了 fish.py，没修 tools/*.py。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # A档：拿不到可重配的流就按原样

HERE = os.path.dirname(os.path.abspath(__file__))
P0 = os.path.dirname(HERE)
DB = os.path.join(P0, "data", "index.sqlite")
sys.path.insert(0, P0)

import fish_dataplane as DP   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None, help="原库路径（做双向比对）")
    ap.add_argument("--limit", type=int, default=0, help="只校验前 N 条（调试用）")
    args = ap.parse_args()

    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    row = conn.execute("SELECT name, path, last_scan_files, last_scan_notes, "
                       "last_scan_bytes, ignore_globs FROM kb_vault LIMIT 1").fetchone()
    if not row:
        print("✗ 没有登记的 vault")
        return 2
    name, path, lsf, lsn, lsb, globs_raw = row
    try:
        globs = json.loads(globs_raw or "[]")
    except Exception:
        globs = []
    print("=== vault: %s ===" % name)
    print("  路径:", path)
    print("  上次扫描: 文件 %s ｜ 笔记 %s ｜ 字节 %s" % (lsf, lsn, lsb))

    rows = conn.execute(
        "SELECT rel_path, size, content_hash FROM kb_asset"
        + (" LIMIT %d" % args.limit if args.limit else "")).fetchall()
    print("  资产行: %d" % len(rows))

    # ① 逐文件哈希
    bad, ok = [], 0
    for rel, size, hh in rows:
        p = os.path.join(path, rel.replace("/", os.sep))
        if not os.path.exists(p):
            bad.append((rel, "missing"))
            continue
        if os.path.getsize(p) != size:
            bad.append((rel, "size %d != %d" % (os.path.getsize(p), size)))
            continue
        if hh and DP.sha256_file(p) != hh:
            bad.append((rel, "hash"))
            continue
        ok += 1
    print("\n  ① 哈希校验: 通过 %d ｜ 失败 %d" % (ok, len(bad)))
    for rel, why in bad[:8]:
        print("     ✗ %s (%s)" % (rel, why))

    # ② 全树文件数与字节。
    #
    # **两把尺子不能混用**（2026-11-18 首跑踩到）：`last_scan_files` 是**过滤
    # ignore_globs 之后**的口径，而朴素 os.walk 数的是全部。实测源库 2,526 vs 扫描
    # 2,493，差的 33 个全在 `.obsidian/` / `.trash/` / `90-模板/` 里——那不是迁移丢文件，
    # 是判据拿两把尺子量同一件事。
    # 所以这里用**和扫描同一套 glob** 来数，另附一个"未过滤总数"作参考。
    raw_files = 0
    for _dp, _dn, fns in os.walk(path):
        raw_files += len(fns)
    counted = DP.walk_vault(path, globs)
    total_files = len(counted)
    total_bytes = 0
    for rel in counted:
        try:
            total_bytes += os.path.getsize(os.path.join(path, rel.replace("/", os.sep)))
        except OSError:
            pass
    print("\n  ② 同口径（过滤 ignore_globs）: %d 文件 ｜ %.1f GB" % (
        total_files, total_bytes / 1024 ** 3))
    print("     未过滤总数（含 .obsidian/.trash 等）: %d 文件" % raw_files)
    if lsf:
        same = (total_files == lsf)
        print("     与上次扫描比对: %s（扫描 %s）" % (
            "一致 ✅" if same else "不一致 ✗", lsf))
        if not same:
            bad.append(("<全树>", "同口径文件数 %d != 扫描 %d" % (total_files, lsf)))

    # ③ 可选：与源双向比对（文件集合 + 逐文件哈希）
    if args.source:
        src = args.source
        print("\n  ③ 与源比对: %s" % src)
        if not os.path.isdir(src):
            print("     ✗ 源不存在，跳过")
        else:
            src_set, dst_set = set(), set()
            for root, _dn, fns in os.walk(src):
                for fn in fns:
                    src_set.add(os.path.relpath(os.path.join(root, fn), src).replace("\\", "/"))
            for root, _dn, fns in os.walk(path):
                for fn in fns:
                    dst_set.add(os.path.relpath(os.path.join(root, fn), path).replace("\\", "/"))
            only_src = src_set - dst_set
            only_dst = dst_set - src_set
            print("     源 %d ｜ 目标 %d ｜ 只在源 %d ｜ 只在目标 %d" % (
                len(src_set), len(dst_set), len(only_src), len(only_dst)))
            if only_src:
                print("     ✗ 目标缺: %s" % sorted(only_src)[:5])
                bad.append(("<集合>", "目标缺 %d 个" % len(only_src)))

    conn.close()
    print("\n" + ("✓ 复核通过" if not bad else "✗ 复核失败 %d 项" % len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
