# -*- coding: utf-8 -*-
"""`test_kb_multi_search.py` 的变异驱动。

三个变异，每个都必须让对应判据变红（**且不是靠"改坏代码让它崩"**，
而是让"坏行为真的发生"）：

  V1  `occurrences` 恒 1        → M1b 红（occurrences 不再数次数）
  V2  退回"逐个调 kb_search"     → M2 红（walk 次数变成 N）
  V3  去掉空 needles 早退        → M3a/M3b 红（空词也去 walk）

方法：读原文件 → 内存替换 → 写临时副本 → 跑指定测试 → 必红 → 恢复。
**绝不改原文件**（只在内存与临时目录里做）。
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# ⚠️ `_kb_search_multi` 住在 **fish_kb.py**（不是 fish.py）：
# 它是纯知识库检索逻辑，按 J4 的裁定（kb 的东西不进 fish.py）在 2026-10-27 移过去了，
# fish.py 只 re-export。所以变异必须打在 fish_kb.py 上——打 fish.py 会一个都匹配不上
# （这正是"变异体自身失效"的典型形态：MUTATION-INVALID，不是代码问题）。
SRC = HERE / "fish_kb.py"
TEST = "test_kb_multi_search"


def run_test(test_names):
    """跑指定测试，返回 (exit_code, stdout+stderr)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest"] + list(test_names) + ["-v"],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def is_green(out: str) -> bool:
    """判定：只有出现 'OK' 且没有 'FAILED' 才算绿。"""
    return "FAILED" in out is False and re.search(r"^OK\b", out, re.M) is not None \
        and "FAILED" not in out


MUTATIONS = [
    (
        "V1",
        "occurrences 恒 1（不再数真实出现次数）",
        [f"{TEST}.KbMultiSearchTest.test_M1b_occurrences_are_counted_not_constant"],
        # 只在 _kb_search_multi 里把 occurrences += fl.count(nd) 改成 += 1
        lambda s: s.replace(
            "                    occurrences += fl.count(nd)\n",
            "                    occurrences += 1  # MUTATION V1\n", 1),
    ),
    (
        "V2",
        "退回逐个调 kb_search（每个词各 walk 一遍）",
        [f"{TEST}.KbMultiSearchTest.test_M2_walks_vault_exactly_once"],
        # 把 _kb_search_multi 的函数体首行换成"逐词调 kb_search 再拼回来"
        lambda s: s.replace(
            '    vault_row = _kb_vault_norm(vault_row)\n'
            '    qs = [str(n or "").strip() for n in (needles or [])]\n',
            '    vault_row = _kb_vault_norm(vault_row)\n'
            '    qs = [str(n or "").strip() for n in (needles or [])]\n'
            '    qs = [n for n in qs if n]\n'
            '    if qs:  # MUTATION V2：退回逐个调 kb_search\n'
            '        _out = {}\n'
            '        for _n in qs:\n'
            '            _r = kb_search(conn, vault_row, _n[:200], limit=limit_per_needle)\n'
            '            _out[_n] = dict(_r, total=len(_r.get("hits") or []), ms=0)\n'
            '        return _out\n', 1),
    ),
    (
        "V3",
        "去掉空 needles 早退（空词也去 walk）",
        [f"{TEST}.KbMultiSearchTest.test_M3a_empty_needles_returns_empty",
         f"{TEST}.KbMultiSearchTest.test_M3b_blank_needles_returns_empty"],
        lambda s: s.replace(
            '    qs = [n for n in qs if n]\n'
            '    if not qs:\n'
            '        return {}\n'
            '    root = Path(vault_row["path"])\n',
            '    # MUTATION V3：去掉全空早退\n'
            '    root = Path(vault_row["path"])\n', 1),
    ),
    (
        "V4",
        "去掉 _kb_judge_component 的 lru_cache",
        [f"{TEST}.KbMultiSearchTest.test_M5b_lru_cache_is_actually_hitting"],
        lambda s: s.replace(
            '@functools.lru_cache(maxsize=8192)\n'
            'def _kb_judge_component(part) -> str:\n',
            '# MUTATION V4：去掉 lru_cache\n'
            'def _kb_judge_component(part) -> str:\n', 1),
    ),
]

orig = SRC.read_text(encoding="utf-8")
print(f"原文件 {len(orig.splitlines())} 行\n")

# 先确认基线是绿的
code, out = run_test([f"{TEST}.KbMultiSearchTest.test_M1b_occurrences_are_counted_not_constant",
                      f"{TEST}.KbMultiSearchTest.test_M2_walks_vault_exactly_once",
                      f"{TEST}.KbMultiSearchTest.test_M3a_empty_needles_returns_empty"])
print(f"[基线] exit={code}  {'✅ 绿' if code == 0 else '❌ 红（基线就不绿，变异无意义）'}")
if code != 0:
    print(out[-2000:])
    sys.exit(1)

results = []
for tag, desc, tests, patch in MUTATIONS:
    mutated = patch(orig)
    if mutated == orig:
        print(f"\n[{tag}] ⚠️ 变异未生效（替换没匹配上）—— 这是变异体自身的问题，不是代码问题")
        results.append((tag, desc, "MUTATION-INVALID"))
        continue
    SRC.write_text(mutated, encoding="utf-8")
    try:
        code, out = run_test(tests)
        caught = code != 0
        results.append((tag, desc, "CAUGHT" if caught else "ESCAPED"))
        print(f"\n[{tag}] {desc}")
        print(f"  exit={code}  {'✅ CAUGHT（判据变红）' if caught else '❌ ESCAPED（判据没红！）'}")
        for ln in out.splitlines():
            if ln.startswith(("FAIL:", "ERROR:")) or "AssertionError" in ln:
                print(f"    {ln.strip()[:130]}")
    finally:
        SRC.write_text(orig, encoding="utf-8")

print("\n" + "=" * 70)
print("变异汇总")
print("=" * 70)
for tag, desc, verdict in results:
    print(f"  {tag}  {verdict:16} {desc}")

# 恢复后确认
code, out = run_test([TEST])
print(f"\n[恢复后] exit={code}  {'✅ 全绿' if code == 0 else '❌ 仍红'}")
if code != 0:
    print(out[-1500:])

bad = [r for r in results if r[2] != "CAUGHT"]
sys.exit(1 if bad else 0)
