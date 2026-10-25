#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把 `session start --cwd` 的接线逐个摘掉，看判据是否变红。

变异（改真源码 → 跑 → 必红 → 字节还原）：
  M-C1  摘掉 cmd_session_start 里的 --cwd 解析      → C1/C5 红
  M-C2  无匹配时回退到"最近更新项目"（破坏"不猜"）  → C2 红
  M-C3  让 --cwd 压过 --project（优先级反转）       → C3 红
  M-C4  诊断句改回 print 到 stdout                  → C4 红
  M-C5  hook 不再传 --cwd                           → C5 红
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"
HOOK = P0 / "fish_dsh_hook.py"
FISH_BAK = P0 / "fish.py.cwd-bak"
HOOK_BAK = P0 / "fish_dsh_hook.py.cwd-bak"

# (编号, 描述, 目标文件, 旧片段, 新片段, 判据)
MUTATIONS = [
    ("M-C1", "摘掉 cmd_session_start 的 --cwd 解析", FISH,
     '    projects = getattr(args, "project", None)\n'
     '    cwd = getattr(args, "cwd", None)\n'
     '    resolved_from_cwd = None\n'
     '    if not projects and cwd:\n'
     '        try:\n'
     '            resolved_from_cwd = resolve_project_by_cwd(conn, cwd)\n'
     '        except Exception:\n'
     '            resolved_from_cwd = None\n'
     '        if resolved_from_cwd:\n'
     '            projects = [resolved_from_cwd]\n',
     '    projects = getattr(args, "project", None)\n'
     '    resolved_from_cwd = None\n',
     ["test_C1_cwd_hit_opens_stint", "test_C5_hook_end_to_end"]),

    ("M-C2", "无匹配时硬塞一个项目（破坏『不猜』）", FISH,
     '        if resolved_from_cwd:\n            projects = [resolved_from_cwd]\n',
     '        if resolved_from_cwd:\n            projects = [resolved_from_cwd]\n'
     '        else:\n'
     '            # 变异：无匹配也硬塞一个（模拟"猜一个项目"的旧行为）。\n'
     '            # 第一版变异写成"回退到最近更新的记忆"，但测试库是空的（只建了\n'
     '            # workspace_map），_fb 恒为 None ⇒ 变异不生效 ⇒ 判据 C2 仍绿。\n'
     '            # 这暴露的是**变异体**的问题，不是判据的问题：要测"能不能抓住猜"，\n'
     '            # 就得让"猜"这件事真的发生。改成硬编码。\n'
     '            resolved_from_cwd = "ANYI"\n'
     '            projects = ["ANYI"]\n',
     ["test_C2_cwd_miss_opens_nothing"]),

    ("M-C3", "优先级反转：--cwd 压过 --project", FISH,
     '    if not projects and cwd:\n',
     '    if cwd:\n',
     ["test_C3_project_overrides_cwd"]),

    ("M-C4", "诊断句改回 print 到 stdout", FISH,
     '        print(f"（--cwd 未匹配任何 workspace_map 条目，未开档期：{cwd}）",\n'
     '              file=sys.stderr)\n',
     '        print(f"（--cwd 未匹配任何 workspace_map 条目，未开档期：{cwd}）")\n',
     ["test_C4_diagnostic_goes_to_stderr_not_stdout"]),

    ("M-C5", "hook 不再传 --cwd", HOOK,
     '    cwd = str(payload.get("cwd") or "").strip()\n'
     '    if cwd:\n'
     '        cmd += ["--cwd", cwd]\n',
     '    cwd = str(payload.get("cwd") or "").strip()\n',
     ["test_C5_hook_end_to_end"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(tests) -> int:
    args = [sys.executable, str(P0 / "test_session_cwd.py")] + \
           [f"SessionCwdTest.{t}" for t in tests]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(P0))
    return r.returncode


def main() -> int:
    base_fish, base_hook = sha(FISH), sha(HOOK)
    print(f"基线 fish.py          sha256 = {base_fish[:16]}…")
    print(f"基线 fish_dsh_hook.py sha256 = {base_hook[:16]}…")
    shutil.copy2(FISH, FISH_BAK)
    shutil.copy2(HOOK, HOOK_BAK)

    all_tests = sorted({t for _, _, _, _, _, ts in MUTATIONS for t in ts})
    print("\n=== 基线（未变异）应全绿 ===")
    bad_base = False
    for t in all_tests:
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        bad_base = bad_base or rc != 0
    if bad_base:
        print("\n基线不全绿，终止。")
        shutil.move(FISH_BAK, FISH)
        shutil.move(HOOK_BAK, HOOK)
        return 1

    results = []
    for mid, desc, target, old, new, tests in MUTATIONS:
        src = target.read_text(encoding="utf-8")
        if old not in src:
            print(f"\n{mid}: 找不到片段，跳过\n  想找: {old[:80]!r}")
            results.append((mid, desc, "SKIP"))
            continue
        target.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            rc = run(tests)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"    判据: {', '.join(tests)}")
            results.append((mid, desc, status))
        finally:
            shutil.copy2(FISH_BAK, FISH)
            shutil.copy2(HOOK_BAK, HOOK)
            ok = sha(FISH) == base_fish and sha(HOOK) == base_hook
            print(f"    还原: {'一致 ✓' if ok else '不一致 ✗'}")
            if not ok:
                print("    ✗✗ 还原失败，中止！")
                return 2

    print("\n=== 还原后复跑 ===")
    for t in all_tests:
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")

    print("\n" + "=" * 72)
    for mid, desc, status in results:
        print(f"{mid:7} {status:18} {desc}")
    print("=" * 72)
    ok = sha(FISH) == base_fish and sha(HOOK) == base_hook
    print(f"最终 sha256 {'全部一致' if ok else '不一致 ✗'}")
    FISH_BAK.unlink(missing_ok=True)
    HOOK_BAK.unlink(missing_ok=True)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
