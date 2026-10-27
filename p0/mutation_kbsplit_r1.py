#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆包判据的变异自证（`fish.py` 拆包第一轮 · T04 · 设计 §1.6 M1–M6）。

为什么要有这一步
----------------
"判据全绿"只能证明代码没踩到判据，不能证明判据**抓得住**问题。这里逐个把
被测性质**故意弄坏**，确认对应的判据会红——红的才是判据，不红的是摆设。

纪律
----
· **只在临时副本上动手**：`tempfile.mkdtemp()` 里复制 6 个模块文件 + 必要的测试文件，
  真源码一个字节都不碰；每个变异跑完立刻还原副本再跑下一个。
· **红的定义**：判据进程退出码 ≠ 0。绿的变异会被记成"未自证"并计入结论。
· 不做"顺手修"：变异只做最小的那一处文本替换，且必须命中**恰好一次**。

用法：`python mutation_kbsplit_r1.py [--only M4]`
输出：控制台 + `变异自证-kbsplit-r1.log`（UTF-8）
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

P0 = Path(__file__).resolve().parent
LOG = P0 / "变异自证-kbsplit-r1.log"
GOLDEN_OLD = P0 / "_golden" / "r1" / "old"
GOLDEN_FIXTURE = P0 / "_golden" / "r1" / "fixture"
GOLDEN_WORK = P0 / "_golden" / "r1" / "_work"

COPY_FILES = ("fish.py", "fish_kb.py", "fish_common.py", "fish_surface.py",
              "fish_naming.py", "fish_activity.py", "test_split_contract.py")

J2_TESTS = ["test_split_contract.SplitDependencyTest.test_j2_no_back_import_static",
            "test_split_contract.SplitDependencyTest.test_j2_no_back_import_runtime_and_both_orders"]
J1_TESTS = ["test_split_contract.SplitSurfaceTest.test_j1_every_kb_name_is_the_same_object_in_fish",
            "test_split_contract.SplitSurfaceTest.test_j1b_reexport_list_matches_ast"]
J3_TESTS = ["test_split_contract.SplitDependencyTest.test_j3_no_unresolved_globals"]
J1C_TESTS = ["test_split_contract.SplitSurfaceTest.test_j1c_kb_commands_ready_is_true"]

# ------------------------------------------------------------------ 变异清单
# kind:
#   unittest  —— 在副本里跑指定的判据，期望红
#   golden    —— 用副本当 --checkout 抓一遍金标，与旧版捕获比，期望红/不红（observed）
#   probe     —— 单命令探针（口径：退出码），与真源码对比，期望两侧不同
MUTATIONS: list[dict] = [
    {
        "id": "M1",
        "what": "fish_kb.py 顶部加 `import fish`（造反向依赖）",
        "file": "fish_kb.py",
        "find": "from fish_common import _exc_text, count_tokens, iso_local, now_utc, ulid",
        "replace": ("import fish  # 变异 M1：故意造反向依赖\n"
                    "from fish_common import _exc_text, count_tokens, iso_local, now_utc, ulid"),
        "kind": "unittest",
        "judge": J2_TESTS,
        "expect": "红（J2 静态 + 运行时双查）",
    },
    {
        "id": "M2",
        "what": "fish.py 的再导出清单删掉 `kb_sha256_file`（模拟漏名）",
        "file": "fish.py",
        "find": "kb_sha256_file, ",
        "replace": "",
        "kind": "unittest",
        "judge": J1_TESTS,
        "expect": "红（J1 缺名 + J1b 清单不等）",
    },
    {
        "id": "M3",
        "what": "fish_kb.py 删掉 `import fnmatch`（漏 import）",
        "file": "fish_kb.py",
        "find": "import fnmatch\n",
        "replace": "",
        "kind": "unittest",
        "judge": J3_TESTS,
        "expect": "红（J3：fnmatch 成了未定义全局名）",
    },
    {
        "id": "M4",
        "what": "fish_kb.py 把 `KB_MAX_LIMIT` 改成 999",
        "file": "fish_kb.py",
        "find": "KB_MAX_LIMIT = 30",
        "replace": "KB_MAX_LIMIT = 999",
        "kind": "golden",
        "expect": "红（对拍：常量变了，输出必须变）",
    },
    {
        "id": "M4b",
        "what": "fish_kb.py 把 `KB_MAX_ITEMS_PER_DIR` 改成 21（M4 的补充：M4 的常量在 22 条命令里不可见）",
        "file": "fish_kb.py",
        "find": "KB_MAX_ITEMS_PER_DIR = 20",
        "replace": "KB_MAX_ITEMS_PER_DIR = 21",
        "kind": "golden",
        "expect": "红（对拍：tree 表头会打印这个常量）",
    },
    {
        "id": "M5",
        "what": "fish_kb.py 让 `_kb_exit_code` 对 not_found 也返回 0",
        "file": "fish_kb.py",
        "find": '    return 2 if getattr(exc, "code", "") == "bad_usage" else 1',
        "replace": ('    if getattr(exc, "code", "") == "bad_usage":\n'
                    '        return 2\n'
                    '    return 0  # 变异 M5：域错误也变成成功'),
        "kind": "probe",
        "probe_argv": ["kb", "rollback", "999-deadbeef"],
        "expect": "红（探针：同一条错命令的退出码与真源码不同）",
    },
    {
        "id": "M6",
        "what": "fish_kb.py 让 `_kb_commands_ready` 返回 False",
        "file": "fish_kb.py",
        "find": '    return "kb_tree" in globals() and "kb_write" in globals()',
        "replace": "    return False  # 变异 M6：假装知识库没接入",
        "kind": "mixed",
        "judge": J1C_TESTS,
        "expect": "红（J1c + 对拍：CAPABILITIES 页与渲染清单都会变）",
    },
]


