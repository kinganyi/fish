#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把注入包里的 status 过滤摘掉，看判据是否变红。

驱动方式（真源码变异 → 跑 → 必红 → 字节还原）：
  python mutation_injection_status.py

本驱动只改 `fish.py` 的四处 SQL，每次改完立即还原（sha256 校验）。
"""
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FISH = Path("fish.py")
BAK = FISH.with_suffix(".py.mutation-bak")

MUTATIONS = [
    (
        "M-A", "去掉 proj 查询的 status 过滤（活跃数虚高）",
        'WHERE dormant=0 AND status=\'active\' GROUP BY project',
        'WHERE dormant=0 GROUP BY project',
        "test_A_active_count_excludes_non_active",
    ),
    (
        "M-B", "去掉 recent 查询的 status 过滤（可能推已作废）",
        'WHERE project=? AND dormant=0 "\n            "AND status=\'active\' ORDER BY code DESC LIMIT 3',
        'WHERE project=? AND dormant=0 "\n            "ORDER BY code DESC LIMIT 3',
        "test_B_recent_excludes_non_active",
    ),
    (
        "M-C", "去掉 today_codes 的 status 过滤（可能列已作废编码）",
        'WHERE code LIKE ? AND status=\'active\'',
        'WHERE code LIKE ?',
        "test_C_today_codes_excludes_non_active",
    ),
    (
        "M-D", "去掉 dormant_n 的 status 过滤（休眠数虚高）",
        'WHERE dormant=1 AND status=\'active\'',
        'WHERE dormant=1',
        "test_D_dormant_count_excludes_non_active",
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(name: str) -> int:
    r = subprocess.run(
        [sys.executable, "test_injection_status.py", f"InjectionStatusFilterTest.{name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    # unittest 用退出码表达结果：0=通过，非0=失败
    return r.returncode


def main() -> int:
    base = sha(FISH)
    print(f"基线 fish.py sha256 = {base[:16]}…")
    print(f"备份到 {BAK.name}")
    shutil.copy2(FISH, BAK)

    # 先确认四条判据在未变异时全绿
    print("\n=== 基线（未变异）应该全绿 ===")
    ok_all = True
    for mid, desc, _old, _new, test in MUTATIONS:
        rc = run_test(test)
        mark = "绿 ✓" if rc == 0 else f"红 ✗ (rc={rc})"
        print(f"  {test:46} {mark}")
        if rc != 0:
            ok_all = False
    if not ok_all:
        print("\n基线就不是全绿——判据本身有问题，终止。")
        shutil.move(BAK, FISH)
        return 1

    results = []
    for mid, desc, old, new, test in MUTATIONS:
        src = FISH.read_text(encoding="utf-8")
        if old not in src:
            print(f"\n{mid}: 找不到待替换片段，跳过（片段可能已变）")
            print(f"  想找: {old[:80]}")
            results.append((mid, desc, test, "SKIP"))
            continue
        mutated = src.replace(old, new, 1)
        FISH.write_text(mutated, encoding="utf-8")
        try:
            rc = run_test(test)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"  目标判据: {test}")
            results.append((mid, desc, test, status))
        finally:
            shutil.copy2(BAK, FISH)
            restored = sha(FISH) == base
            print(f"  还原: sha256 {'一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print("  ✗✗ 还原失败，立即中止！")
                return 2

    # 收尾再跑一次全绿
    print("\n=== 还原后复跑，应全绿 ===")
    for mid, desc, _o, _n, test in MUTATIONS:
        rc = run_test(test)
        print(f"  {test:46} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")

    print("\n" + "=" * 70)
    for mid, desc, test, status in results:
        print(f"{mid:6} {test:46} {status}")
    print("=" * 70)
    print(f"最终 fish.py sha256 = {sha(FISH)[:16]}…（与基线{'一致' if sha(FISH) == base else '不一致'}）")
    BAK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
