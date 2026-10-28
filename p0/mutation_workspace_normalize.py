#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把 normalize_workspace_path 的三处修复逐个摘掉，看判据是否变红。

变异（改真源码 → 跑 → 必红 → 字节还原）：
  M-N1  摘掉 posixpath.normpath  → `..` / `.` 不归一化 → N1/N2 红
  M-N3a 盘符正则改回 [a-z]:      → 大写 C: 匹配不上 → N3 红
  M-N3b 把去尾斜杠的 while 挪到补盘符 if 之后 → 补的 / 被砍 → N3 红
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

FISH = Path("fish.py")
BAK = FISH.with_suffix(".py.norm-bak")

MUTATIONS = [
    ("M-N1", "摘掉 posixpath.normpath（.. / . 不归一化）",
     "    import posixpath\n    s = posixpath.normpath(s)\n",
     "",
     ["test_N1_dotdot_normalized_and_not_matched", "test_N2_dot_normalized"]),
    ("M-N3a", "盘符正则改回只认小写 [a-z]:",
     'if re.fullmatch(r"[A-Za-z]:", s):',
     'if re.fullmatch(r"[a-z]:", s):',
     ["test_N3_drive_root_keeps_slash"]),
    ("M-N3b", "去尾斜杠的 while 挪到补盘符之后（补的 / 被砍掉）",
     '    while len(s) > 1 and s.endswith("/"):\n        s = s[:-1]\n'
     '    # ⚠️ 正则必须容忍大写盘符：此时还没走到末尾的 s.lower()，\n'
     '    # `C:\\` 归一后是 `C:`，用 r"[a-z]:" 会匹配不上、盘符根就不补斜杠了（实测踩过）。\n'
     '    if re.fullmatch(r"[A-Za-z]:", s):\n        s += "/"\n',
     '    if re.fullmatch(r"[A-Za-z]:", s):\n        s += "/"\n'
     '    while len(s) > 1 and s.endswith("/"):\n        s = s[:-1]\n',
     ["test_N3_drive_root_keeps_slash"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(tests) -> int:
    """跑指定判据；返回 0=全绿，非0=有红。"""
    args = [sys.executable, "test_workspace_normalize.py"] + \
           [f"WorkspaceNormalizeTest.{t}" for t in tests]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode


def main() -> int:
    base = sha(FISH)
    print(f"基线 sha256 = {base[:16]}…")
    shutil.copy2(FISH, BAK)

    print("\n=== 基线（未变异）应全绿 ===")
    all_tests = [t for _, _, _, _, ts in MUTATIONS for t in ts]
    bad_base = False
    for t in sorted(set(all_tests)):
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            bad_base = True
    if bad_base:
        print("\n基线不是全绿，终止。")
        shutil.move(BAK, FISH)
        return 1

    results = []
    for mid, desc, old, new, tests in MUTATIONS:
        src = FISH.read_text(encoding="utf-8")
        if old not in src:
            print(f"\n{mid}: 找不到待替换片段，跳过")
            print(f"  想找: {old[:90]!r}")
            results.append((mid, desc, "SKIP"))
            continue
        FISH.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            rc = run(tests)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            for t in tests:
                print(f"    目标判据: {t}")
            results.append((mid, desc, status))
        finally:
            shutil.copy2(BAK, FISH)
            restored = sha(FISH) == base
            print(f"    还原: sha256 {'一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print("    ✗✗ 还原失败，中止！")
                return 2

    print("\n=== 还原后复跑，应全绿 ===")
    for t in sorted(set(all_tests)):
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")

    print("\n" + "=" * 70)
    for mid, desc, status in results:
        print(f"{mid:7} {status:20} {desc}")
    print("=" * 70)
    final = sha(FISH)
    print(f"最终 sha256 = {final[:16]}…（与基线{'一致' if final == base else '不一致'}）")
    BAK.unlink(missing_ok=True)
    # 外部审查 🟠-B2（2026-10-29）：SKIP / MISS 必须让进程**非 0**——
    # 旧实现无条件 `return 0`，于是"锚点缺失"被读成"变异通过"（工具在说谎）。
    bad = [mid for mid, _desc, status in results
           if str(status).startswith(("SKIP", "MISS", "变异没生效"))]
    if bad:
        print(f"✗ {len(bad)} 个变异未真正执行/未生效：{bad}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
