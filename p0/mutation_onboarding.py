#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把「接入体验」的四段修法逐个改坏，看判据是否变红。

变异（改**真源码** → 跑指定判据 → 必红 → 字节级还原）：
  M-A1  register 无条件加「凭证仍指向」提示        → 判据 A 红（自己 register 自己也有）
  M-B1  register 顺手把凭证改指向新公民（改语义）  → 判据 B 红（归属变了）
  M-C1  hook 不查候选（直接当"没有"）              → 判据 C 红（缺候选）
  M-D1  hook 无条件输出候选段                       → 判据 D 红（无候选也啰嗦）
  M-E1  hook 顺手把 cwd 自动声明成候选项目          → 判据 E 红（workspace_map 变了）
  M-F1  `project retire` 计划里顺手落库             → 判据 F 红（计划改了状态）

纪律：每个变异体跑完**立刻**从备份逐字节还原，并校验 sha256；还原失败立即中止。
**全程只碰源码副本与 tempdir**（测试自己用 mkdtemp），不碰生产库。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"
MCP = P0 / "mcp_server.py"
HOOK = P0 / "fish_dsh_hook.py"
BACKUPS = {FISH: P0 / "fish.py.onb-bak",
           MCP: P0 / "mcp_server.py.onb-bak",
           HOOK: P0 / "fish_dsh_hook.py.onb-bak"}

# (编号, 描述, 目标文件, 旧片段, 新片段, 判据)
MUTATIONS = [
    ("M-A1", "register 无条件加「仍指向」提示（名相同也说）", MCP,
     '        if current == name:\n'
     '            return ""                      # 就是你自己：不用警示（变异体在这一行红）\n',
     '        if current is None:                # 变异：名相同也提示（无条件加）\n'
     '            return ""\n',
     ["test_A_warning_only_when_display_name_differs"]),

    ("M-B1", "register 顺手把凭证改指向新公民（违反「不改身份语义」）", MCP,
     '        return (f"{cid}\\n{name} / {kind}\\n"\n'
     '                + self._credential_unmoved_note(ident, name)\n'
     '                + self._register_next_step(a, cid))\n',
     '        if ident and ident.citizen_id:     # 变异：register 顺手改归属\n'
     '            self.conn.execute(\n'
     '                "UPDATE session_tokens SET citizen_id=? WHERE citizen_id=?",\n'
     '                (cid, ident.citizen_id))\n'
     '            self.conn.commit()\n'
     '        return (f"{cid}\\n{name} / {kind}\\n"\n'
     '                + self._credential_unmoved_note(ident, name)\n'
     '                + self._register_next_step(a, cid))\n',
     ["test_B_register_does_not_change_attribution"]),

    ("M-C1", "hook 不查候选（直接当「没有」）", HOOK,
     '    cands = _workspace_candidates(cwd)\n    if not cands:\n',
     '    cands = []                            # 变异：不查候选\n    if not cands:\n',
     ["test_C_hook_lists_candidates_under_cwd"]),

    ("M-D1", "hook 无条件输出候选段（无候选也啰嗦）", HOOK,
     '    if not cands:\n',
     '    if False:                             # 变异：无条件走候选段\n',
     ["test_D_no_candidates_keeps_old_wording"]),

    ("M-E1", "hook 顺手把 cwd 自动声明成第一个候选项目", HOOK,
     '    cands = _workspace_candidates(cwd)\n    if not cands:\n',
     '    cands = _workspace_candidates(cwd)\n'
     '    if cands:                             # 变异：自动声明（顺手写库）\n'
     '        subprocess.run([sys.executable, str(FISH), "--root", str(_data_root()),\n'
     '                        "workspace", "set", cwd, cands[0]["project"]],\n'
     '                       capture_output=True)\n'
     '    if not cands:\n',
     ["test_E_candidate_lookup_is_readonly"]),

    ("M-F1", "`project retire` 计划里顺手落库", FISH,
     '    if plan["already"]:\n        return plan\n',
     '    if plan["already"]:\n        return plan\n'
     '    conn.execute("UPDATE code_registry SET status=? WHERE prefix=?", (target, name))\n'
     '    conn.commit()                         # 变异：计划阶段就写库\n',
     ["test_F_project_retire_plan_is_readonly"]),
]

TEST_FILE = P0 / "test_onboarding.py"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(tests) -> int:
    args = [sys.executable, str(TEST_FILE)] + \
           [f"OnboardingJudgeTest.{t}" for t in tests]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(P0))
    return r.returncode


def restore(hashes) -> bool:
    for path, bak in BACKUPS.items():
        shutil.copy2(bak, path)
    return all(sha(p) == h for p, h in hashes.items())


def main() -> int:
    # 与 fish.py 的 main 同一处置：计划任务/无控制台环境下 stdout 是 GBK，
    # ✓/✗ 这类符号会直接抛 UnicodeEncodeError（"功能正常但退出码 1"的假失败）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    hashes = {p: sha(p) for p in BACKUPS}
    for p, h in hashes.items():
        print(f"基线 {p.name:20} sha256 = {h[:16]}…")
    for path, bak in BACKUPS.items():
        shutil.copy2(path, bak)

    all_tests = sorted({t for *_, tests in MUTATIONS for t in tests})
    print(f"\n=== 基线（未变异）应全绿：{len(all_tests)} 条判据 ===")
    bad_base = False
    for t in all_tests:
        rc = run([t])
        print(f"  {t:56} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        bad_base = bad_base or rc != 0
    if bad_base:
        print("\n基线不全绿，终止。")
        restore(hashes)
        return 1

    results = []
    for mid, desc, target, old, new, tests in MUTATIONS:
        src = target.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"\n{mid}: 片段命中 {src.count(old)} 次（必须恰好 1 次），跳过")
            print(f"  想找: {old[:90]!r}")
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
            ok = restore(hashes)
            print(f"    还原: {'逐字节一致 ✓' if ok else '不一致 ✗'}")
            if not ok:
                print("    ✗✗ 还原失败，中止！（备份在 p0/*.onb-bak）")
                return 2

    print("\n=== 还原后复跑 ===")
    after_ok = True
    for t in all_tests:
        rc = run([t])
        print(f"  {t:56} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        after_ok = after_ok and rc == 0

    print("\n" + "=" * 76)
    for mid, desc, status in results:
        print(f"{mid:7} {status:18} {desc}")
    print("=" * 76)
    green_ok = all(s == "红（符合预期）" for _, _, s in results)
    final_ok = all(sha(p) == h for p, h in hashes.items())
    print(f"变异全部变红: {'是 ✓' if green_ok else '否 ✗'}；"
          f"源码逐字节还原: {'是 ✓' if final_ok else '否 ✗'}；"
          f"复跑全绿: {'是 ✓' if after_ok else '否 ✗'}")
    for bak in BACKUPS.values():
        bak.unlink(missing_ok=True)
    return 0 if (green_ok and final_ok and after_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
