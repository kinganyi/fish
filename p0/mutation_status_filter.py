#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读路径 status 过滤 · 变异自证驱动（2026-10-24）
================================================
用途：**改真源码 → 跑 → 必红 → 字节还原**。七条变异（判据 A–F + 附加 G），
一条都不许"照绿"。

为什么不只靠 test_status_filter.py 里的断言：
  断言只证明"现在是对的"。它证明不了"我把这段删掉之后它会红"——恒真断言在
  实现坏掉时照样通过。这个脚本改的是磁盘上的**字节**，用独立的 pytest 进程跑
  对应判据，证明的是"真把这段删了/改了，那条判据真的会红"。

只碰一个目标文件（本轮改动面）：
  · fish.py —— 三个函数的 status 过滤：A / B / C / D / E / F / G

判据 F 是**真实数据复核**：M-F 把 similar_codes 的 status 过滤摘掉，跑生产库
**只读副本**那条判据，必须重新看到 `ANYI-261022-29`（status='withdrawn'）。

纪律（与 mutation_identity_p1a/p1b/p1c.py 同一套）：
  · 读写走字节（文件是 LF 结尾的，文本模式在 Windows 上会把 LF 改成 CRLF → 还原不干净）；
  · 每改一次都比对 sha256，结束后**必须**逐字节还原并复核；
  · 任何异常（进程崩溃 / Ctrl-C）都走 finally 还原；
  · 还原失败 → 立刻非零退出并打印人工恢复步骤（备份在 历史版本\\）。

用法：
    python mutation_status_filter.py              # 全跑（约 30-60 秒，含一次生产库只读拷贝）
    python mutation_status_filter.py --only M-F   # 只跑一条
    python mutation_status_filter.py --list       # 只列清单
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = "test_status_filter.py"
TEST_CLASS = "FishStatusFilterTest"
BACKUP_HINT = "历史版本\\fish.py.20261024-1755-status.bak"

TARGETS = {
    "fish.py": HERE / "fish.py",
}

SIM_CANDS_OLD = (
    '        "SELECT f.code FROM memory_fts f JOIN code_index ci ON ci.code = f.code "\n'
    '        "WHERE memory_fts MATCH ? AND ci.status = \'active\' LIMIT 100", (expr,))]\n'
)
SIM_CANDS_NEW = (
    '        "SELECT code FROM memory_fts WHERE memory_fts MATCH ? LIMIT 100", (expr,))]\n'
)
MENTION_OLD = (
    '        + ",".join("?" * min(len(probes), _MENTION_MAX_PROBES)) '
    '+ ") AND status = \'active\'",\n'
)
MENTION_NEW = (
    '        + ",".join("?" * min(len(probes), _MENTION_MAX_PROBES)) + ")",\n'
)
FALLBACK_OLD = (
    "    if not include_superseded:\n"
    '        where.append("status = \'active\'")\n'
)

# 一条变异 = (编号, 守哪条判据, 目标文件, 目标用例, 原样片段, 改成什么, 说明)
MUTATIONS = (
    (
        "M-A", "A", "fish.py", "test_A_similar_codes_excludes_inactive",
        SIM_CANDS_OLD, SIM_CANDS_NEW,
        "similar_codes 候选不再 JOIN code_index → 已作废条目重新进入相似结果",
    ),
    (
        "M-B", "B", "fish.py", "test_B_similar_codes_keeps_active",
        '        "WHERE memory_fts MATCH ? AND ci.status = \'active\' LIMIT 100", (expr,))]\n',
        '        "WHERE memory_fts MATCH ? AND ci.status != \'active\' LIMIT 100", (expr,))]\n',
        "过滤写反（只留非 active）→ 活跃的相似条目被滤掉",
    ),
    (
        "M-C", "C", "fish.py", "test_C_mention_excludes_inactive",
        MENTION_OLD, MENTION_NEW,
        "code_mentions_in 不再筛 status → 正文提及已作废编码也建边",
    ),
    (
        "M-D", "D", "fish.py", "test_D_mention_keeps_active",
        MENTION_OLD,
        '        + ",".join("?" * min(len(probes), _MENTION_MAX_PROBES)) + ") AND 1 = 0",\n',
        "过滤写成恒假 → 正文提及活跃编码也识别不出来",
    ),
    (
        "M-E", "E", "fish.py", "test_E_recent_fallback_excludes_inactive",
        FALLBACK_OLD,
        "    if False:   # 变异：不筛状态\n"
        '        where.append("status = \'active\'")\n',
        "recent_fallback 不再筛 status → 未命中提示里冒出已作废条目",
    ),
    (
        "M-F", "F", "fish.py", "test_F_prod_copy_similar_codes_skips_retired",
        SIM_CANDS_OLD, SIM_CANDS_NEW,
        "生产库副本复核：摘掉 status 过滤 → similar_codes 重新返回 ANYI-261022-29",
    ),
    (
        "M-G", "G", "fish.py", "test_G_recent_fallback_include_superseded_opt_in",
        FALLBACK_OLD,
        "    if True:   # 变异：忽略 include_superseded（状态过滤恒开）\n"
        '        where.append("status = \'active\'")\n',
        "include_superseded 参数成了摆设（显式要看退役条目也看不到）",
    ),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_source(path: Path) -> str:
    """按字节读、再解码：文件是 LF 结尾的，文本模式会把换行悄悄改写掉。"""
    return path.read_bytes().decode("utf-8")


def write_source(path: Path, text: str):
    path.write_bytes(text.encode("utf-8"))


def run_test(test_name: str) -> tuple:
    """跑目标用例，返回 (是否通过, 最后一行输出)。

    "没有跑到用例"（名字写错 / 收集失败）必须**响亮报错**，不能当成"红"——
    否则一条永远跑不起来的变异会伪装成"判据有判别力"。
    """
    node = f"{TEST_FILE}::{TEST_CLASS}::{test_name}"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-header"],
        cwd=str(HERE), capture_output=True)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    tail = lines[-1] if lines else "(无输出)"
    if "no tests ran" in out.lower() or "error: not found" in out.lower():
        raise RuntimeError(f"目标用例没有跑起来（{node}）：{tail}")
    return proc.returncode == 0, tail[:160]


