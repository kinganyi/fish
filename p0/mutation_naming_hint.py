#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命名判据的变异自证（2026-11-22）。

## 为什么这一组非证不可

`test_naming_hint.py` 里最关键的那条（N2）在**改之前是 0/173 命中**：生成器产
173 个名字，检测器一个都认不出。也就是说，**同一段代码既可能是"全绿"也可能是
"全漏"**，而单看判据通过与否分不出来。

这就是变异自证要解决的问题：把"让它认得出"的那几行拆掉，判据必须红。

## 五个变异体

    M1  `is_auto_name` 的前缀判定删掉            → N2b 必须红
    （原 M2「新格式精确比对」已删：变异证明那段不可达，是死代码）
    M3  `is_auto_name` 的旧格式精确比对删掉      → N3  必须红
    M4  `_unnamed_hint` 不传 host/session_key    → N5  必须红
    M5  `_print_session_start` 的引导块删掉      → N6  必须红
    M6  生成器退回英文 `<host>-<tail[:12]>`      → N1  必须红

每条都跑三步：对照组绿 → 变异红 → 还原后复绿 + sha256 字节一致。
"""

import argparse
import subprocess
import sys

# 控制台是 GBK，不装 UTF-8 会把 ✓ / ✗ 打崩（判据会看到 exit 1 而不是真结论）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
NAMING = HERE / "fish_naming.py"
BOARD = HERE / "fish_board.py"
FISH = HERE / "fish.py"
TEST = "test_naming_hint.py"


def _run(kexpr):
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
         "-p", "no:cacheprovider", "-k", kexpr],
        cwd=str(HERE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.strip().split("\n") if ln.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:110]


MUTANTS = [
    # ── is_auto_name 的三层判定，逐层拆 ──────────────────────────
    ("M1", NAMING, "N2b",
     "    if head.startswith(UNNAMED_PREFIX):\n        return True\n",
     ""),

    # 注意：替换成 `pass` 而不是空串。空串会留下一个只有注释的 `if` 块 →
    # IndentationError → pytest 报 "error" 而不是 "failed"。那是**语法坏了**，
    # 不是判据有判别力；红得再响也证明不了什么。
    ("M3", NAMING, "N3_",
     "        tail = (session_key or \"\").split(\":\", 1)[-1].split(\"#\", 1)[0]\n"
     "        if tail and head == f\"{host}-{tail[:12]}\":\n"
     "            return True",
     "        pass"),

    # ── 提示的两处接线 ─────────────────────────────────────────
    ("M4", BOARD, "N5_",
     "    if name and not fish_naming.is_auto_name(name, host=host, session_key=sk):",
     "    if name and not fish_naming.is_auto_name(name):"),

    ("M5", FISH, "N6_",
     "    if fish_naming.is_auto_name(res[\"display_name\"], host=res[\"host\"],\n"
     "                                session_key=res[\"session_key\"]):\n"
     "        print(\"\")\n"
     "        print(f\"你还没有可读名（当前占位 `{res['display_name']}`）。\")\n"
     "        print(fish_naming.rename_guidance())",
     "    if False:\n        pass"),

    # ── 生成器退回英文 ─────────────────────────────────────────
    ("M6", NAMING, "N1",
     "    short = session_short(session_key) or (host or \"cli\")\n"
     "    base = f\"{UNNAMED_PREFIX}{short}\"",
     "    tail = (session_key or \"\").split(\":\", 1)[-1].split(\"#\", 1)[0]\n"
     "    base = f\"{host or 'cli'}-{(tail or 'x')[:12]}\""),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
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
                print(f"  {name}  ✗ 变异后判据仍然绿（没有判别力）：{kexpr}")
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