# ------------------------------------------------------------------ 执行
def copy_tree(dst: Path) -> None:
    for name in COPY_FILES:
        shutil.copy2(P0 / name, dst / name)


def apply_mutation(root: Path, item: dict) -> None:
    """在副本上做最小替换（恰好一处）。

    源码全是 CRLF，而变异锚点按 LF 写（人读/写都方便）——所以这里统一
    "读成 LF → 替换 → 写回 CRLF"，并在写回前断言原文件确实是全 CRLF。
    """
    path = root / item["file"]
    raw = path.read_bytes().decode("utf-8")
    if raw.count("\r\n") != raw.count("\n"):
        raise SystemExit(f"{item['id']}：{item['file']} 行尾不一致，停止变异")
    text = raw.replace("\r\n", "\n")
    find = item["find"].replace("\r\n", "\n")
    replace = item["replace"].replace("\r\n", "\n")
    occurrences = text.count(find)
    if occurrences != 1:
        raise SystemExit(f"{item['id']}：锚点命中 {occurrences} 次（要求恰好 1 次）："
                         f"{item['find']!r}")
    path.write_bytes(text.replace(find, replace).replace("\n", "\r\n").encode("utf-8"))


def restore(root: Path, item: dict) -> None:
    shutil.copy2(P0 / item["file"], root / item["file"])


def run_unittest(root: Path, tests: list[str]) -> dict:
    cmd = [sys.executable, "-X", "utf8", "-m", "unittest"] + tests
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    tail = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()][-6:]
    return {"exit": proc.returncode, "tail": tail, "cmd": " ".join(cmd[-len(tests):])}


