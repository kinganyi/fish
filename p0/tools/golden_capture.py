#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对拍捕获器（`fish.py` 拆包第一轮 · T01 · 设计 §1.5）。

它做什么
--------
按 `tools/golden_cmds.txt` 里的 22 条命令，把**同一个 `fish.py`** 的
stdout / stderr / 退出码原样抓下来，存成 `run-<rep>.json` + `run-<rep>.txt`。
跑 `--repeat N` 就抓 N 遍，用来把"两次之间的噪声"与"两版之间的差异"分开——
没有这一步，掩码清单就只能靠猜。

三道硬闸（都是"防呆"，不是"防人"）
----------------------------------
1. **`--root` 必须在 `_golden/` 之内**：所有命令都会 `executescript(SCHEMA)+migrate`，
   指到 `p0/data` 就是拿生产库做实验。这里在**起进程之前**断言路径。
2. **每条命令都跑在夹具的副本上**（`_work-<rep>`），且副本里的 `kb_vault.path`
   被改写成副本自身的位置——否则写命令会去改原夹具，第二次跑就不是同一个起点。
3. **捕获只记录、不改写**：stderr 与 stdout 分开存，退出码原样保留。
   任何"看起来不对但我帮你改一下"的行为都会让对拍失去意义。

用法
----
    python tools/golden_capture.py --out _golden/r1/old --repeat 2
    python tools/golden_capture.py --checkout <另一棵树> --out _golden/r1/new --repeat 2
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
P0 = HERE.parent
GOLDEN = P0 / "_golden"
R1 = GOLDEN / "r1"
DEFAULT_FIXTURE = R1 / "fixture"
DEFAULT_CMDS = HERE / "golden_cmds.txt"

PIN_VAULT_ID = "01GOLDEN0000000000000000V"
WRITE_ID_RE = re.compile(r"fish kb rollback ([0-9]+-[0-9a-fA-F]+)")

ROOTS = {"main": "", "empty": "empty"}   # 命令文件里的 root 记号 → 相对副本根的落点


# ------------------------------------------------------------------ 闸门
def assert_inside_golden(path: Path, what: str) -> None:
    """硬断言：路径必须位于 `_golden/` 之下（含 `_golden` 本身的下级目录）。"""
    parts = [p.lower() for p in path.resolve().parts]
    if "_golden" not in parts:
        raise SystemExit(
            f"拒绝执行：{what} 必须在 `_golden/` 之下，收到 {path}\n"
            "  （`fish` 的所有命令都会建库/写库；指到生产目录就会污染真库。）")


# ------------------------------------------------------------------ 命令清单
def load_commands(path: Path) -> list[dict]:
    """读命令清单：`<slug>|<root>|<args...>`，`#` 起头是注释。

    分隔符用 `|` 而不是制表符：命令里带空格（`--doing "一句话"`），
    制表符在编辑器/格式化里太容易被打散，而 `|` 不会。
    """
    if not path.is_file():
        raise SystemExit(f"找不到命令清单：{path}")
    out = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|", 2)]
        if len(fields) != 3 or not fields[2]:
            raise SystemExit(
                f"{path.name}:{lineno} 期望 `slug|root|args` 三段，实际 {len(fields)} 段：{line!r}")
        slug, root_token, argv = fields
        if root_token not in ROOTS:
            raise SystemExit(
                f"{path.name}:{lineno} root 只能是 {sorted(ROOTS)}，收到 {root_token!r}")
        if not slug or " " in slug:
            raise SystemExit(f"{path.name}:{lineno} slug 必须非空且不含空格：{slug!r}")
        out.append({"slug": slug, "root": root_token, "argv": argv.split()})
    slugs = [c["slug"] for c in out]
    if len(set(slugs)) != len(slugs):
        dup = sorted({s for s in slugs if slugs.count(s) > 1})
        raise SystemExit(f"{path.name} 里有重复 slug：{dup}")
    return out


def substitute(argv: list[str], *, scratch: Path, write_id: str | None) -> list[str]:
    """`{fixture}` / `{write_id}` 占位符替换（`{root}` 由调用方拼在 `--root` 上）。"""
    out = []
    for item in argv:
        text = item.replace("{fixture}", str(scratch))
        if "{write_id}" in text:
            if not write_id:
                raise SystemExit(
                    "命令引用 {write_id}，但它之前的命令没有产出可用的 write_id。"
                    "（`kb write` 的回执里必须有 `fish kb rollback <id>` 那一行。）")
            text = text.replace("{write_id}", write_id)
        out.append(text)
    return out


# ------------------------------------------------------------------ 副本
def prepare_scratch(fixture: Path, scratch: Path) -> None:
    """把夹具复制成一次性的工作副本，并把库里的 vault 路径改写成副本的位置。

    为什么必须改写：`kb_vault.path` 是**绝对路径**。不改写的话，副本里的
    `kb write` 会真的写到原夹具目录去——第二次跑就不是同一个起点了。
    `path_key` 与写入路径同口径：`normcase(realpath(path))`。
    """
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(fixture, scratch)
    vault = scratch / "vault"
    key = os.path.normcase(os.path.realpath(str(vault)))
    db = scratch / "data" / "index.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE kb_vault SET path=?, path_key=?", (str(vault), key))
        conn.commit()
        rows = conn.execute("SELECT name, path FROM kb_vault").fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise SystemExit(f"夹具里有 {len(rows)} 个 vault（期望恰好 1 个）：{rows}")