def apply_mutation(path: Path, old: str, new: str) -> str:
    """就地改源码，返回改之前的原文（用于还原）。找不到/多处命中就报错，绝不静默跳过。"""
    text = read_source(path)
    hits = text.count(old)
    if hits != 1:
        raise AssertionError(f"{path.name} 里变异片段命中 {hits} 次（要求恰好 1 次）：{old[:60]!r}")
    write_source(path, text.replace(old, new))
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="读路径 status 过滤变异自证")
    parser.add_argument("--only", help="只跑某一条（如 M-F）")
    parser.add_argument("--list", action="store_true", help="只列变异清单，不执行")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):        # 控制台是 GBK，不让符号把输出搞崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list:
        for mut_id, judgment, fname, test_name, _o, _n, note in MUTATIONS:
            print(f"{mut_id:<6}{judgment:<4}{fname:<10}{test_name:<58}{note}")
        return 0

    baseline = {name: sha256(path) for name, path in TARGETS.items()}
    for name, path in TARGETS.items():
        print(f"基线 {name}：{len(path.read_bytes())} 字节 / sha256 {baseline[name][:16]}…")

    # 环境自证：未变异时判据 A 与判据 F 都必须通过——绿了才有资格谈"变异后变红"。
    for probe in ("test_A_similar_codes_excludes_inactive",
                  "test_F_prod_copy_similar_codes_skips_retired"):
        passed, tail = run_test(probe)
        if not passed:
            print(f"[X] 环境自证失败：未变异时 {probe} 都没过（{tail}）——先修环境再看变异")
            return 2
    print("环境自证：未变异时判据 A / F 均通过 [OK]\n")

    rows = []
    failures = []
    for mut_id, judgment, fname, test_name, old, new, note in MUTATIONS:
        if args.only and args.only != mut_id:
            continue
        path = TARGETS[fname]
        original = None
        went_green = None
        restored_ok = False
        tail = ""
        try:
            original = apply_mutation(path, old, new)
            went_green, tail = run_test(test_name)
        except Exception as exc:                   # noqa: BLE001 —— 任何异常都要先还原
            failures.append(f"{mut_id} 执行异常：{exc}")
        finally:
            if original is not None:
                write_source(path, original)
            restored_ok = sha256(path) == baseline[fname]
            if not restored_ok:
                failures.append(
                    f"{mut_id} 还原后 {fname} 的 sha256 不一致！立即手工恢复："
                    f"用 {BACKUP_HINT} 覆盖 {fname}")

        red = went_green is False and restored_ok
        if went_green is False and original is None:
            red = False                           # 片段没命中，不算"变异生效过"
        if went_green is not False:
            failures.append(f"{mut_id} 变异后判据仍然是绿的——这条判据没有判别力")
        rows.append((mut_id, judgment, red, restored_ok, note, tail))
        print(f"{mut_id} [判据 {judgment}] {'红（符合预期）' if red else '[X] 意外'}")
        print(f"   变异：{note}")
        print(f"   目标：{TEST_FILE}::{TEST_CLASS}::{test_name}")
        print(f"   结果：{tail}")
        print(f"   还原：{'sha256 与基线一致' if restored_ok else '[X] sha256 不一致'}\n")

    print("=" * 96)
    for mut_id, judgment, red, restored_ok, note, _tail in rows:
        print(f"{mut_id:<6}{judgment:<5}{'红/预期' if red else '[X]意外':<12}"
              f"{'已还原' if restored_ok else '[X]未还原':<12}{note}")
    print("=" * 96)
    same = all(sha256(path) == baseline[name] for name, path in TARGETS.items())
    detail = " / ".join(f"{name} {sha256(path)[:12]}…" for name, path in TARGETS.items())
    print(f"最终 sha256：{detail} {'（与基线一致）' if same else '（[X] 与基线不一致）'}")
    if failures:
        print("\n失败项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"\n全部 {len(rows)} 条变异：改源码后判据全红、字节已还原。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
