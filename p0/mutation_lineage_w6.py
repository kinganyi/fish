#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W6（历史回填与对账）判据的变异自证（113 §10.2，2026-11-25）。

## 为什么 W6 必须变异

W6 的判据有三种"很容易写成恒绿"的形状：

  · **"0 行也要打印"**——把打印挪进 `if ids:` 之后，报告在"没有异常"时**一样干净**，
    只有真出环的那天才发现它从来不打印（而那天恰好最需要它打印）；
  · **"标记"**——`relation='cycle'` / `basis='conflict'` 是**副作用**，不标也能算出清单，
    于是一年之后没人知道那两行为什么"看起来正常"；
  · **"不猜"**——判不到的父/深度只要随手填一个值，报告就会变好看（而树是假的）。

三条都先跑对照组（必须绿）、再变异（必须红）、还原后必须复绿。**没有对照组红的变异
不算证据**：那可能只是变异脚本自己坏了。

## 八个变异体（靶点都在本波改过/新增的行上）

  M1  `list_lines` 的空清单分支写成恒假        → J-W6-C 红（0 行不再打印）
  M2  `detect_cycles` 永不标环                → J-D3 红（relation/basis 不落）
  M3  `parent_of` 去掉环过滤                   → J-D3 红（环成员被当成有父 = 可继承/可推送）
  M4  `detect_cycles` 不收集 conflict          → 冲突不列名
  M5  `scan` 的 dry-run 分支恒假（直接写生产库） → "dry-run 零写"红
  M6  `scan` 不做父闭合                        → WB 树整片孤儿
  M7  `scan` 按 cwd 猜父                       → "孤儿不许被编一条边"红
  M8  `scan` 不重算深度（直接沿用默认 0）        → 四层链读到 0/0/0/0