# ------------------------------------------------------------------ 跑命令
def run_one(fish_py: Path, root: Path, argv: list[str], *, cwd: Path) -> dict:
    cmd = [sys.executable, "-X", "utf8", str(fish_py), "--root", str(root)] + list(argv)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return {"argv": list(argv), "root": str(root), "cmd": cmd,
            "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def capture_once(fish_py: Path, fixture: Path, commands: list[dict], scratch: Path,
                 *, rep: int) -> dict:
    prepare_scratch(fixture, scratch)
    results = []
    write_id = None
    for item in commands:
        rel = ROOTS[item["root"]]
        root = (scratch / rel / "data") if rel else (scratch / "data")
        argv = substitute(item["argv"], scratch=scratch, write_id=write_id)
        rec = run_one(fish_py, root.resolve(), argv, cwd=scratch)
        rec["slug"] = item["slug"]
        found = WRITE_ID_RE.search(rec["stdout"] + rec["stderr"])
        if found:
            write_id = found.group(1)
        results.append(rec)
        print(f"  [{rep}] {item['slug']:<22} exit={rec['exit']:<2} "
              f"out={len(rec['stdout']):>6}B err={len(rec['stderr']):>5}B")
    stamp = datetime.datetime.now().astimezone()
    return {
        "meta": {
            "rep": rep,
            "checkout": str(fish_py),
            "fixture": str(fixture),
            "scratch": str(scratch),
            "captured_at": stamp.isoformat(timespec="seconds"),
            "captured_local_date": stamp.strftime("%Y-%m-%d"),
            "python": sys.version.split()[0],
        },
        "commands": [{k: r[k] for k in ("slug", "cmd", "exit", "stdout", "stderr")}
                     for r in results],
    }


def render_text(run: dict) -> str:
    """人读的镜像：`=== slug | exit=N ===` + stdout + `--- stderr ---`。"""
    lines = [f"# run-{run['meta']['rep']} · fish.py={run['meta']['checkout']}",
             f"# captured_at={run['meta']['captured_at']}", ""]
    for item in run["commands"]:
        lines.append(f"=== {item['slug']} | exit={item['exit']} ===")
        lines.append("[stdout]")
        lines.append(item["stdout"].rstrip("\n"))
        lines.append("[stderr]")
        lines.append(item["stderr"].rstrip("\n"))
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="抓一组 fish 命令的逐字节读数（T01）")
    ap.add_argument("--checkout", default=str(P0),
                    help="含 fish.py 的目录（默认本仓库；抓旧版就把旧树放进去）")
    ap.add_argument("--out", required=True, help="输出目录（必须位于 _golden/ 之下）")
    ap.add_argument("--repeat", type=int, default=2, help="抓几遍（≥2 才能算出噪声）")
    ap.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--cmds", default=str(DEFAULT_CMDS))
    ap.add_argument("--work", default=str(R1 / "_work"),
                    help="工作副本的固定位置（旧/新两版必须用**同一个**路径，见下）")
    ap.add_argument("--keep-work", action="store_true",
                    help="保留工作副本（排查用）")
    args = ap.parse_args()

    checkout = Path(args.checkout).resolve()
    fish_py = checkout / "fish.py"
    if not fish_py.is_file():
        raise SystemExit(f"--checkout 里没有 fish.py：{fish_py}")
    fixture = Path(args.fixture).resolve()
    if not (fixture / "data" / "index.sqlite").is_file():
        raise SystemExit(f"夹具不完整（缺 data/index.sqlite）：{fixture}\n"
                         "  先跑：python tools/golden_fixture.py")
    out = Path(args.out).resolve()
    assert_inside_golden(out, "--out")
    assert_inside_golden(fixture, "--fixture")
    if args.repeat < 1:
        raise SystemExit("--repeat 至少 1（要算噪声请给 ≥2）")

    commands = load_commands(Path(args.cmds))
    if len(commands) != 22:
        raise SystemExit(f"命令清单应为 22 条（设计 §1.5），实际 {len(commands)} 条")

    out.mkdir(parents=True, exist_ok=True)
    # 工作副本用**固定且共享**的位置，理由有两条，都是"可比"的前提：
    # ① `layout render` 会把 vault 绝对路径写进门面产物，再对产物算 sha256；
    #    路径一换，摘要就变，而摘要**不能**被掩码（掩掉摘要等于放弃内容判据）；
    # ② 旧版与新版必须在**同一路径**上跑，否则"路径不同"这类差异会淹没真差异。
    # 每次捕获前都会整目录重建，所以共享位置不会串味。
    scratch = Path(args.work).resolve()
    assert_inside_golden(scratch, "--work")
    for rep in range(1, args.repeat + 1):
        print(f"[run {rep}/{args.repeat}] checkout={checkout}")
        run = capture_once(fish_py, fixture, commands, scratch, rep=rep)
        # 每次捕获的"起点"都是夹具本身：这里顺手把副本里的 vault 指纹记下来，
        # 任何"夹具被写坏了"的问题都能在 JSON 里一眼看到（比事后猜强）。
        run["meta"]["fixture_db_sha256"] = _sha256(fixture / "data" / "index.sqlite")
        (out / f"run-{rep}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / f"run-{rep}.txt").write_text(render_text(run), encoding="utf-8")
        if not args.keep_work:
            shutil.rmtree(scratch, ignore_errors=True)
        failed = [c["slug"] for c in run["commands"] if c["exit"] != 0]
        print(f"[run {rep}/{args.repeat}] 完成：{len(run['commands'])} 条命令，"
              f"非零退出 {len(failed)} 条{('：' + ', '.join(failed)) if failed else ''}")

    print(f"捕获完成：{out}")
    return 0


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
