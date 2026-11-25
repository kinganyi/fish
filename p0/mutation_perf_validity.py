#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-4 / P2-5 判据的变异自证（2026-11-26 · W2-b）。

## 为什么

这轮修的三个点（TTL、status JOIN、缓存上限）都能被写成**恒绿**判据：
缓存测试如果自己把缓存清掉、SQL 测试如果只测 active 公民，都看不出修复点。
所以每个变异体都按同一顺序跑：

  ① 基线（未变异）必须绿 → ② 变异后目标判据必须红 → ③ 按原始字节还原 →
  ④ 还原后再跑必须回到绿。

## 四个变异体

  M1  `CACHE_TTL_S["hosts"]` 3.0 → 0.0（等于删 TTL）→ P1 红
  M2  `CACHE_TTL_S["push_overview"]` 10.0 → 0.0 → P4 红
  M3  `SQL_SESSION_TOKEN_CITIZEN` 去掉 `c.status='active'` → V1 红
  M4  `_CACHE` 淘汰循环恒假（不淘汰）→ P5 红
  M5  MCP `count` 默认改回 True → P6 红

用法：`python -X utf8 mutation_perf_validity.py [--only M1,M3]`
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
WB = HERE / "fish_workbench_api.py"
ACT = HERE / "fish_activity.py"
MCP = HERE / "mcp_server.py"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 说明)
MUTANTS = [
    ("M1", WB,
     "test_p1_two_reads_probe_once_within_ttl",
     '    "hosts": 3.0,\n',
     '    "hosts": 0.0,\n',
     "P1：删掉 hosts 的 3 秒 TTL（第二次读取必须重探）"),
    ("M2", WB,
     "test_p4_two_reads_once_then_write_invalidates",
     '    "push_overview": 10.0,\n',
     '    "push_overview": 0.0,\n',
     "P4：删掉 push_overview 的 10 秒 TTL（第二次必须重算）"),
    ("M3", ACT,
     "test_v1_retired_citizen_token_is_invalid or "
     "test_v3_resolve_citizen_matches_validate_session_token",
     "JOIN citizens_all c ON c.citizen_id=session_tokens.citizen_id AND c.status='active' ",
     "JOIN citizens_all c ON c.citizen_id=session_tokens.citizen_id ",
     "V1：去掉 citizens_all.status='active'（退役公民 token 又变成有效）"),
    ("M4", WB,
     "test_p5_cache_is_bounded_and_evicts_oldest",
     "        while len(_CACHE) > CACHE_MAX_ENTRIES:\n"
     "            _CACHE.popitem(last=False)\n",
     "        while False and len(_CACHE) > CACHE_MAX_ENTRIES:\n"
     "            _CACHE.popitem(last=False)\n",
     "P5：缓存淘汰循环恒假（_CACHE 无上限）"),
    ("M5", MCP,
     "test_p6_default_count_is_false or test_p6b_explicit_count_true",
     'count=bool(a.get("count", False)))',
     'count=bool(a.get("count", True)))',
     "P6：MCP search 默认 count 改回 True（全库 COUNT 重新出现）"),
]


def _run(test_file, selector):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", test_file, "-q",
         "--no-header", "-p", "no:cacheprovider", "-k", selector],
        cwd=str(HERE), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = [line for line in output.strip().split("\n") if line.strip()][-1:]
    return proc.returncode == 0, (tail[0].strip()[:160] if tail else "(无输出)")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None, help="逗号分隔，如 M1,M3")
    args = parser.parse_args(argv)
    picked = MUTANTS
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        picked = [m for m in MUTANTS if m[0] in wanted]
        if not picked:
            print(f"没有匹配的变异体：{sorted(wanted)}")
            return 2

    failures = []
    for name, path, selector, old, new, why in picked:
        test_file = {"test_perf_cache.py": "test_perf_cache.py",
                     "test_activity_validity.py": "test_activity_validity.py"}[
                         "test_activity_validity.py" if path.name == "fish_activity.py"
                         else "test_perf_cache.py"]
        print(f"{name} [{path.name}] {why}")
        src = SourceText(path)
        baseline = src.sha256()
        try:
            green, tail = _run(test_file, selector)
            if not green:
                print(f"  x 基线红（判据本身有问题）：{tail}")
                failures.append(name)
                continue
            print(f"  ok 基线绿：{tail}")
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"  x 靶点未命中：{exc}")
                failures.append(name)
                continue
            mutated_green, mut_tail = _run(test_file, selector)
            if mutated_green:
                print(f"  x 变异后仍绿——判据没有判别力：{mut_tail}")
                failures.append(name)
            else:
                print(f"  ok 变异红：{mut_tail}")
        finally:
            src.restore()
        if src.sha256() != baseline or not src.restored():
            print("  x 还原失败：工作树被污染，停机检查")
            failures.append(name)
            continue
        again_green, again_tail = _run(test_file, selector)
        if not again_green:
            print(f"  x 还原后没有复绿：{again_tail}")
            failures.append(name)
        else:
            print(f"  ok 还原后复绿：{again_tail}")

    print("-" * 72)
    if failures:
        print(f"FAIL {len(failures)}/{len(picked)}：{failures}")
        return 1
    print(f"OK {len(picked)}/{len(picked)}：每条都红过、都复绿过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