用法：`python -X utf8 mutation_lineage_w6.py [--only M1,M3]`
"""
import argparse
import subprocess
import sys
from pathlib import Path

from mutation_source import SourceText

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
LINEAGE_PY = HERE / "fish_lineage.py"
BACKFILL_PY = HERE / "fish_lineage_backfill.py"
TEST = "test_lineage_w6.py"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", BACKFILL_PY, "test_w6_zero_lists_are_printed_not_silent",
     "    if not ids:\n        return [f\"  {label}：本次检测到 0 行（{hint}）\"]\n",
     "    if False:\n        return [f\"  {label}：本次检测到 0 行（{hint}）\"]\n",
     "J-W6-C：空清单不再打印 → 『没打印』与『没检测』重新混成一件事"),
    ("M2", LINEAGE_PY, "test_w6_cycle_is_marked_and_not_inherited",
     "    marks = ({\"marked\": [], \"restored\": []} if not apply\n"
     "             else _mark_cycles(conn, host, cycles, rows))\n",
     "    marks = {\"marked\": [], \"restored\": []}\n",
     "J-D3：从不标环 → relation/basis 永远停在适配器给的值（清单算得出来但库看不见）"),
    ("M3", LINEAGE_PY, "test_w6_cycle_is_marked_and_not_inherited",
     "        if row[1] == \"cycle\":      # 环成员：对上层表现为\"没有父\"（不参与继承/推送）\n"
     "            return None\n",
     "        if False:      # 环成员：对上层表现为\"没有父\"（不参与继承/推送）\n"
     "            return None\n",
     "J-D3：环成员进入继承/推送（`parent_of` 照常返回父）"),
    ("M4", LINEAGE_PY, "test_w6_conflict_names_rows_with_host_value_mismatch",
     "        if depth_host is not None and depth_host != depth:\n"
     "            conflicts.append(sid)\n",
     "        if False and depth_host is not None and depth_host != depth:\n"
     "            conflicts.append(sid)\n",
     "conflict 清单：不再收集对不上的行（只报个数也变成恒 0）"),
    ("M5", BACKFILL_PY, "test_w6_dry_run_writes_nothing_and_apply_writes",
     "        if dry_run:\n            target = _shadow_of(conn, host)\n",
     "        if False:\n            target = _shadow_of(conn, host)\n",
     "dry-run：缺省模式直接写生产库（影子库那段成为死代码）"),
    ("M6", BACKFILL_PY, "test_w6_parent_closure_brings_parents_outside_scan_surface",
     "            extra_nodes, missing_parents = _resolve_parents(adapter, nodes)\n",
     "            extra_nodes, missing_parents = [], []\n",
     "父闭合：扫描面之外的父不再补进投影 → WB 的 2,100 个成员全落 orphan"),
    ("M7", BACKFILL_PY, "test_w6_orphan_parent_is_not_guessed_from_cwd",
     "    nodes, duplicates = _unique_nodes(nodes)\n",
     "    nodes, duplicates = _unique_nodes(nodes)\n"
     "    _known = {n.get(\"session_id\") for n in nodes}\n"
     "    _by_cwd = {}\n"
     "    for _n in nodes:\n"
     "        if _n.get(\"parent_session\") and _n[\"parent_session\"] in _known:\n"
     "            _by_cwd.setdefault(_n.get(\"cwd\"), _n[\"parent_session\"])\n"
     "    for _n in nodes:\n"
     "        if _n.get(\"parent_session\") and _n[\"parent_session\"] not in _known:\n"
     "            if _by_cwd.get(_n.get(\"cwd\")):\n"
     "                _n[\"parent_session\"] = _by_cwd[_n.get(\"cwd\")]\n",
     "按 cwd 猜父：判不到的父被同工作区的另一个父顶掉 → 孤儿『消失』（树是编的）"),
    ("M8", BACKFILL_PY, "test_w6_four_level_chain_backfills_depths",
     "            depth = fish_lineage.recompute_depth(target, host)\n",
     "            depth = {\"updated\": 0, \"conflicts\": 0, \"conflict_ids\": [],\n"
     "                     \"cycles\": [], \"orphans\": [], \"max_depth\": 0, \"walk_rows\": 0}\n",
     "深度重算：回填直接把宿主默认值当深度 → 四层链读到 0/0/0/0"),
]


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q", "--no-header",
         "-p", "no:cacheprovider", "-k", k],
        cwd=str(HERE), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    out = (result.stdout or "") + (result.stderr or "")
    tail = [line for line in out.strip().split("\n") if line.strip()][-1:] or ["(无输出)"]
    return result.returncode == 0, tail[0].strip()[:160]


def source_mutants(picked) -> list:
    fails = []
    for name, path, selector, old, new, why in picked:
        print(f"  {name}  [{path.name} / -k {selector}]")
        print(f"       打的是：{why}")
        green, tail = _run(selector)
        if not green:
            print(f"       x 对照组就红了（判据本身有问题）：{tail}")
            fails.append(name)
            continue
        print(f"       ok 对照组绿：{tail}")
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"       x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mut_green, mut_tail = _run(selector)
            if mut_green:
                print("       x 变异后仍绿——这条判据没有判别力")
                fails.append(name)
            else:
                print(f"       ok 变红：{mut_tail}")
        finally:
            src.restore()
        if not (src.sha256() == before and src.restored()):
            print("       x 还原失败：工作树被变异体污染，立刻停机检查")
            fails.append(name)
            continue
        again_green, again_tail = _run(selector)
        if not again_green:
            print(f"       x 还原后没有复绿：{again_tail}")
            fails.append(name)
        else:
            print(f"       ok 还原后复绿：{again_tail}")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑这些变异体（逗号分隔，如 M1,M3）")
    args = ap.parse_args(argv)
    picked = MUTANTS
    if args.only:
        want = {item.strip().upper() for item in args.only.split(",") if item.strip()}
        picked = [m for m in MUTANTS if m[0] in want]
        if not picked:
            print(f"没有匹配的变异体：{sorted(want)}；可选 {[m[0] for m in MUTANTS]}")
            return 2
    print(f"W6 变异自证：{len(picked)} 个变异体（对照组 + 变异 + 还原三步都跑）")
    fails = source_mutants(picked)
    print("-" * 72)
    if fails:
        print(f"FAIL {len(fails)}/{len(picked)}：{fails}")
        return 1
    print(f"OK {len(picked)}/{len(picked)}：每条都红过、都复绿过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
