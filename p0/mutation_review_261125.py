#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：P0-1 / P1-6 / P1-9 / P1-10 / P2-6 的判据**能不能红**。

驱动方式（改**真源码** → 跑指定判据 → 必红 → 字节级还原）：
    python mutation_review_261125.py

纪律与 `mutation_injection_blocks.py` 同一套：每个变异体跑完**立刻**从备份逐字节
还原并校验 sha256；还原失败立即中止。**只碰 p0/ 下的源码与 tempdir 的测试库**，
不碰生产数据目录。

每个变异体都改"实现里最可能被写错的那一句"，且**基线先全绿**（否则"红"说明不了
任何事——那可能只是判据本来就坏）。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent

# (编号, 说明, 目标文件, 旧片段, 新片段, 判据节点)
MUTATIONS = [
    # ── P0-1：作者信任门 ──────────────────────────────────────────────
    ("M-TRUST1", "删掉整条作者信任过滤（visitor / 无主照进包）",
     "fish_inject.py",
     '    strict_sql = ("owner IS NOT NULL AND owner NOT IN (?,?) "\n'
     '                  "AND owner NOT IN (SELECT citizen_id FROM citizens WHERE kind=?)")\n',
     '    return "", []                      # 变异：整条作者信任过滤被删掉\n'
     '    strict_sql = ("owner IS NOT NULL AND owner NOT IN (?,?) "\n'
     '                  "AND owner NOT IN (SELECT citizen_id FROM citizens WHERE kind=?)")\n',
     ["test_inject_trust_gate.py::InjectTrustGateTest::test_visitor_lesson_is_excluded_resident_kept",
      "test_inject_trust_gate.py::InjectTrustGateTest::test_visitor_rule_is_excluded_resident_kept",
      "test_inject_trust_gate.py::InjectTrustGateTest::test_unknown_and_null_owner_rule_excluded"]),

    ("M-TRUST2", "开关支路把 visitor 也一起放行（门等于没关）",
     "fish_inject.py",
     '    return (" AND (owner IS NULL OR owner IN (?,?) OR (" + strict_sql + "))",\n'
     '            list(UNTRUSTED_OWNERS) + strict_params)\n',
     '    return " AND 1=1", []              # 变异：开关把 visitor 也一起放行\n',
     ["test_inject_trust_gate.py::InjectTrustGateTest::test_switch_releases_only_unowned_not_visitor"]),

    # ── P1-6：重绑吊销 ───────────────────────────────────────────────
    ("M-REVOKE", "重绑时不吊销旧 token（P1-6 原状）",
     "fish.py",
     '        revoked = revoke_session_tokens(conn, session_key=key) if alive is not None else 0\n',
     '        revoked = 0                    # 变异：重绑不吊销旧 token\n',
     ["test_session_rebind_revoke.py::EnsureRebindRevokesTest::test_rebind_revokes_old_token"]),

    # ── P1-9：判不到要 exit 2 ────────────────────────────────────────
    ("M-HEALTH", "断链扫描失败被念成干净（exit 0）",
     "fish.py",
     '    raise SystemExit(2 if broken is None else (1 if (broken or _ghost) else 0))\n',
     '    raise SystemExit(1 if (broken or _ghost) else 0)   # 变异：判不到被念成干净\n',
     ["test_healthcheck_inconclusive.py::HealthcheckTristateTest"
      "::test_scan_failure_exits_two_and_says_unknown"]),

    # ── P2-5：体检只读连接 ───────────────────────────────────────────
    ("M-READONLY", "体检改回读写连接（connect）",
     "fish.py",
     '    import fish_ui as _ui          # 本地 import：只有体检需要，别让每次 CLI 启动都付\n'
     '    conn = _ui.open_readonly(str(_db))\n',
     '    conn = connect(_root)          # 变异：体检用读写连接\n',
     ["test_healthcheck_inconclusive.py::HealthcheckTristateTest"
      "::test_healthcheck_uses_the_readonly_opener"]),

    # ── P2-6#1：死分支 ──────────────────────────────────────────────
    ("M-DEAD", "把 `and False:` 死分支放回去（永不执行却让人以为约束还在）",
     "fish.py",
     '            kind=kind, role=role)\n'
     '        # 2026-10-29（外部审查 🔴1）：`self:` 认领**不再**要求证明——`session_key` 就是\n',
     '            kind=kind, role=role)\n'
     '        if not who.get("created") and False:      # 变异：死分支回来了\n'
     '            _prove_citizen_binding(conn, who["citizen_id"],\n'
     '                                   session_key=ident["session_key"],\n'
     '                                   bind_token=bind_token)\n'
     '        # 2026-10-29（外部审查 🔴1）：`self:` 认领**不再**要求证明——`session_key` 就是\n',
     ["test_session_rebind_revoke.py::SelfClaimNeedsNoProofTest"
      "::test_source_has_no_dead_false_branch"]),

    # ── P2-6#10：锚 sessions 语义 ────────────────────────────────────
    # 靶点是**整块判定**：只把 `if not ...: return {}` 改成 `if False` 是**行为等价**的
    # （空列表过不了后面的白名单成员检查），所以变异必须还原成旧的那一整段结构。
    ("M-ANCHOR", "空 sessions 又被当成「不限制」（hook 照样传 --citizen）",
     "fish_dsh_hook.py",
     '        sessions = data.get("sessions")\n'
     '        if not isinstance(sessions, list) or not sessions:\n'
     '            return {}\n'
     '        key = ("dsh:" + session_id) if session_id else ""\n'
     '        if not any(key == str(s).strip() for s in sessions):\n'
     '            return {}\n',
     '        sessions = data.get("sessions")\n'
     '        if isinstance(sessions, list) and sessions:      # 变异：空 = 不限制\n'
     '            key = ("dsh:" + session_id) if session_id else ""\n'
     '            if not any(key == str(s).strip() for s in sessions):\n'
     '                return {}\n',
     ["test_session_rebind_revoke.py::IdentityAnchorEmptySessionsTest"
      "::test_empty_sessions_anchor_grants_nothing"]),

    # ── P1-10：缺省只读 ─────────────────────────────────────────────
    ("M-INSTALL", "缺省又变成写（P1-10 原状）",
     "install_mcp.py",
     '    if not args.apply:\n'
     '        # P1-10：**缺省只读**。写是唯一会改宿主配置的动作，必须被显式点名。\n'
     '        return cmd_check(args.host, cfg)\n',
     '    if not args.apply:\n'
     '        return cmd_apply(args.host, cfg)         # 变异：缺省又变成写\n',
     ["test_install_mcp_defaults.py::InstallMcpDefaultReadOnlyTest::test_no_args_writes_nothing"]),

    ("M-GUARD", "删掉家目录护栏（越界路径照写）",
     "install_mcp.py",
     '    refusal = _refuse_unsafe_target(args.host, cfg, explicit=bool(args.file))\n',
     '    refusal = None                             # 变异：护栏被删\n',
     ["test_install_mcp_defaults.py::InstallMcpDefaultReadOnlyTest"
      "::test_refuses_target_outside_home"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(nodes) -> int:
    args = [sys.executable, "-m", "pytest", "-q", *nodes]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(P0))
    return r.returncode


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # noqa: BLE001
            pass

    touched = sorted({m[2] for m in MUTATIONS})
    backups = {name: P0 / (name + ".mrev-bak") for name in touched}
    base = {}
    for name in touched:
        path = P0 / name
        base[name] = sha(path)
        shutil.copy2(path, backups[name])
        print(f"基线 {name} sha256 = {base[name][:16]}…")

    nodes = sorted({n for *_, ns in MUTATIONS for n in ns})
    print(f"\n=== 基线（未变异）应全绿：{len(nodes)} 条判据 ===")
    bad_base = False
    for node in nodes:
        rc = run([node])
        print(f"  {node:78} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        bad_base = bad_base or rc != 0
    if bad_base:
        print("\n基线不全绿，终止（备份在 p0/*.mrev-bak）。")
        for name in touched:
            shutil.copy2(backups[name], P0 / name)
            backups[name].unlink(missing_ok=True)
        return 1

    results = []
    for mid, desc, name, old, new, tests in MUTATIONS:
        path = P0 / name
        src = path.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"\n{mid}: 片段命中 {src.count(old)} 次（必须恰好 1 次），跳过")
            print(f"  想找: {old[:90]!r}")
            results.append((mid, desc, "SKIP"))
            continue
        path.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            rc = run(tests)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"    文件: {name} / 判据: {', '.join(tests)}")
            if rc == 0:
                out = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", *tests],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=str(P0))
                print("    ---- 变异后判据输出（尾部 400 字）----")
                print((out.stdout + out.stderr)[-400:])
            results.append((mid, desc, status))
        finally:
            shutil.copy2(backups[name], path)
            restored = sha(path) == base[name]
            print(f"    还原: {'逐字节一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print(f"    ✗✗ 还原失败，中止！（备份在 {backups[name]}）")
                return 2

    print("\n=== 还原后复跑 ===")
    after_ok = True
    for node in nodes:
        rc = run([node])
        print(f"  {node:78} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        after_ok = after_ok and rc == 0

    print("\n" + "=" * 78)
    for mid, desc, status in results:
        print(f"{mid:12} {status:18} {desc}")
    print("=" * 78)
    all_red = all(s == "红（符合预期）" for _, _, s in results)
    same = all(sha(P0 / name) == base[name] for name in touched)
    print(f"变异全部变红: {'是 ✓' if all_red else '否 ✗'}；"
          f"源码逐字节还原: {'是 ✓' if same else '否 ✗'}；"
          f"复跑全绿: {'是 ✓' if after_ok else '否 ✗'}")
    for name in touched:
        backups[name].unlink(missing_ok=True)
    return 0 if (all_red and same and after_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
