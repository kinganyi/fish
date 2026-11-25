#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W5（读取路由与 UI）判据的变异自证（113 §9.4，2026-11-25）。

## 为什么 W5 必须变异

W5 的三条判据全是"**判不到怎么办**"的形状：

  J7          键集合 ⊇ 旧 5 + 三新 —— 少一个键在 99% 的界面上看不出来
  J-W5-legacy 假代理 = 0        —— 迁移跑过就恒 0，只有"新写入还在抄"才会复原
  J6          团队会话 ≥2 行且有 agent_role —— 投影表 0 行时全绿（本机实测就是 0 行）

这三类都能被写成**恒绿**。所以每个变异体都先跑一遍对照组（必须绿），再变异（必须红），
还原后必须复绿：只红不复绿说明变异体污染了工作树，只绿不红说明判据读空了。

## 四个变异体（靶点都在本波改过的行上）

  M1  `_lineage_keys` 的"判不到"分支删掉 `parentSession` → J7 键集合红
  M2  `fish_board.board_auto_checkin` 恢复 `sk or None`（假代理）→ J-W5-legacy 红
  M3  清退 ② 步的条件写成恒假（假代理不清）→ J-W5-legacy 三步计数红
  M4  树视图行把 `agent_id` 置空 → J6 红

用法：`python -X utf8 mutation_w5.py [--only M1|M2|M3|M4]`
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
WB_PY = HERE / "fish_workbench_api.py"
BOARD_PY = HERE / "fish_board.py"
FISH_PY = HERE / "fish.py"
TEST = "test_w5_read_paths.py"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", WB_PY, "j7_all_four_branches_keep_five_and_gain_three or "
                  "j7_no_lineage_gives_none_not_main",
     '        return {"parentSession": None, "agent": None, "relation": None}\n',
     '        return {"agent": None, "relation": None}\n',
     "J7：「判不到」分支少一个 `parentSession` 键（四档里三档的界面看不出来）"),
    ("M2", BOARD_PY, "j_w5_legacy_checkin_writes_no_fake_agent",
     '            values += [None, "active", _auto_doing_text(source), "", now, now, "",\n',
     '            values += [sk or None, "active", _auto_doing_text(source), "", now, now, "",\n',
     "J-W5-legacy：自动行恢复 `sk or None`（拿会话键冒充代理）"),
    ("M3", FISH_PY, "j_w5_legacy_three_steps or j_w5_legacy_idempotent",
     '    ("agent_id=NULL", "agent_id IS NOT NULL AND agent_id <> \'\' AND agent_id = session_key"),\n',
     '    ("agent_id=NULL", "0"),\n',
     "J-W5-legacy：② 清假代理恒假（迁移跑完仍是 0，但计数与清退都失效）"),
    ("M4", WB_PY, "j6_tree_projection_has_two_children_with_role",
     '        node["depth"] = int(item["d"])\n',
     '        node["agent_id"] = None\n        node["depth"] = int(item["d"])\n',
     "J6：树视图把 `agent_id` 置空（只剩角色 = 只说'有个代理'）"),
]


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q",
                        "--no-header", "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:140]


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
            mut_green, mut_tail = _run(k)
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
        again_green, again_tail = _run(k)
        if not again_green:
            print(f"       x 还原后没有复绿：{again_tail}")
            fails.append(name)
        else:
            print(f"       ok 还原后复绿：{again_tail}")
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑这些变异体（逗号分隔，如 M1,M3）")
    args = ap.parse_args(argv)
    picked = MUTANTS
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        picked = [m for m in MUTANTS if m[0] in want]
        if not picked:
            print(f"没有匹配的变异体：{sorted(want)}；可选 {[m[0] for m in MUTANTS]}")
            return 2
    print(f"W5 变异自证：{len(picked)} 个变异体（对照组 + 变异 + 还原三步都跑）")
    fails = source_mutants(picked)
    print("-" * 72)
    if fails:
        print(f"FAIL {len(fails)}/{len(picked)}：{fails}")
        return 1
    print(f"OK {len(picked)}/{len(picked)}：每条都红过、都复绿过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
