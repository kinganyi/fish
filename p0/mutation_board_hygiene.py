#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板卫生判据的变异自证（2026-11-22）。

## 为什么这一组特别需要变异

`test_board_hygiene.py` 里 9 条判据**全是"某件事没有发生"型**：

    · 行数没变
    · 文件没写
    · 板没被归档

这类判据有一个共同的失效模式：**它在任何情况下都绿**。
构造函数名打错、异常被吞、靶点没命中——它照样绿，而且看起来很合格。

所以每条判据都必须证明：**把守卫拆掉，它会红**。

## 五个变异体

    M1  `board_block` 的 `create=False` 拆掉      → H1 必须红
    M2  `board_show` 的 `create=False` 拆掉       → H2 必须红
    M3  `_render_one` 的「未登记 = 未发生」删掉    → H3 必须红
    M4  `board_sweep` 的空判 OR 段删掉            → H4 必须红
    M5  台账里的 `board_sweep` 删掉               → H5 必须红

## 每条都跑三步

    ① 对照组：未变异的树上跑该判据，**必须绿**（否则"变异后红"没有意义）
    ② 变异：改坏源码
    ③ 还原：写回原字节，**校验 sha256 一致**（不信 try/finally，只信字节）

用法：`python mutation_board_hygiene.py [--only M3]`
"""

import argparse
import subprocess
import sys

# 控制台是 GBK，不装 UTF-8 会把 ✓ / ✗ 这类符号打崩（判据会看到 exit 1 而不是真结论）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
BOARD = HERE / "fish_board.py"
MAINT = HERE / "fish_maintenance.py"
TEST = "test_board_hygiene.py"


def _run(kexpr):
    """跑一条判据，返回 (ok, 摘要)。ok=True 表示该判据**通过**。"""
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
         "-p", "no:cacheprovider", "-k", kexpr],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.strip().split("\n") if ln.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:110]


# (名字, 目标文件, 判据表达式, 原文靶点, 替换后)
MUTANTS = [
    ("M1", BOARD, "H1",
     "board = board_ensure(conn, scope, key, create=False)",
     "board = board_ensure(conn, scope, key)"),

    ("M2", BOARD, "test_H2_",
     "board = board_ensure(conn, scope, scope_key, period_key, create=False)",
     "board = board_ensure(conn, scope, scope_key, period_key)"),

    # M3 的新靶点：C 撤回了"空板不产文件"，所以变异改为拆掉"空板明写"那句
    # —— 它守的是既有契约（`test_board.py::B4` 与 `board_block` 约束 3）。
    ("M3", BOARD, "H3",
     "        lines.append(\"_（还没有人签到——**未登记 = 未发生**）_\")",
     "        pass"),

    ("M4", BOARD, "test_H4_",
     "    OR (? IS NOT NULL AND period_key <= ?\"\n"
     "        \"        AND NOT EXISTS(SELECT 1 FROM board_posts p WHERE p.board_id = boards.id))\"\n"
     "        \") ORDER BY period_key, id\", (cutoff, empty_cutoff, empty_cutoff)).fetchall()",
     "    )\"\n"
     "        \") ORDER BY period_key, id\", (cutoff, empty_cutoff, empty_cutoff)).fetchall()"),

    ("M5", MAINT, "H5",
     "    \"board_sweep\",\n",
     ""),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某个变异体（M1/M2/...）")
    args = ap.parse_args(argv)

    picked = [m for m in MUTANTS if not args.only or m[0] == args.only]
    if not picked:
        print(f"未知变异体：{args.only}")
        return 2

    failures = []
    for name, path, kexpr, old, new in picked:
        if not path.exists():
            print(f"  {name}  SKIP  目标不存在：{path.name}")
            failures.append((name, "目标文件不存在"))
            continue

        # ① 对照组：未变异必须绿
        ctrl_ok, ctrl_tail = _run(kexpr)
        if not ctrl_ok:
            print(f"  {name}  ✗ 对照组就红了，变异无意义：{ctrl_tail}")
            failures.append((name, f"对照组红：{ctrl_tail}"))
            continue

        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except Exception as exc:                      # noqa: BLE001
                print(f"  {name}  ✗ 靶点未命中：{type(exc).__name__}: {exc}")
                failures.append((name, f"靶点未命中：{exc}"))
                continue
            mut_ok, mut_tail = _run(kexpr)
            if mut_ok:
                print(f"  {name}  ✗ 变异后判据仍然绿（这条判据没有判别力）：{kexpr}")
                failures.append((name, "变异后仍绿"))
            else:
                print(f"  {name}  ✓ 变异后变红：{mut_tail}")
        finally:
            src.restore()
            same = src.sha256() == before and src.restored()
        if not same:
            print(f"  {name}  ✗ 还原失败——源码字节没回来！")
            failures.append((name, "还原失败"))
        else:
            post_ok, post_tail = _run(kexpr)
            if not post_ok:
                print(f"  {name}  ✗ 还原后判据仍红：{post_tail}")
                failures.append((name, "还原后仍红"))
            else:
                print(f"  {name}       还原后复绿 ✓")

    print()
    if failures:
        print(f"  失败 {len(failures)} 项：")
        for name, why in failures:
            print(f"    {name}: {why}")
        return 1
    print(f"  全部 {len(picked)} 个变异体通过：对照组绿 → 变异红 → 还原绿 + 字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
