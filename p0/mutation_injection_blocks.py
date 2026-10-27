#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把「规矩 / 该注意什么」两段逐条改坏，看判据是否变红。

驱动方式（改**真源码** → 跑指定判据 → 必红 → 字节级还原）：
    python mutation_injection_blocks.py

变异体（每条判据至少一个，且都改的是"实现里最可能被写错的那一句"）：
  M-A1  规矩查询改回按 owner（作者）筛                  → 判据 A′ 红（别的项目的规矩被选中）
  M-A2  全局栏不查 `project IS NULL`（写成 `project = ?`）→ 判据 A2 红（全局规矩漏掉）
  M-A3  全局栏无条件输出表头                            → 判据 A3 红（无全局规矩也出现「全局规矩」）
  M-B1  规矩段把**正文**整段塞进去                      → 判据 B 红（段长 > 200 字符）
  M-B′  lesson 排序不看项目（只按时间，上一版口径）      → 判据 B′ 红（取到跨项目的坑）
  M-B2  lesson 不做跨项目补齐（同项目不足就空着）        → 判据 B2 红（只印 1 条）
  M-C1  规矩段无条件输出表头                            → 判据 C 红（无 rule 也出现"规矩（"）
  M-D1  未闭合不筛标题（`1 = 1`）                        → 判据 D 红（普通事实也成未闭合）
  M-E1  C 类自己拼同类范围（不用 S1 的 scope）          → 判据 E 红（该出现的没出现）
  M-E2  把 S1 的门槛常量 3 改成 2（模拟两份实现漂移）    → 判据 E 红（低于门槛的那条冒出来）
  M-F1  注意段无条件输出表头                            → 判据 F 红（三类都空也出现）
  M-G1  裁剪顺序里把「规矩」提到最前                    → 判据 G 红（L2 还在就砍规矩）
  M-H1  meta 不记 rules_codes                            → 判据 H 红（包里印了但 meta 空）

纪律（与 mutation_onboarding.py 同一套）：每个变异体跑完**立刻**从备份逐字节还原并
校验 sha256；还原失败立即中止。**只碰 tempdir 的测试库**，不碰生产库。
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent
FISH = P0 / "fish.py"
BAK = P0 / "fish.py.mib-bak"
TEST_FILE = P0 / "test_injection_blocks.py"
TEST_CLASS = "InjectionBlocksTest"

