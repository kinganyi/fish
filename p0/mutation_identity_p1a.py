#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
身份层 P1-a（纯结构）· 变异自证驱动
=====================================
用途：**改真源码 → 跑 → 必红 → 字节还原**。每条判据配一条变异，一条都不许"照绿"。

为什么不只靠 test_identity_p1a.py 里的 `*_mutation_*` 用例：
  那些用例用 mock.patch / 字符串手术模拟"摘掉一处"，可重跑、进 CI，但**不等于**真删源码
  ——mock 可能掩盖"摘掉之后代码路径根本没走到"。这个脚本改的是磁盘上的 fish.py **字节**，
  用独立的 pytest 进程跑目标判据，证明的是"真把这段删了，那条判据真的会红"。
两层互为补充：CI 守"判据有判别力"，本脚本守"删源码真的会红"。

纪律：
  · **换行无关**：匹配在 LF 归一视图上做、写回保持文件原本风格（工作树是 CRLF，
    见 `mutation_source.py`；2026-10-26 F1 前这里是 0 命中的假失败）；
  · 每改一次都比对 sha256，结束后**必须**逐字节还原并复核（还原走原始字节）；
  · 任何异常（进程崩溃 / Ctrl-C）都走 finally 还原；
  · 还原失败 → 立刻非零退出并打印人工恢复步骤（备份在 历史版本\\）。

用法：
    python mutation_identity_p1a.py            # 全跑（约 2 分钟）
    python mutation_identity_p1a.py --only M-C # 只跑一条
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
TARGET = HERE / "fish.py"
TEST_FILE = "test_identity_p1a.py"
TEST_CLASS = "FishIdentityP1aTest"

# 一条变异 = (编号, 守哪条判据, 目标用例, 原样片段, 改成什么, 说明)
MUTATIONS = (
    (
        "M-A1", "A", "test_A_fresh_db_structure_complete",
        "_IDENTITY_TABLE_DDLS = (_CITIZENS_ARCHIVE_DDL, _AGENT_ACCOUNTS_DDL,\n",
        "_IDENTITY_TABLE_DDLS = (_CITIZENS_ARCHIVE_DDL,\n",
        "把 agent_accounts 的建表 DDL 从共用清单里删掉（新库/升级两条路径都没了）",
    ),
    (
        "M-A2", "A", "test_A_fresh_db_structure_complete",
        '    "archived_at",\n)\n\n\ndef _citizens_all_view_ddl(',
        ")\n\n\ndef _citizens_all_view_ddl(",
        "citizens_all 的列契约少一列（视图只剩 12 列，与热表不再对齐）",
    ),
    (
        "M-B", "B", "test_B_legacy_db_gains_columns",
        "_CITIZENS_NEW_COLS = (\n",
        "_CITIZENS_NEW_COLS = ()\n_DEAD_NEW_COLS = (\n",
        "摘掉 citizens 的 5 列补列清单（旧库升不上 13 列）",
    ),
    (
        "M-C", "C", "test_C_view_sees_hot_row",
        'f"SELECT {cols} FROM citizens "\n'
        '            f"UNION ALL SELECT {cols} FROM citizens_archive")',
        'f"SELECT {cols} FROM citizens_archive "\n'
        '            f"UNION ALL SELECT {cols} FROM citizens_archive")',
        "视图只剩归档表那一条 SELECT（热表里的行查不到了）",
    ),
    (
        "M-D", "D", "test_D_archived_row_visible_once",
        'f"UNION ALL SELECT {cols} FROM citizens_archive")',
        'f"UNION ALL SELECT {cols} FROM citizens_archive "\n'
        '            f"UNION ALL SELECT {cols} FROM citizens_archive")',
        "视图把归档分支写了两遍（同一行被列出两次：搬迁假成功会被放大成重复行）",
    ),
    (
        "M-E", "E", "test_E_open_stint_unique",
        '    "CREATE UNIQUE INDEX IF NOT EXISTS ux_stint_open "\n'
        '    "ON project_stints(citizen_id, project) WHERE to_ts IS NULL")',
        '    "SELECT 1")   # 变异：唯一索引不再建（第二个开档期不再被拒）',
        "唯一索引不再建（等价于旧版『索引清单里删掉 ux_stint_open』）",
    ),
    (
        "M-F1", "F", "test_F_session_token_checks",
        "  CHECK (channel != 'parent' OR parent_token IS NOT NULL),\n",
        "",
        "删掉「parent 必带 parent_token」那条 CHECK",
    ),
    (
        "M-F2", "F", "test_F_session_token_checks",
        "  CHECK (channel != 'parent' OR parent_token IS NOT NULL),\n"
        "  CHECK (channel != 'parent' OR evidence = 'inherited')\n",
        "  CHECK (channel != 'parent' OR parent_token IS NOT NULL)\n",
        "只删第 4 条 CHECK（parent 渠道可以贴 machine 标签 = 越权入口）",
    ),
    (
        "M-G", "G", "test_G_existing_queries_untouched",
        "\"SELECT citizen_id FROM citizens WHERE is_owner=1 AND status='active'\"",
        "\"SELECT citizen_id FROM citizens WHERE is_owner=1\"",
        "改掉一处既有查询（resolve_owner 的 owner 有效性判定）",
    ),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(test_name: str) -> tuple:
    """跑目标用例，返回 (是否通过, 最后一行输出)。判据"红"= 不通过（失败或报错都算）。"""
    node = f"{TEST_FILE}::{TEST_CLASS}::{test_name}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header"],
        cwd=str(HERE), capture_output=True)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    tail = lines[-1] if lines else "(无输出)"
    return proc.returncode == 0, tail[:160]


