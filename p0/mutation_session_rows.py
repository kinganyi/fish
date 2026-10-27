#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 撞车自撞修复（设计 45 §7.2）。

## 为什么要有它

「判据全绿」本身不能证明判据有判别力：实现改坏之后判据照样绿，说明它测的不是那件事。
所以每一条关键判据都要配一个**真源码变异**：把实现改回旧行为 → 指定判据必红 →
**其余新判据文件不许新红**（否则说明变异把别的东西一起打坏了，红的原因不可信）→
逐字节还原 → sha256 与基线一致。

驱动方式：

    cd p0
    python mutation_session_rows.py          # exit 0 = 9 个变异体全部被抓住

## 与设计 §7.2 的两处偏差（都写在注释里）

  · **M2 的靶点判据**：设计写的是 S11（`test_own_session_excluded`），但那条用例里
    **只有我自己那一行**在场——"按公民排除"退化成"按会话排除"，同一个答案，抓不到变异
    （实测：变异后它仍然绿）。真正有判别力的是 S12：同公民的**另一个会话**也必须出现在
    快照里（那时按公民排除会把它吞掉）。
  · **M6 的靶点判据**：设计写 S1/S18。S13 抓不到它——S13 读的是**快照**（`fish_activity`
    侧），而 M6 改的是 `board_presence_rows`（渲染侧）。S1 断言 `session_key_source`，
    正好钉住"列被优先读"这件事。
  · **M5 的 `others`**：只要求 schema / rows 保持绿。碰撞判据（S5/S10/S12）**也**断言标签
    里带 `·dsh#`——那是同一条不变量的第二处覆盖，不是"被变异打坏了"，所以它跟着红是
    预期内的（写在 `expected_red_files` 里，不掩盖）。

## 退出码

    0  全部按预期（变异被抓、其余文件仍绿、源码逐字节还原）
    1  基线不是全绿（判据本身有问题，先修判据）
    2  某个变异体没被抓住 / 其余文件出现新红 / 还原失败
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 本机控制台默认是 gbk（PowerShell 5.1），而本脚本要打「✓/✗」：显式切 utf-8，
# 否则重定向到文件时会 UnicodeEncodeError（"功能正常但脚本崩"的假失败）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from mutation_source import SourceText  # noqa: E402

NEW_FILES = {
    "schema": "test_session_schema",
    "rows": "test_session_rows",
    "collision": "test_session_collision",
    "render": "test_session_render",
    "qa": "test_session_qa_fixes",
}

