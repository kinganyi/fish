#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W2（DSH 适配器 + depth 重算）判据的变异自证（113 §3.4，2026-11-25）。

## 为什么这一波要做变异

W2 的判据大多是"例外为 0"——这类断言最容易被写成恒绿：SQL 里少一个 `AND`、`or` 写成
`and`、统计用了个恒 0 的计数器，全都会让"0 例外"在坏代码上照样绿。所以每条判据都得先在
**坏代码**下红一次，再在还原后复绿。**没有对照组红的变异不算证据**。

## 一个数据级变异 + 六个源码级变异

  D0（数据级，不是源码变异）——113 §3.2/§3.4 明文要求的那个：
      删掉某个子会话头的 `origin` 字段 → 该行必须落 `unknown` + `inferred`，
      **不得变 `main`**；并且证明"就算有人写成 main，库也不会收"（J-D4 的库级 CHECK）。

  M1  `fish_lineage.py` 去掉 `instr` 环守卫          → 环用例红（无限递归被进度回调掐断）
  M2  `fish_lineage_dsh.py` 把 fork 判据的「与」改成「或」 → fork 用例红（4 行 seed+spawn 被误标）
  M3  `fish_lineage_dsh.py` 把矛盾行的 origin 写成 `main` → 红（撞库级 CHECK）
  M4  `fish_lineage_dsh.py` 摘掉孤儿降级               → 红（父缺失的行不再落 missing）
  M5  `fish_lineage.py` 重算改成直接读宿主值           → 四层链读到 0/0/0/0
  M6  `fish_lineage.py` 不再标 conflict                → 宿主值与重算不一致时无人知道

M1 特别说明：去掉守卫之后那条 CTE 是**真的无限递归**（不是"结果不对"）。测试侧装了
SQLite 进度回调（20 万步预算），所以变异体会以 `interrupted` 快速失败——否则这个变异
得跑满超时才能"红"，那等于没法用。

用法：`python -X utf8 mutation_lineage_dsh.py [--only M3] [--data-only] [--src-only]`
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
LINEAGE_PY = HERE / "fish_lineage.py"
DSH_PY = HERE / "fish_lineage_dsh.py"
TEST = "test_lineage_dsh.py"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", LINEAGE_PY, "cycle_is_cut",
     "     AND instr(u.path, '/' || p.parent_session || '/') = 0\n",
     "     AND 1\n",
     "环守卫：去掉后 A↔B 无限递归（测试侧的进度回调把它掐断 → 红）"),
    ("M2", DSH_PY, "rule1_fork",
     "    if seeded and parent and depth == 0:\n",
     "    if seeded or (parent and depth == 0):\n",
     "fork 判据：三条「同时」改成「或」→ seed+depth=1 的 4 行被误标 fork"),
    ("M3", DSH_PY, "conflict_is_unknown",
     '        verdict = _verdict("unknown", "spawn", "inferred", SOURCE_DEPTH, "conflict",\n',
     '        verdict = _verdict("main", "spawn", "inferred", SOURCE_DEPTH, "conflict",\n',
     "矛盾行：origin 落 main → 撞库级 CHECK（J-D4）"),
    ("M4", DSH_PY, "orphan_parent",
     "    if parent and known_ids is not None and parent not in known_ids:\n",
     "    if False and parent and known_ids is not None and parent not in known_ids:\n",
     "孤儿诚实：父缺失不再降级 → 行按宿主口吻落库（不是 unknown/missing）"),
    ("M5", LINEAGE_PY, "four_level_chain",
     "        depth, path, cur = best[sid]\n",
     '        depth, path, cur = (rows[sid][5] or 0, "", best[sid][2])\n',
     "深度重算：改成直接读宿主值 → 四层链读到 0/0/0/0"),
    ("M6", LINEAGE_PY, "conflict_is_reversible",
     "        conflict = depth_host is not None and depth_host != depth\n",
     "        conflict = False\n",
     "对拍留痕：不再标 conflict → 宿主值与重算不一致时无人知道"),
]