# (编号, 描述, 旧片段, 新片段, 判据)
MUTATIONS = [
    ("M-A1", "规矩查询改回按 owner（作者）筛——2026-10-25 已证伪的口径",
     '    key = (project or "").strip() or None\n'
     '    if key is None:\n'
     '        clause, where_params = "project IS NULL", []\n'
     '    else:\n'
     '        clause, where_params = "project = ?", [key]\n',
     '    # 变异：按 owner（作者）筛规矩——owner 是"谁写的"，不是"约束谁"\n'
     '    clause, where_params = "owner = ?", [resolve_owner(conn)]\n',
     ["test_A_rules_by_scope_not_author"]),

    ("M-A2", "全局栏不查 project IS NULL（写成 project = ?）",
     '    if key is None:\n'
     '        clause, where_params = "project IS NULL", []\n',
     '    if key is None:\n'
     '        clause, where_params = "project = ?", [key]   # 变异：不查 NULL，全局规矩全漏\n',
     ["test_A2_global_rule_in_global_section"]),

    ("M-A3", "全局栏无条件输出表头（没有全局规矩也印「全局规矩：」）",
     '        if global_list:\n'
     '            lines.append("全局规矩：")\n',
     '        if True:                       # 变异：无条件输出全局栏表头\n'
     '            lines.append("全局规矩：")\n',
     ["test_A3_no_global_section_when_empty"]),

    ("M-B1", "规矩段把正文整段塞进去（应该只取标题）",
     '    rules = build_short_rules_block(conn, project=proj_name)\n',
     '    def _rules_with_full_text(conn, project=None):\n'
     '        # 变异：把正文整段塞进规矩段（正确实现只取 code+标题）\n'
     '        rows = conn.execute(\n'
     '            "SELECT code,title,file_path FROM code_index WHERE mem_type=\'rule\' "\n'
     '            "AND status=\'active\' AND dormant=0 "\n'
     '            "ORDER BY recorded_at DESC, code DESC LIMIT 3").fetchall()\n'
     '        lines = [f"本项目规矩（{project}）："]\n'
     '        codes = []\n'
     '        for _code, _title, _fp in rows:\n'
     '            _body = ""\n'
     '            if _fp and (Path(root_s) / _fp).exists():\n'
     '                _body = (Path(root_s) / _fp).read_text(encoding="utf-8")\n'
     '            lines.append(f"- {_code} {_title} {_body}")\n'
     '            codes.append(_code)\n'
     '        return {"text": "\\n".join(lines) + "\\n", "codes": codes, "trimmed": []}\n'
     '\n'
     '    rules = _rules_with_full_text(conn, project=proj_name)\n',
     ["test_B_rules_only_carry_titles"]),

    ("M-B′", "lesson 排序不看项目（只按时间——上一版口径）",
     '    out = _lesson_rows(conn, since_z, limit, exclude, same_project=key) if key else []\n',
     '    out = (_lesson_rows(conn, since_z, limit, exclude, same_project=None)\n'
     '           if key else [])      # 变异：只按时间排，不看是否同项目\n',
     ["test_B_prime_lessons_same_project_first"]),

    ("M-B2", "lesson 不做跨项目补齐（同项目不足就空着）",
     '    if len(out) < limit:\n',
     '    if False:                      # 变异：严格只取同项目，不足不补\n',
     ["test_B2_lessons_cross_project_fill"]),

    ("M-C1", "规矩段无条件输出表头（空库也出现「规矩（」）",
     '        if proj_list:\n'
     '            lines.append(f"本项目规矩（{proj_key}）：")\n',
     '        if True:                       # 变异：无条件输出表头\n'
     '            lines.append(f"本项目规矩（{proj_key}）：")\n',
     ["test_C_no_rules_no_section"]),

    ("M-D1", "未闭合不筛标题（所有 active 记忆都算未闭合）",
     '    cond = " OR ".join("INSTR(LOWER(COALESCE(title,\'\')), ?) > 0"\n'
     '                       for _ in ATTENTION_OPEN_KEYWORDS)\n'
     '    sql = ("SELECT code,title FROM code_index WHERE status=\'active\' AND dormant=0 "\n'
     '           "AND recorded_at >= ? AND (" + cond + ") "\n'
     '           "ORDER BY recorded_at DESC, code DESC LIMIT ?")\n'
     '    params = [since_z] + [k.lower() for k in ATTENTION_OPEN_KEYWORDS]\n',
     '    cond = "1 = 1"                     # 变异：不筛标题\n'
     '    sql = ("SELECT code,title FROM code_index WHERE status=\'active\' AND dormant=0 "\n'
     '           "AND recorded_at >= ? AND (" + cond + ") "\n'
     '           "ORDER BY recorded_at DESC, code DESC LIMIT ?")\n'
     '    params = [since_z]\n',
     ["test_D_open_loop_matched_by_title"]),

    ("M-E1", "C 类自己拼同类范围（不再复用 S1 解析出的 scope）",
     '    try:\n'
     '        return _knowledge_gaps(conn, scopes, label, since_z, int(limit))\n'
     '    except sqlite3.Error:\n'
     '        return []\n',
     '    # 变异：自己拼一份同类范围（只拿会话名当账号）——S1 用 citizen_id 展开\n'
     '    try:\n'
     '        return _knowledge_gaps(conn, [{"basis": "actor", "accounts": {label},\n'
     '                                       "projects": set()}], label, since_z, int(limit))\n'
     '    except sqlite3.Error:\n'
     '        return []\n',
     ["test_E_high_value_gap_reuses_s1_threshold"]),

    ("M-E2", "把 S1 的门槛常量 3 改成 2（两份实现漂移的最小形态）",
     'KNOWLEDGE_GAP_MIN_PEER_READS = 3     # 同类其他会话累计读过 ≥3 次才算"别人常读"\n',
     'KNOWLEDGE_GAP_MIN_PEER_READS = 2     # 变异：门槛被改小\n',
     ["test_E_high_value_gap_reuses_s1_threshold"]),

    ("M-F1", "注意段无条件输出表头（三类都空也出现）",
     '        return (("该注意什么：\\n" + "\\n".join(lines) + "\\n") if lines else ""), codes\n',
     '        return ("该注意什么：\\n" + "\\n".join(lines) + "\\n"), codes   # 变异：无条件\n',
     ["test_F_no_attention_no_section"]),

    ("M-G1", "裁剪顺序里把「规矩」提到最前（先砍规矩）",
     # 靶点只取 **L2 那个循环本身**（2026-10-26 修）：上一版把「L2 循环」与
     # 「注意事项循环」两段拼成一个片段，而两者之间现在夹着一整段解释裁剪
     # 顺序的注释（`fish.py:7641-7658`）——注释一改，靶点就失配、整条变异被 SKIP。
     # 纪律：靶点落在**紧邻的语句**上，绝不跨过可自由改写的注释区。
     '    while total() > budget and l2:\n'
     '        l2 = ""\n'
     '        trimmed.append("L2")\n',
     '    while total() > budget and rules_text:      # 变异：先砍规矩\n'
     '        rules_text = ""\n'
     '        trimmed.append("规矩")\n'
     '    while total() > budget and l2:\n'
     '        l2 = ""\n'
     '        trimmed.append("L2")\n',
     ["test_G_trim_order_drops_index_before_rules"]),

    ("M-H1", "meta 不记 rules_codes（包里印了但 meta 空）",
     '        "rules_codes": list(rules["codes"]) if rules_text else [],\n',
     '        "rules_codes": [],                  # 变异：不记\n',
     ["test_H_meta_records_printed_codes"]),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run(tests) -> int:
    args = [sys.executable, str(TEST_FILE)] + [f"{TEST_CLASS}.{t}" for t in tests]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(P0))
    return r.returncode


