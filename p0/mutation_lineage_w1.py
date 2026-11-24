#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1（`session_lineage` / `session_identity` 骨架）判据的变异自证（113 §2.6，2026-11-25）。

## 为什么 W1 也要变异

W1 的全部承诺是"**什么都没变**"（0 行、旧读数不变、migrate 幂等）。这类"没有变化"
的判据最容易变成恒绿：表名写错、库连错、断言写空，全是绿的。所以每条判据都要先在
坏输入下红一次，再在还原后复绿——**没有对照组红的变异不算证据**。

## 七个变异体

  D0（数据级，不是源码变异）
      绕过 `upsert_node`、绕过任何开关，直接 `INSERT` 一行到**临时库**
      → 同一个 `assert_lineage_empty` 必须红；`DELETE` 后必须复绿。
      （113 §2.6 J-W1-1 的变异要求：证明"0 行"读数不是假的）

  M1  `_ensure_lineage_tables` 顺手写一行         → J-W1-1b 红（migrate 写了行）
  M2  摘掉 `CHECK (origin <> 'main' OR parent_session IS NULL)` → J-W1-3 红
  M3  去掉 DDL 的 `IF NOT EXISTS`                 → J-W1-4 红（第二次 table already exists）
  M4  注释掉 `migrate()` 里那句 `_ensure_lineage_tables(conn)` → J-W1-5 红（旧库补不出表）
  M5  SCHEMA 拼装链摘掉 `+ _LINEAGE_SCHEMA`        → J-W1-1 红（新库根本没有表）
  M6  摘掉 `_ensure_lineage_tables` 的索引循环      → J-W1-5 红（旧库升级缺索引）

