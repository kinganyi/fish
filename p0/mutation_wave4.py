#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 4 判据的变异自证（2026-11-22）。六个变异体。"""

import argparse
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path
from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
ACT = HERE / "fish_activity.py"
FISH = HERE / "fish.py"
TEST = "test_wave4_attribution.py"


def _run(k):
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:110]


MUTANTS = [
    ("M1", ACT, "U1_", "WHERE action='write' AND (citizen_id IS NULL OR citizen_id='') ",
     "WHERE (citizen_id IS NULL OR citizen_id='') "),
    ("M2", ACT, "U2_", "    if not matched or not apply:\n", "    if not matched:\n"),
    ("M3", ACT, "U3_",
     "        \"UPDATE tool_activity SET citizen_id=? WHERE action='write' \"\n"
     "        \"AND (citizen_id IS NULL OR citizen_id='') AND session_key=?\",",
     "        \"UPDATE tool_activity SET citizen_id=? WHERE action='write' \"\n"
     "        \"AND session_key=?\","),
    ("M4", ACT, "U4_", '    if not sk or not cid:\n        raise ValueError',
     '    if False:\n        raise ValueError'),
    ("M5", ACT, "U5_", '    if not rows:\n        return ""', '    if not rows:\n        return "x"'),
    ("M6", FISH, "U6_",
     '        _unattr = fish_activity.render_unattributed_line(conn)\n'
     '        if _unattr:\n            print(_unattr)\n',
     '        pass\n'),
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
            fails.append(name)
            continue
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except Exception as exc:
                print(f"  {name}  x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mok, mtail = _run(k)
            if mok:
                print(f"  {name}  x 变异后仍绿")
                fails.append(name)
            else:
                print(f"  {name}  ok 变红：{mtail}")
        finally:
            src.restore()
            same = src.sha256() == before and src.restored()
        if not same:
            print(f"  {name}  x 还原失败")
            fails.append(name)
        else:
            pok, _ = _run(k)
            print(f"  {name}     {'还原复绿' if pok else 'x 还原后仍红'}")
            if not pok:
                fails.append(name)
    print()
    print(f"  {'全部 %d 个变异体通过' % len(picked) if not fails else '失败：%s' % fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