#: D0 用的小语料：一个根 + 一个子代理（子代理的 origin 是可删的那一列）
_D0_EVENTS = {
    "root-d0": {"type": "session", "version": 4, "id": "root-d0",
                "createdAt": 1791167529348, "cwd": "I:\\w", "delegationDepth": 0,
                "agentPreset": "dafish-team"},
    "sub-d0": {"type": "session", "version": 4, "id": "sub-d0", "cwd": "I:\\w",
               "createdAt": 1791167529348, "origin": "subagent",
               "parentSession": "root-d0", "delegationDepth": 1,
               "agentPreset": "dafish-team"},
}


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q",
                            "--no-header", "-p", "no:cacheprovider", "-k", k],
                           cwd=str(HERE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    out = (result.stdout or "") + (result.stderr or "")
    tail = [line for line in out.strip().split("\n") if line.strip()][-1:] or ["(无输出)"]
    return result.returncode == 0, tail[0].strip()[:140]


def data_mutation() -> bool:
    """D0：删掉子会话头的 `origin` → 必须 `unknown` + `inferred`，**不得变 `main`**。"""
    import fish
    import fish_lineage
    import fish_lineage_dsh as dsh
    import test_lineage_dsh as T          # 与测试**同一份**判据

    tmp = tempfile.mkdtemp(prefix="fish-w2-d0-")
    conn = None
    ok = True
    try:
        frames = Path(tmp) / "sessions"
        for sid, event in _D0_EVENTS.items():
            path = frames / "--ws--" / sid / dsh.FRAME_NAME
            path.parent.mkdir(parents=True)
            body = json.dumps(event, ensure_ascii=False) + "\n"
            path.write_bytes(T.zstandard.ZstdCompressor().compress(body.encode("utf-8")))
        root = Path(tmp) / "data"
        root.mkdir()
        conn = fish.connect(root)
        conn.executescript(fish.SCHEMA)
        nodes, report = dsh.DshLineageAdapter(roots=frames).scan_with_report()
        for node in nodes:
            fish_lineage.upsert_node(conn, node)
        conn.commit()
        fish_lineage.recompute_depth(conn, "dsh")
        conn.commit()
        case = unittest.TestCase()
        try:
            T.assert_projection_invariants(case, conn, "【D0 对照组】")
            print("  D0      ok 对照组绿（2 个会话头，0 例外）")
        except AssertionError as exc:
            print(f"  D0      x 对照组就红了：{exc}")
            return False
        if report["failed"]:
            print(f"  D0      x 夹具不合格：解析失败 {report['failed']}")
            return False
        before = conn.execute(
            "SELECT origin, basis FROM session_lineage WHERE session_id='sub-d0'").fetchone()
        if before != ("subagent", "header"):
            print(f"  D0      x 夹具不合格：子代理行是 {before}")
            return False

        # —— 变异：删掉子会话头的 origin（数据级，不改任何源码）——
        header = dsh.read_header(next((frames / "--ws--" / "sub-d0").glob("session*")))
        del header["origin"]
        verdict = dsh.classify(header, known_ids={"root-d0", "sub-d0"})
        got = (verdict["origin"], verdict["basis"])
        if got == ("unknown", "inferred"):
            print("  D0      ok 变红定性正确：origin='unknown' / basis='inferred'")
        else:
            print(f"  D0      x 删掉 origin 后落成 {got}（应为 unknown/inferred）")
            ok = False
        if verdict["origin"] == "main":
            print("  D0      x 落成了 main —— J-D4 禁止的兜底路径出现了")
            ok = False
        fish_lineage.upsert_node(conn, dsh.node_from_header(header, verdict=verdict))
        conn.commit()
        after = conn.execute(
            "SELECT origin, basis, source FROM session_lineage WHERE session_id='sub-d0'"
        ).fetchone()
        if after[0] == "unknown" and after[1] == "inferred" and after[2].endswith(":depth"):
            print(f"  D0      ok 库内落点正确：{after}")
        else:
            print(f"  D0      x 库内落点不对：{after}")
            ok = False

        # 反证：就算实现把它兜成 main，库级 CHECK 也不会收（证明"不得变 main"有牙）
        try:
            conn.execute(
                "INSERT INTO session_lineage (host, session_id, parent_session, origin, basis, "
                "source, observed_at) VALUES ('dsh','main-with-parent','root-d0','main',"
                "'header','dsh:header','2026-11-25T00:00:00Z')")
            print("  D0      x main+父 居然插进去了 —— J-D4 的库级兜底不在")
            ok = False
        except sqlite3.IntegrityError:
            print("  D0      ok 反证成立：main+父 被库级 CHECK 拒（J-D4 的兜底还在）")
        conn.rollback()

        # —— 还原：把 origin 加回去，同一行必须回到 subagent ——
        header["origin"] = "subagent"
        fish_lineage.upsert_node(conn,
                                 dsh.node_from_header(header, known_ids={"root-d0", "sub-d0"}))
        conn.commit()
        restored = conn.execute(
            "SELECT origin, basis FROM session_lineage WHERE session_id='sub-d0'").fetchone()
        if restored == ("subagent", "header"):
            print("  D0      ok 还原复绿（子代理行回到 subagent）")
        else:
            print(f"  D0      x 还原后仍是 {restored}")
            ok = False
        return ok
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def source_mutants(picked):
    fails = []
    for name, path, selector, old, new, why in picked:
        print(f"  {name}  [{path.name} / -k {selector}]")
        print(f"       打的是：{why}")
        green, tail = _run(selector)
        if not green:
            print(f"       x 对照组就红了（判据本身有问题）：{tail}")
            fails.append(name)
            continue
        source = SourceText(path)
        before = source.sha256()
        try:
            try:
                source.write(source.replaced(old, new))
            except AssertionError as exc:
                print(f"       x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mutated_green, mutated_tail = _run(selector)
            if mutated_green:
                print("       x 变异后仍绿 —— 这条判据没有判别力")
                fails.append(name)
            else:
                print(f"       ok 变红：{mutated_tail}")
        finally:
            source.restore()
        if not (source.sha256() == before and source.restored()):
            print("       x 还原失败：工作树被变异体污染，立刻停机检查")
            fails.append(name)
            continue
        back_green, back_tail = _run(selector)
        if back_green:
            print("       还原复绿")
        else:
            print(f"       x 还原后仍红：{back_tail}")
            fails.append(name)
    return fails


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="只跑某个变异体（D0 / M1..M6）")
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--src-only", action="store_true")
    args = parser.parse_args(argv)

    fails = []
    print("\n=== W2 变异自证（113 §3.4）===")
    if not args.src_only:
        print("\n[D0] 数据级变异：删掉子会话头的 origin（113 §3.2/§3.4 明文要求）")
        if args.only and args.only != "D0":
            print("  D0      跳过（--only 指定了别的）")
        elif not data_mutation():
            fails.append("D0")
    if not args.data_only:
        picked = [m for m in MUTANTS if not args.only or m[0] == args.only]
        if picked:
            print("\n[源码级变异]")
            fails.extend(source_mutants(picked))

    print()
    if fails:
        print(f"  失败：{fails}")
        return 1
    print("  全部变异体通过（红 → 还原 → 绿 三步都成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