def apply_mutation(old: str, new: str) -> SourceText:
    """就地改源码，返回**改之前的字节句柄**（还原走 `restore()`，逐字节一致）。

    找不到片段/多处命中就抛错，绝不静默跳过。
    """
    src = SourceText(TARGET)
    src.write(src.replaced(old, new))
    return src


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="身份层 P1-a 变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-C）")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，不让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    baseline = sha256(TARGET)
    print(f"基线 fish.py：{len(TARGET.read_bytes())} 字节 / sha256 {baseline[:16]}…")

    # 环境自证：先跑一遍未变异的用例，绿了才有资格谈"变异后变红"
    passed, tail = run_test("test_A_fresh_db_structure_complete")
    if not passed:
        print(f"[X] 环境自证失败：未变异的判据 A 都没过（{tail}）——先修环境再看变异")
        return 2
    print("环境自证：未变异时判据 A 通过 [OK]\n")

    rows = []
    failures = []
    for mut_id, judgment, test_name, old, new, note in MUTATIONS:
        if args.only and args.only != mut_id:
            continue
        original = None
        went_green = None
        tail = ""
        try:
            original = apply_mutation(old, new)
            went_green, tail = run_test(test_name)
        except Exception as exc:                   # noqa: BLE001 —— 任何异常都要先还原
            failures.append(f"{mut_id} 执行异常：{exc}")
        finally:
            if original is not None:
                original.restore()
            restored_ok = sha256(TARGET) == baseline
            if not restored_ok:
                failures.append(
                    f"{mut_id} 还原后 sha256 不一致！立即手工恢复："
                    f"用 历史版本\\fish.py.<时间戳>.bak 覆盖 fish.py")

        red = went_green is False and restored_ok
        if went_green is not False:
            failures.append(f"{mut_id} 变异后判据仍然是绿的——这条判据没有判别力")
        rows.append((mut_id, judgment, red, restored_ok, note, tail))
        print(f"{mut_id} [判据 {judgment}] {'红（符合预期）' if red else '[X] 意外'}")
        print(f"   变异：{note}")
        print(f"   目标：{TEST_FILE}::{TEST_CLASS}::{test_name}")
        print(f"   结果：{tail}")
        print(f"   还原：{'sha256 与基线一致' if restored_ok else '[X] sha256 不一致'}\n")

    print("=" * 78)
    for mut_id, judgment, red, restored_ok, note, _tail in rows:
        print(f"{mut_id:<6}{judgment:<6}{'红/预期' if red else '[X]意外':<12}"
              f"{'已还原' if restored_ok else '[X]未还原':<12}{note}")
    print("=" * 78)
    same = sha256(TARGET) == baseline
    print(f"最终 sha256：{sha256(TARGET)[:16]}… {'（与基线一致）' if same else '（[X] 与基线不一致）'}")
    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n全部 {len(rows)} 条变异：改源码后判据全红、字节已还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