用法：`python -X utf8 mutation_lineage_w1.py [--only M3] [--data-only] [--src-only]`
"""
import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
SCHEMA_PY = HERE / "fish_schema.py"
DB_PY = HERE / "fish_db.py"
FISH_PY = HERE / "fish.py"
TEST = "test_lineage_schema.py"

_NOW = "2026-11-25T00:00:00Z"
_LEAK_INSERT = (
    "    conn.execute(\"INSERT INTO session_lineage (host, session_id, origin, basis, "
    "source, observed_at) VALUES ('dsh','leak','unknown','missing','dsh:mut','"
    + _NOW + "')\")\n")

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", DB_PY, "j_w1_1",
     "    for ddl in (_SESSION_LINEAGE_DDL, _SESSION_IDENTITY_DDL):\n"
     "        conn.execute(ddl)                  # IF NOT EXISTS：已有的表原样不动\n",
     "    for ddl in (_SESSION_LINEAGE_DDL, _SESSION_IDENTITY_DDL):\n"
     "        conn.execute(ddl)\n" + _LEAK_INSERT,
     "J-W1-1b：migrate 顺手写了一行"),
    ("M2", SCHEMA_PY, "j_w1_3",
     "  CHECK (origin <> 'main' OR parent_session IS NULL)\n",
     "  CHECK (1)\n",
     "J-W1-3：主+有父 不再被拒"),
    ("M3", SCHEMA_PY, "j_w1_4",
     "CREATE TABLE IF NOT EXISTS session_identity (",
     "CREATE TABLE session_identity (",
     "J-W1-4：第二次 migrate 报 table already exists"),
    ("M4", FISH_PY, "j_w1_5",
     "    _ensure_lineage_tables(conn)\n",
     "    # _ensure_lineage_tables(conn)\n",
     "J-W1-5：旧库升级补不出表"),
    ("M5", SCHEMA_PY, "j_w1_1_tables_exist",
     "    + _ACTIVITY_SCHEMA + _LINEAGE_SCHEMA + _KB_SCHEMA + _PROFILE_SCHEMA + _GIT_SCHEMA"
     "   # 画像 / git / 会话树\n",
     "    + _ACTIVITY_SCHEMA + _KB_SCHEMA + _PROFILE_SCHEMA + _GIT_SCHEMA"
     "   # 画像 / git / 会话树\n",
     "J-W1-1：SCHEMA 没拼上会话树"),
    ("M6", DB_PY, "j_w1_5",
     "    for stmt in _LINEAGE_INDEXES:\n        conn.execute(stmt)\n",
     "    pass\n",
     "J-W1-5：旧库升级缺 5 条索引"),
]


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q",
                        "--no-header", "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:120]


def data_mutation() -> bool:
    """D0：直接 INSERT 一行（绕过 `upsert_node` 与任何开关）→ 共用判据必须红。"""
    import fish
    import test_lineage_schema as T          # 与测试**同一份**判据

    tmp = tempfile.mkdtemp(prefix="fish-w1-d0-")
    conn = None
    ok = True
    try:
        conn = fish.connect(Path(tmp) / "data")
        conn.executescript(fish.SCHEMA)
        fish.migrate(conn)
        case = unittest.TestCase()
        try:
            T.assert_lineage_empty(case, conn, "【D0 对照组】")
            print("  D0      ok 对照组绿（迁移后 0 行）")
        except AssertionError as exc:
            print(f"  D0      x 对照组就红了：{exc}")
            return False
        # 绕过 upsert_node：像"别处偷偷写"那样直接 INSERT
        conn.execute(
            "INSERT INTO session_lineage (host, session_id, origin, basis, source, observed_at) "
            "VALUES ('dsh','d0-leak','unknown','missing','dsh:mut',?)", (_NOW,))
        conn.commit()
        try:
            T.assert_lineage_empty(case, conn, "【D0 变异后】")
            print("  D0      x 变异后仍绿——'0 行'判据没有判别力")
            ok = False
        except AssertionError as exc:
            print(f"  D0      ok 变红：{str(exc).splitlines()[-1][:100]}")
        conn.execute("DELETE FROM session_lineage")
        conn.commit()
        try:
            T.assert_lineage_empty(case, conn, "【D0 还原后】")
            print("  D0      ok 还原复绿")
        except AssertionError as exc:
            print(f"  D0      x 还原后仍红：{exc}")
            ok = False
        # 身份表同理（两张表都要有判据，不能只盯一张）
        conn.execute(
            "INSERT INTO session_identity (session_key, citizen_id, origin, evidence, "
            "created_at, observed_at) VALUES ('dsh:d0','cit-x','direct','machine',?,?)",
            (_NOW, _NOW))
        conn.commit()
        try:
            T.assert_lineage_empty(case, conn, "【D0 身份表变异后】")
            print("  D0      x session_identity 的 0 行判据没有判别力")
            ok = False
        except AssertionError:
            print("  D0      ok 变红（session_identity）")
        conn.execute("DELETE FROM session_identity")
        conn.commit()
        T.assert_lineage_empty(case, conn, "【D0 最终还原】")
        print("  D0      ok 最终还原复绿")
        return ok
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def source_mutants(picked):
    fails = []
    for name, path, k, old, new, why in picked:
        print(f"  {name}  [{path.name} / -k {k}]")
        print(f"       打的是：{why}")
        green, tail = _run(k)
        if not green:
            print(f"       x 对照组就红了（判据本身有问题）：{tail}")
            fails.append(name)
            continue
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"       x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mut_green, mut_tail = _run(k)
            if mut_green:
                print(f"       x 变异后仍绿——这条判据没有判别力")
                fails.append(name)
            else:
                print(f"       ok 变红：{mut_tail}")
        finally:
            src.restore()
        if not (src.sha256() == before and src.restored()):
            print("       x 还原失败：工作树被变异体污染，立刻停机检查")
            fails.append(name)
            continue
        back_green, back_tail = _run(k)
        if back_green:
            print("       还原复绿")
        else:
            print(f"       x 还原后仍红：{back_tail}")
            fails.append(name)
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某个变异体（D0 / M1..M6）")
    ap.add_argument("--data-only", action="store_true")
    ap.add_argument("--src-only", action="store_true")
    a = ap.parse_args(argv)

    fails = []
    print("\n=== W1 变异自证（113 §2.6）===")
    if not a.src_only:
        print("\n[D0] 数据级变异：绕过 upsert_node 直接 INSERT 一行")
        if a.only and a.only != "D0":
            print("  D0      跳过（--only 指定了别的）")
        elif not data_mutation():
            fails.append("D0")
    if not a.data_only:
        picked = [m for m in MUTANTS if not a.only or m[0] == a.only]
        if picked:
            print("\n[源码级变异]")
            fails.extend(source_mutants(picked))

    print()
    if fails:
        print(f"  失败：{fails}")
        return 1
    print("  全部变异体通过（红→还原→绿 三步都成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