def main() -> int:
    # 与 fish.py 的 main 同一处置：无控制台/计划任务环境下 stdout 是 GBK，
    # ✓/✗ 会抛 UnicodeEncodeError（"功能正常但退出码 1"的假失败）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    base = sha(FISH)
    print(f"基线 fish.py sha256 = {base[:16]}…")
    shutil.copy2(FISH, BAK)

    all_tests = sorted({t for *_, tests in MUTATIONS for t in tests})
    print(f"\n=== 基线（未变异）应全绿：{len(all_tests)} 条判据 ===")
    bad_base = False
    for t in all_tests:
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        bad_base = bad_base or rc != 0
    if bad_base:
        print("\n基线不全绿，终止（备份在 p0/fish.py.mib-bak）。")
        shutil.copy2(BAK, FISH)
        BAK.unlink(missing_ok=True)
        return 1

    results = []
    for mid, desc, old, new, tests in MUTATIONS:
        src = FISH.read_text(encoding="utf-8")
        if src.count(old) != 1:
            print(f"\n{mid}: 片段命中 {src.count(old)} 次（必须恰好 1 次），跳过")
            print(f"  想找: {old[:90]!r}")
            results.append((mid, desc, "SKIP"))
            continue
        FISH.write_text(src.replace(old, new, 1), encoding="utf-8")
        try:
            rc = run(tests)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"    判据: {', '.join(tests)}")
            if rc == 0:
                # 变异没被抓住时，把子进程输出打出来，便于定位到底是判据太松还是变异没生效
                out = subprocess.run(
                    [sys.executable, str(TEST_FILE)]
                    + [f"{TEST_CLASS}.{t}" for t in tests],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", cwd=str(P0))
                print("    ---- 变异后判据输出（尾部 400 字）----")
                print((out.stdout + out.stderr)[-400:])
            results.append((mid, desc, status))
        finally:
            shutil.copy2(BAK, FISH)
            restored = sha(FISH) == base
            print(f"    还原: {'逐字节一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print("    ✗✗ 还原失败，中止！（备份在 p0/fish.py.mib-bak）")
                return 2

    print("\n=== 还原后复跑 ===")
    after_ok = True
    for t in all_tests:
        rc = run([t])
        print(f"  {t:52} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        after_ok = after_ok and rc == 0

    print("\n" + "=" * 78)
    for mid, desc, status in results:
        print(f"{mid:7} {status:18} {desc}")
    print("=" * 78)
    all_red = all(s == "红（符合预期）" for _, _, s in results)
    final_ok = sha(FISH) == base
    print(f"变异全部变红: {'是 ✓' if all_red else '否 ✗'}；"
          f"源码逐字节还原: {'是 ✓' if final_ok else '否 ✗'}；"
          f"复跑全绿: {'是 ✓' if after_ok else '否 ✗'}")
    BAK.unlink(missing_ok=True)
    return 0 if (all_red and final_ok and after_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