def run_golden(root: Path, out: Path) -> dict:
    """用副本当 --checkout 抓金标（夹具与工作副本位置**与旧版捕获完全一致**）。"""
    cmd = [sys.executable, "-X", "utf8", str(P0 / "tools" / "golden_capture.py"),
           "--checkout", str(root), "--out", str(out), "--repeat", "1",
           "--fixture", str(GOLDEN_FIXTURE), "--work", str(GOLDEN_WORK)]
    cap = subprocess.run(cmd, cwd=str(P0), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if cap.returncode != 0:
        return {"exit": cap.returncode, "tail": ["捕获失败",
                                                 (cap.stdout + cap.stderr)[-1500:]]}
    cmp_cmd = [sys.executable, "-X", "utf8", str(P0 / "tools" / "golden_compare.py"),
               "--old", str(GOLDEN_OLD), "--new", str(out), "--base", str(out)]
    cmp_proc = subprocess.run(cmp_cmd, cwd=str(P0), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    tail = [ln for ln in (cmp_proc.stdout + cmp_proc.stderr).splitlines() if ln.strip()]
    diff_path = out / "diff.txt"
    changed = []
    if diff_path.is_file():
        changed = [ln.split("/", 2)[-1].rstrip("）").split("（")[0]
                   for ln in diff_path.read_text(encoding="utf-8").splitlines()
                   if ln.startswith("### 差异：") or ln.startswith("### 差异:")]
    return {"exit": cmp_proc.returncode, "tail": tail, "changed": changed}


def run_probe(root: Path, argv: list[str]) -> dict:
    """单命令探针：在固定工作副本上跑同一条命令，比退出码 + stderr。"""
    cmd = [sys.executable, "-X", "utf8", str(root / "fish.py"),
           "--root", str(GOLDEN_WORK / "data")] + argv
    proc = subprocess.run(cmd, cwd=str(P0), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return {"exit": proc.returncode, "stderr": proc.stderr.strip()}


def prepare_probe_work() -> None:
    """给探针准备一份干净的工作副本（复用金标捕获器里的同一份实现，不另写一套）。"""
    if str(P0 / "tools") not in sys.path:
        sys.path.insert(0, str(P0 / "tools"))
    import golden_capture  # noqa: E402 —— 与金标捕获共用"复制夹具 + 改写 vault 路径"
    golden_capture.prepare_scratch(GOLDEN_FIXTURE, GOLDEN_WORK)


def main() -> int:
    ap = argparse.ArgumentParser(description="拆包判据的变异自证 M1–M6")
    ap.add_argument("--only", default=None, help="只跑某一个变异（如 M4）")
    args = ap.parse_args()

    items = [m for m in MUTATIONS if not args.only or m["id"] == args.only]
    if not items:
        raise SystemExit(f"没有匹配的变异：{args.only}")

    lines = ["# 变异自证 · fish.py 拆包第一轮（M1–M6）",
             "",
             f"- 运行方式：`python {Path(__file__).name}`（临时副本，真源码不动）",
             f"- 金标旧版捕获：`{GOLDEN_OLD}`",
             ""]
    results = []
    for item in items:
        root = Path(tempfile.mkdtemp(prefix=f"kbsplit-{item['id']}-"))
        try:
            copy_tree(root)
            apply_mutation(root, item)
            if item["kind"] == "golden":
                out = root / "_golden" / "r1" / "mut"
                out.parent.mkdir(parents=True, exist_ok=True)
                result = run_golden(root, out)
                red = result["exit"] != 0
                detail = result.get("changed") or result.get("tail")
            elif item["kind"] == "probe":
                prepare_probe_work()
                mutant = run_probe(root, item["probe_argv"])
                prepare_probe_work()
                baseline = run_probe(P0, item["probe_argv"])
                red = mutant["exit"] != baseline["exit"]
                detail = [f"真源码 exit={baseline['exit']}",
                          f"变异版 exit={mutant['exit']}",
                          f"stderr 相同={mutant['stderr'] == baseline['stderr']}"]
            elif item["kind"] == "mixed":
                unit = run_unittest(root, item["judge"])
                out = root / "_golden" / "r1" / "mut"
                out.parent.mkdir(parents=True, exist_ok=True)
                gold = run_golden(root, out)
                red = unit["exit"] != 0 and gold["exit"] != 0
                detail = [f"unittest exit={unit['exit']}"] + unit["tail"] + \
                         [f"golden exit={gold['exit']}"] + (gold.get("changed") or [])
            else:
                unit = run_unittest(root, item["judge"])
                red = unit["exit"] != 0
                detail = [f"exit={unit['exit']}"] + unit["tail"]
        finally:
            restore(root, item)
            shutil.rmtree(root, ignore_errors=True)

        verdict = "红（自证成立）" if red else "**绿（未自证）**"
        results.append({"id": item["id"], "red": red, "what": item["what"]})
        lines.append(f"## {item['id']} · {item['what']}")
        lines.append(f"- 预期：{item['expect']}")
        lines.append(f"- 观测：{verdict}")
        for row in detail[:12]:
            lines.append(f"  - {row}")
        lines.append("")
        print(f"{item['id']:<4} {verdict:<16} {item['what']}")

    ok = sum(1 for r in results if r["red"])
    lines.append(f"## 结论：{ok}/{len(results)} 个变异被判据抓住")
    for row in results:
        if not row["red"]:
            lines.append(f"- 未自证：{row['id']} · {row['what']}")
    LOG.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    print(f"\n{ok}/{len(results)} 红；日志：{LOG}")
    return 0 if ok == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
