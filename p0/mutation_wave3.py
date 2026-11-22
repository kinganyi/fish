#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 3 判据的变异自证（2026-11-22）。五个变异体，每个都应让对应判据变红。"""

import argparse
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "fish_schema.py"
ACT = HERE / "fish_activity.py"
BOARD = HERE / "fish_board.py"
TEST = "test_wave3_session_surface.py"


def _run(k):
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:110]


MUTANTS = [
    ("M1", SCHEMA, "W1_",
     '    ("session_surface", "TEXT"),\n', ""),
    ("M2", ACT, "W2_",
     "    if cum is None:\n", "    if True:\n"),
    ("M3", ACT, "W2b",
     "    if cum is None:\n", "    if False:\n"),
    ("M4", BOARD, "W3",
     '            if _cum and _cum != (p.get("auto_surface") or "").strip():\n',
     "            if False:\n"),
    ("M5", BOARD, "W4",
     '            if _cum and _cum != (p.get("auto_surface") or "").strip():\n',
     '            if _cum:\n'),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    a = ap.parse_args(argv)
    picked = [m for m in MUTANTS if not a.only or m[0] == a.only]
    fails = []
    for name, path, k, old, new in picked:
        ok, tail = _run(k)
        if not ok:
            print(f"  {name}  x 对照组红：{tail}")
            fails.append((name, "对照组红"))
            continue
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except Exception as exc:
                print(f"  {name}  x 靶点未命中：{exc}")
                fails.append((name, "靶点未命中"))
                continue
            mok, mtail = _run(k)
            if mok:
                print(f"  {name}  x 变异后仍绿")
                fails.append((name, "变异后仍绿"))
            else:
                print(f"  {name}  ok 变红：{mtail}")
        finally:
            src.restore()
            same = src.sha256() == before and src.restored()
        if not same:
            print(f"  {name}  x 还原失败")
            fails.append((name, "还原失败"))
        else:
            pok, _ = _run(k)
            if not pok:
                print(f"  {name}  x 还原后仍红")
                fails.append((name, "还原后仍红"))
            else:
                print(f"  {name}     还原复绿")
    print()
    if fails:
        print(f"  失败 {len(fails)}：{fails}")
        return 1
    print(f"  全部 {len(picked)} 个变异体通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
