#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 会话头判据的**源码级**变异自证（P2-3 残留 · W2 评审，2026-11-26）。

## 为什么单独一条

`test_dsh_session_header.test_03` 改的是**样本 dict** 的键名（`parentSession` →
`parent_session`），它证明的是「适配器只认 camelCase」；但评审指出的问题是：这没有
证明 `fish_lineage_dsh.header_from_event` 的**源码提取点**被改时会红。本脚本把
真源码里的提取表达式改成 snake_case，跑同一组判据必须红，再按字节还原必须绿。

## 变异体

  M-DSH-HEADER  `fish_lineage_dsh.py` 的
                 `str(event["parentSession"]) if event.get("parentSession") else None`
                 → `str(event["parent_session"]) if event.get("parent_session") else None`
                 目标判据：`test_dsh_session_header` 全文件（test_02 的父链断言必红）

## 纪律

  ① 基线必须绿 → ② 变异后必须红 → ③ `write_bytes` 逐字节还原（sha256 相等）
  → ④ 还原后必须复绿。任一步不成立，退出码 1。

用法：`python -X utf8 mutation_dsh_header.py`
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # A档：拿不到可重配的流就按原样

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fish_lineage_dsh.py"
TEST_FILE = "test_dsh_session_header.py"

OLD = ('str(event["parentSession"]) if event.get("parentSession") else None')
NEW = ('str(event["parent_session"]) if event.get("parent_session") else None')


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_tests() -> tuple[int, str]:
    """跑整个 DSH 会话头判据文件；返回 (退出码, 输出尾部)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", TEST_FILE, "-q",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=str(HERE), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=300)
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join([line for line in output.strip().split("\n") if line.strip()][-3:])
    return proc.returncode, tail


def main() -> int:
    original = SOURCE.read_bytes()
    baseline_sha = _sha256(original)
    text = original.decode("utf-8")
    hits = text.count(OLD)
    if hits != 1:
        print(f"FAIL：源码锚点命中 {hits} 处（应为 1）——判据脚本与实现漂了")
        return 1

    print(f"M-DSH-HEADER [{SOURCE.name}] 源码级变异：parentSession → parent_session")
    try:
        print("== ① 基线（未变异）应全绿 ==")
        green, tail = _run_tests()
        print(f"  退出码={green}｜{tail}")
        if green != 0:
            print("  × 基线不绿：判据本身有问题，本次变异不作数")
            return 1

        print("== ② 变异后（必须红）==")
        SOURCE.write_bytes(text.replace(OLD, NEW, 1).encode("utf-8"))
        red, tail = _run_tests()
        print(f"  退出码={red}｜{tail}")
        if red == 0:
            print("  × 变异后仍绿——判据没有测到源码提取点")
            return 1
        print("  ok 变异被抓住（红）")

        print("== ③ 逐字节还原 ==")
        SOURCE.write_bytes(original)
        restored_sha = _sha256(SOURCE.read_bytes())
        print(f"  sha256 基线={baseline_sha[:16]}… 还原={restored_sha[:16]}…")
        if restored_sha != baseline_sha:
            print("  × 还原失败：工作树被污染，停机检查")
            return 1
        print("  ok 字节一致")

        print("== ④ 还原后复跑（应回到绿）==")
        again, tail = _run_tests()
        print(f"  退出码={again}｜{tail}")
        if again != 0:
            print("  × 还原后仍红——源码未干净恢复")
            return 1
        print("  ok 复绿")
    finally:
        # 任何异常路径都按原始字节还原（幂等）
        if SOURCE.read_bytes() != original:
            SOURCE.write_bytes(original)

    print("-" * 72)
    print("OK 1/1：基线绿 → 源码变异红 → 逐字节还原 → 复绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