MUTANTS = [
    {
        "id": "M1",
        "file": "fish.py",
        "desc": "我的声明面 SQL 去掉会话过滤（同公民另一会话的声明被当成我的）",
        "old": '''            sk_filter = (" AND (p.session_key=?"
                         " OR (p.session_key='' AND p.agent_id=?))"
                         if (my_session and has_sk) else "")''',
        "new": '''            sk_filter = ""''',
        "must_fail": ["test_session_collision.PushCandidateTest."
                      "test_S9_other_session_declaration_is_not_mine"],
        "others": ["schema", "rows", "render"],
    },
    {
        "id": "M2",
        "file": "fish_activity.py",
        "desc": "find_collisions 的排除改成按 citizen_id（同公民另一会话被吞掉）",
        "old": '''        if own_session_key and entry_session == own_session_key:
            continue''',
        "new": '''        own_citizen = ""
        if own_session_key:
            for other_entry in entries:
                if (isinstance(other_entry, dict)
                        and str(other_entry.get("session_key") or "") == own_session_key):
                    own_citizen = str(other_entry.get("citizen_id") or "")
                    break
        if own_session_key and entry.get("citizen_id") and str(
                entry.get("citizen_id")) == own_citizen:
            continue''',
        "must_fail": [
            "test_session_collision.SnapshotJudgeTest."
            "test_S12_find_collisions_reports_other_session",
        ],
        "others": ["schema", "rows", "render"],
    },
    {
        "id": "M3",
        "file": "fish_activity.py",
        "desc": "快照 session_key 改回 agent_id 推断（列被忽略）",
        "old": '''            "session_key": sk,''',
        "new": '''            "session_key": agent if agent and ":" in str(agent) else "",''',
        "must_fail": ["test_session_collision.SnapshotJudgeTest."
                      "test_S13_snapshot_key_comes_from_column"],
        "others": ["schema", "rows", "render"],
    },
    {
        "id": "M4",
        "file": "fish.py",
        "desc": "回填条件放宽成「非空 agent_id 就回填」（伪造会话键）",
        "old": '''_PRESENCE_SESSION_BACKFILL = (
    "CASE WHEN agent_id IS NOT NULL AND ("
    " agent_id GLOB 'dsh:?*' OR agent_id GLOB 'codebuddy:?*'"
    " OR agent_id GLOB 'workbuddy:?*' OR agent_id GLOB 'cli:?*'"
    " OR agent_id GLOB 'self:?*') THEN agent_id ELSE '' END"
)''',
        "new": '''_PRESENCE_SESSION_BACKFILL = (
    "CASE WHEN agent_id IS NOT NULL AND agent_id != '' THEN agent_id ELSE '' END"
)''',
        "must_fail": ["test_session_schema.MigrationTest.test_S15_backfill_rules"],
        "others": ["rows", "collision", "render"],
    },
    {
        "id": "M5",
        "file": "fish_naming.py",
        "desc": "session_labels 去掉会话后缀（永远用公民名）",
        # 靶点随 QA R2-C1（双层 host 吸收）更新：原来是单行拼接，现在有 if/else 两支。
        # 改成 `if False` 比改拼接行更稳——'永远不加后缀' 是这条变异的本意。
        "old": '''        if len(sessions.get(cid) or ()) >= 2:
            short = shorts.get((cid, sk)) or NO_SESSION_SHORT''',
        "new": '''        if False:  # M5 变异：永远用公民名（不加会话后缀）
            short = shorts.get((cid, sk)) or NO_SESSION_SHORT''',
        "must_fail": ["test_session_render.SessionLabelsTest."
                      "test_S19_two_sessions_get_distinct_short_labels"],
        # 同一条不变量的第二/第三处覆盖：`test_session_collision` 与
        # `test_session_rows` 的断言里也带「标签含会话短标识」——它们跟着红是
        # **预期内**的（不掩盖），所以不列进 `others`。
        "others": ["schema"],
        "expected_red_files": ["collision", "rows"],
    },
    {
        "id": "M6",
        "file": "fish.py",
        "desc": "board_presence_rows 的 SELECT 不带 session_key（只剩旧列兜底）",
        "old": '''    sk_col = "p.session_key" if _presence_has_session(conn) else "''"''',
        "new": '''    sk_col = "''"''',
        "must_fail": [
            "test_session_rows.TwoSessionsSameCitizenTest.test_S1_two_sessions_two_rows",
        ],
        "others": ["schema", "collision", "render"],
    },
    {
        "id": "M7",
        "file": "fish.py",
        "desc": "注入包不折叠（直接取前 3 行 → 同公民的会话把别人挤出去）",
        "old": '''        for p in _fold_presence_for_inject(ordered, limit=3):''',
        "new": '''        for p in ordered[:3]:''',
        "must_fail": ["test_session_render.InjectFoldTest."
                      "test_S20_same_citizen_folds_into_one_cell"],
        "others": ["schema", "rows", "collision"],
    },
    {
        "id": "M8",
        "file": "fish_activity.py",
        "desc": "去掉 F1 双读（entry 只读 session_key 列）",
        "old": '''    col = str(session_key or "").strip()
    legacy = str(agent_id or "").strip() if fish_naming.is_session_key(agent_id) else ""
    if col:
        return col, "column"
    if legacy:
        return legacy, "agent_id:legacy"
    return "", "none"''',
        "new": '''    col = str(session_key or "").strip()
    return col, ("column" if col else "none")''',
        "must_fail": [
            "test_session_collision.SnapshotJudgeTest."
            "test_S25_legacy_agent_id_fallback_is_observable",
            "test_pretool_ask.ExclusionTest.test_own_session_excluded",
        ],
        "others": ["schema", "rows", "render"],
    },
    {
        "id": "M9",
        "file": "fish_activity.py",
        "desc": "F2 第三档退让成公民级（取不到就借同公民别的会话的面）",
        "old": '''    if session_key:
        return by_session.get((citizen_id, session_key)) or {}
    return by_citizen.get(citizen_id) or {}''',
        "new": '''    if session_key:
        return by_session.get((citizen_id, session_key)) or by_citizen.get(citizen_id) or {}
    return by_citizen.get(citizen_id) or {}''',
        "must_fail": ["test_session_collision.ActualSurfacesTest."
                      "test_S26_session_without_activity_gets_no_borrowed_dirs"],
        "others": ["schema", "rows", "render"],
    },
    # M10/M12 是 QA Round 1 后补的（原因：42 条新判据全绿却漏掉 F1/F3，
    # 说明「缺的判据 = 缺的防护」；这两条变异体正好钉住新补的 S28/S31）。
    # M11（多会话判定改全局行数）**不可实现**：S29 的"板内"语义由调用方
    # （`board_presence_rows(conn, board)` 只查单板）天然保证，纯函数内部没有
    # board_id 可看——详见 45 §7.2 的订正说明。
    {
        "id": "M10",
        "file": "fish_naming.py",
        "desc": "session_labels 去掉 citizen 去重（同公民多会话 → 假 #N）",
        "old": '''    base = disambiguate(unique_rows)''',
        "new": '''    base = disambiguate([{"citizen_id": cid, "display_name": name, "host": host,
                          "created_at": created}
                         for cid, _sk, name, host, created in prepared])''',
        "must_fail": ["test_session_qa_fixes.S28NoFakeHashTest."
                      "test_S28_three_boards_same_identity_no_fake_hash"],
        "others": ["schema", "rows", "collision", "render"],
    },
    {
        "id": "M12",
        "file": "fish_activity.py",
        "desc": "去掉快照跨板合并（同身份键多板重复 → 同一 label 出现多次）",
        "old": '''    entries = _merge_entries_by_identity(entries)''',
        "new": '''    entries = list(entries)''',
        "must_fail": ["test_session_qa_fixes.S31CrossBoardMergeTest."
                      "test_S31_snapshot_merges_same_identity_across_boards"],
        "others": ["schema", "rows", "collision", "render"],
    },
]


