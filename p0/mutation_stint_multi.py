#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「项目档期多项目」7 条判据的变异驱动（2026-10-24）。

做法（与 mutation_injection_status.py 同一套纪律）：
  备份 fish.py → 改**真源码**（每处只改一个片段，要求命中恰好 1 次）→ 跑目标判据
  → 必须红 → 字节还原（sha256 比对）。

为什么用真源码变异而不是 mock.patch：本轮改的正是"库层索引 + 写入口语义"，
mock 掉函数只能证明"代码调用链断了我还绿"，证明不了"库层约束真的在岗"——
判据 A/B/G 要靠真的 SQLite 索引行为变红。

驱动方式：
  python mutation_stint_multi.py            # 全部 7 条
  python mutation_stint_multi.py --only M-C  # 只跑一条
  python mutation_stint_multi.py --list      # 只列清单

判据 ↔ 变异对照
---------------
A 同会话多项目    | M-A 索引退回一列（等于迁移没做）→ 第二个项目插入被拒
B 同项目只一个    | M-B 去掉 reused 分支 → 重复插入（库层拒绝，报 IntegrityError）
C 切项目不关旧档期 | M-C 把"先关后开"加回去 → ANYI 的 to_ts 不再是 NULL
D end 关所有      | M-D 只关最新一条 → stints_closed 变成 1
E start 多项目    | M-E 只处理 projects 的第一个元素 → stints 长度 1
F 单数版兼容      | M-F current_open_stint 直接返回 None
G 旧库迁移        | M-G 迁移不 DROP 旧索引 → 索引仍是一列
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
FISH = HERE / "fish.py"
BAK = FISH.with_name("fish.py.mutation-stint-bak.bak")
TEST_FILE = "test_stint_multi.py"
TEST_CLASS = "StintMultiProjectTest"

