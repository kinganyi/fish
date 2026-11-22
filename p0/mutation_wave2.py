#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wave 2 判据的变异自证（2026-11-22）。

## 六个变异体

    M1  `_surface_hint` 的"已填就不说"判断删掉        → S2 必须红
    M2  `autolink_memory_code` 的"已有编码就不补"删掉 → S6 必须红
    M3  `autolink_memory_code` 的开关判断删掉        → S7 必须红
    M4  `board_post` 不存 `session_key`              → S4 必须红
    M5  `recent_memory_code_for_session` 去掉会话过滤 → S8 必须红
    M6  `board_block` 里 `_surface_hint` 的接线删掉   → S1b 必须红

M5 是这一组里最要紧的：**跨会话关联就是编造因果**。去掉那个 `session_actor=?`
过滤，任何一条老记忆都会被贴到任意会话的帖子后面。

每条跑三步：对照组绿 → 变异红 → 还原后复绿 + sha256 字节一致。
"""

import argparse
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
BOARD = HERE / "fish_board.py"
FISH = HERE / "fish.py"
TEST = "test_wave2_surface_link.py"


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
    ("M1", BOARD, "S2_",
     "    if surface or not doing:\n        return \"\"",
     "    if not doing:\n        return \"\""),

    ("M2", BOARD, "S6_",
     "    if _POST_CODE_RE.search(body):\n        return body\n",
     ""),

    ("M3", BOARD, "S7_",
     "    if not enabled or not body:",
     "    if not body:"),

    ("M4", BOARD, "S4_",
     "    cur = conn.execute(\n"
     "        \"INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at,\"\n"
     "        \" session_key) VALUES (?,?,?,?,?,?)\",\n"
     "        (board[\"id\"], citizen_id, kind, text, now_utc(),\n"
     "         (session_key or \"\").strip()))",
     "    cur = conn.execute(\n"
     "        \"INSERT INTO board_posts (board_id, citizen_id, kind, text, created_at)\"\n"
     "        \" VALUES (?,?,?,?,?)\",\n"
     "        (board[\"id\"], citizen_id, kind, text, now_utc()))"),

    ("M5", BOARD, "S8_",
     "            \"AND json_extract(payload,'$.session_actor')=? \"",
     "            \"AND (? IS NOT NULL) \""),

    ("M6", BOARD, "S1b",
     "    surf = _surface_hint(conn, board, citizen_id, session_key)\n"
     "    if surf:\n        lines.append(surf)",
     "    pass"),
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