LOG_DIR = HERE / "_audit" / "mutation_session_rows"


def _run(*specs) -> int:
    return _run_detail(*specs)[0]


def _run_detail(*specs):
    """跑一组 unittest，返回 `(退出码, stderr 尾巴)`（尾巴只用于解释"为什么红"）。"""
    cmd = [sys.executable, "-m", "unittest", *specs]
    # 子进程也钉 utf-8：判定只认退出码，但断言失败的中文消息若撞上 gbk 会变成
    # UnicodeEncodeError（另一个退出码）——那会让"红"这件事说不清原因。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=900)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    name = "-".join(spec.split(".")[-1] for spec in specs)[:80]
    (LOG_DIR / f"{name}.log").write_text(
        f"$ python -m unittest {' '.join(specs)}\nexit={proc.returncode}\n\n"
        f"---- stdout ----\n{proc.stdout}\n---- stderr ----\n{proc.stderr}",
        encoding="utf-8")
    return proc.returncode, proc.stderr.strip().splitlines()[-6:]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    only = None
    if "--only" in argv:
        index = argv.index("--only")
        only = (argv[index + 1] if index + 1 < len(argv) else "").upper()
    print("=" * 74)
    print(f"撞车自撞修复 · 变异自证（{len(MUTANTS)} 个变异体；M11 不可实现见注释）"
          + (f" ｜ 只跑 {only}" if only else ""))
    print("=" * 74)
    print("\n[0/3] 基线：四个新判据文件必须全绿")
    for name, module in NEW_FILES.items():
        rc = _run(module)
        print(f"  {module:26} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            print("\n基线就不是全绿——判据本身有问题，终止。", file=sys.stderr)
            return 1

    failures = []
    print("\n[1/3] 逐个变异 → 指定判据必红 → 其余文件不许新红 → 逐字节还原")
    for mutant in MUTANTS:
        if only and mutant["id"] != only:
            continue
        path = HERE / mutant["file"]
        src = SourceText(path)
        base_sha = _sha(path)
        try:
            try:
                src.write(src.replaced(mutant["old"], mutant["new"]))
            except AssertionError as exc:
                print(f"\n{mutant['id']} 靶点失效（源码形态变了）：{exc}", file=sys.stderr)
                failures.append((mutant["id"], "靶点失效"))
                continue
            caught = True
            details = []
            for spec in mutant["must_fail"]:
                rc, tail = _run_detail(spec)
                details.append(f"{spec.split('.')[-1]}={'红 ✓' if rc != 0 else '绿 ✗'}")
                if rc == 0:
                    caught = False
                    print(f"\n    ↳ {spec} 变异后仍然绿（判据没有判别力）：{tail}")
            clean = True
            for key in mutant["others"]:
                rc, tail = _run_detail(NEW_FILES[key])
                if rc != 0:
                    clean = False
                    details.append(f"{NEW_FILES[key]}=新红 ✗")
                    print(f"\n    ↳ {NEW_FILES[key]} 出现**预期外**的新红：{tail}")
            print(f"\n{mutant['id']} {'CAUGHT' if caught and clean else 'MISSED'} "
                  f"{mutant['desc']}")
            print(f"    {mutant['file']} ｜ " + "，".join(details))
            for key in mutant.get("expected_red_files") or ():
                rc = _run(NEW_FILES[key])
                print(f"    （同一条不变量的第二处覆盖：{NEW_FILES[key]} "
                      f"{'也红 ✓' if rc != 0 else '却绿 ✗'}，预期内）")
                if rc == 0:
                    clean = False
            if not caught:
                failures.append((mutant["id"], "指定判据没红"))
            if not clean:
                failures.append((mutant["id"], "其余新判据文件出现新红"))
        finally:
            src.restore()
            restored = _sha(path) == base_sha
            print(f"    还原：sha256 {'一致 ✓' if restored else '不一致 ✗'}（{path.name}）")
            if not restored:
                print("    ✗✗ 还原失败，立即中止！", file=sys.stderr)
                return 2

    print("\n[2/3] 还原后复跑（应全绿）")
    for name, module in NEW_FILES.items():
        rc = _run(module)
        print(f"  {module:26} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            failures.append((module, "还原后仍红"))

    print("\n[3/3] 结论")
    print("=" * 74)
    ran = [m["id"] for m in MUTANTS if not only or m["id"] == only]
    if failures:
        for mid, why in failures:
            print(f"  {mid}: {why}", file=sys.stderr)
        print(f"结果：{len(failures)} 项不符 —— 变异自证 **未通过**", file=sys.stderr)
        return 2
    print(f"结果：{len(ran)}/{len(ran)} 变异体全部被抓住，其余新判据文件无新红")
    print("exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