# (编号, 守哪条判据, 说明, 原样片段, 改成什么, 目标用例)
MUTATIONS = (
    (
        "M-A", "A", "开档期索引退回一列（等价于迁移没做）",
        '"ON project_stints(citizen_id, project) WHERE to_ts IS NULL")',
        '"ON project_stints(citizen_id) WHERE to_ts IS NULL")',
        "test_A_same_session_multi_projects",
    ),
    (
        "M-B", "B", "去掉 open_project_stint 的同项目复用分支",
        '    for st in current_open_stints(conn, citizen_id):\n'
        '        if st["project"] == project:\n'
        '            return {"action": "reused", "project": project, "stint_id": st["id"],\n'
        '                    "from_ts": st["from_ts"], "closed": 0}\n',
        '    _open_now = current_open_stints(conn, citizen_id)   # 变异：不复用\n',
        "test_B_same_project_single_open",
    ),
    (
        "M-C", "C", "把「先关后开」加回 open_project_stint（本轮最核心的语义回归点）",
        '    from_ts = now_utc()\n'
        '    cur = conn.execute(\n'
        '        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "\n',
        '    close_open_stints(conn, citizen_id, reason="project_switch")   # 变异：先关后开\n'
        '    from_ts = now_utc()\n'
        '    cur = conn.execute(\n'
        '        "INSERT INTO project_stints (citizen_id,account_id,project,from_ts,set_by) "\n',
        "test_C_switch_keeps_old_stint_open",
    ),
    (
        "M-D", "D", "close_open_stints 只关最新一条（end 收不干净）",
        '        "UPDATE project_stints SET to_ts=?, closed_reason=? "\n'
        '        "WHERE citizen_id=? AND to_ts IS NULL",\n',
        '        "UPDATE project_stints SET to_ts=?, closed_reason=? "\n'
        '        "WHERE citizen_id=? AND to_ts IS NULL "\n'
        '        "AND id=(SELECT MAX(id) FROM project_stints s "\n'
        '        "WHERE s.citizen_id=project_stints.citizen_id AND s.to_ts IS NULL)",\n',
        "test_D_end_session_closes_all",
    ),
    (
        "M-E", "E", "start_session 只处理 projects 的第一个元素",
        "        for name in project_list\n",
        "        for name in project_list[:1]   # 变异：只处理第一个项目\n",
        "test_E_start_session_multi_projects",
    ),
    (
        "M-F", "F", "current_open_stint 单数版直接返回 None（兼容层失效）",
        "    stints = current_open_stints(conn, citizen_id)\n"
        "    return stints[-1] if stints else None\n",
        "    _ = current_open_stints(conn, citizen_id)\n"
        "    return None   # 变异：单数版直接失效\n",
        "test_F_current_open_stint_compat",
    ),
    (
        "M-G", "G", "迁移时不 DROP 旧的一列索引（只靠 CREATE IF NOT EXISTS）",
        "    if have:\n"
        "        # 存在但列不对（旧库一列版 / 更早的实验版）→ 必须先删掉旧定义\n"
        "        conn.execute(f\"DROP INDEX IF EXISTS {_STINT_OPEN_INDEX_NAME}\")\n",
        "    if have:\n"
        "        pass   # 变异：不删旧索引，只靠 CREATE IF NOT EXISTS\n",
        "test_G_legacy_one_column_index_migrated",
    ),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(name: str) -> tuple:
    """跑一条判据。返回 (是否全绿, 输出尾部)。用退出码判定：0=绿。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", f"{TEST_FILE}::{TEST_CLASS}::{name}"],
        cwd=str(HERE), capture_output=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = " / ".join(ln.strip() for ln in out.strip().splitlines()[-4:] if ln.strip())
    return proc.returncode == 0, tail


def apply_mutation(old: str, new: str) -> SourceText:
    """改源码，返回**改之前的字节句柄**（供还原）。命中次数必须恰好 1，否则抛错不写。

    读写纪律（2026-10-24 踩过，2026-10-26 F1 又踩一次）：**不能用 Path.read_text /
    write_text**，也不能用 `read_bytes().decode()` 直接匹配——前者写回时会把 `\\n`
    翻成 `\\r\\n`（字节还原必失败），后者在 CRLF 树上命中 0 次（整条变异被跳过）。
    统一走 `mutation_source.SourceText`：原字节先存、匹配在 LF 归一视图上做、
    写回保持原风格、还原走 `restore()`（逐字节一致）。
    """
    src = SourceText(FISH)
    src.write(src.replaced(old, new))
    return src


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="项目档期多项目判据的变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-C）")
    parser.add_argument("--list", action="store_true", help="只列清单，不执行")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，别让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for mut_id, judgment, note, _o, _n, test in MUTATIONS:
            print(f"{mut_id:<6}{judgment:<4}{test:<42}{note}")
        return 0

    base = sha256(FISH)
    print(f"基线 fish.py：{len(FISH.read_bytes())} 字节 / sha256 {base[:16]}…")
    shutil.copy2(FISH, BAK)
    print(f"备份：{BAK.name}\n")

    # 环境自证：未变异时必须全绿，否则"变异后红"毫无意义
    print("=== 基线（未变异）应全绿 ===")
    baseline_ok = True
    for mut_id, judgment, _note, _old, _new, test in MUTATIONS:
        if args.only and args.only != mut_id:
            continue
        green, tail = run_test(test)
        print(f"  [{judgment}] {test:<42} {'绿 ✓' if green else '红 ✗ ' + tail}")
        if not green:
            baseline_ok = False
    if not baseline_ok:
        print("\n基线不全绿——先修环境/判据，再看变异。")
        BAK.unlink(missing_ok=True)
        return 1

    rows = []
    failures = []
    for mut_id, judgment, note, old, new, test in MUTATIONS:
        if args.only and args.only != mut_id:
            continue
        original = None
        try:
            original = apply_mutation(old, new)
            green, tail = run_test(test)
        except Exception as exc:                  # noqa: BLE001 —— 任何异常都要先还原
            failures.append(f"{mut_id} 执行异常：{exc}")
            rows.append((mut_id, judgment, "异常", False, note, str(exc)))
            continue
        finally:
            if original is not None:
                original.restore()
        restored = sha256(FISH) == base
        if not restored:
            print(f"[X] {mut_id} 还原失败！用 历史版本\\fish.py.20261024-1940-stints.bak 手工恢复")
            return 2
        red = green is False
        status = "红（符合预期）" if red else "[X] 仍是绿（无判别力）"
        print(f"\n{mut_id} [判据 {judgment}] {status}")
        print(f"   变异：{note}")
        print(f"   目标：{TEST_FILE}::{TEST_CLASS}::{test}")
        print(f"   结果：{tail}")
        print("   还原：sha256 与基线一致 ✓")
        if not red:
            failures.append(f"{mut_id} 变异后判据仍绿")
        rows.append((mut_id, judgment, status, red, note, test))

    print("\n" + "=" * 96)
    for mut_id, judgment, status, red, note, test in rows:
        print(f"{mut_id:<6}{judgment:<5}{'红/预期' if red else '[X] 意外':<12}{test:<42}{note}")
    print("=" * 96)
    print(f"最终 fish.py sha256 {sha256(FISH)[:16]}…"
          f"（{'与基线一致' if sha256(FISH) == base else '[X] 与基线不一致'}）")
    BAK.unlink(missing_ok=True)
    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n全部 {len(rows)} 条变异：改真源码后判据全红、字节已还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
