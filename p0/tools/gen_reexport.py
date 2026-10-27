#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""再导出清单生成器（`fish.py` 拆包第一轮 · T02 · 设计 §1.4）。

它解决的是"漏名"这一类**静默**故障
------------------------------------
`fish.py` 里的 `from fish_kb import (...)` 是手写不出来的：121 个名字，漏一个的
后果不是报错，而是 `fish.kb_xxx` 在**运行时**才消失（`_kb_commands_ready()` 甚至会
因此悄悄降级 CAPABILITIES 页）。所以清单从 `fish_kb.py` 的 AST 反推：

    python tools/gen_reexport.py --check     # 判据用：不一致就 exit 1
    python tools/gen_reexport.py --write     # 下一轮加函数后重新生成（会改 fish.py）
    python tools/gen_reexport.py --print     # 只打印，不动文件

`--check` 与 `--write` 用的是**同一个渲染函数**，所以"生成"和"校验"不会各说各话。
排序固定（按名字升序、每行 ≤ 88 列）：同一份 AST 永远渲染出同一段文本，
`--check` 才有意义。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

P0 = Path(__file__).resolve().parent.parent
FISH = P0 / "fish.py"
FISH_KB = P0 / "fish_kb.py"
MODULE = "fish_kb"
HEADER = f"from {MODULE} import (  # noqa: F401"
WIDTH = 88


def exported_names(path: Path = FISH_KB) -> list[str]:
    """`fish_kb` 的导出面：顶层函数/类/常量名（不含 import 与 `__all__` 自身）。

    刻意**不用** `fish_kb.__all__`（那要 import 一遍才拿得到，判断脚本不想有副作用）：
    这里从 AST 直接取，和 `__all__` 的算法同源不同路——两路对上才算真对上。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
    # `__all__` / `_IMPORTED` 是本模块自己的记账，不属于导出面
    return sorted(n for n in set(names) if n not in ("__all__", "_IMPORTED"))


def render(names: list[str], *, width: int = WIDTH) -> str:
    """渲染成 `from fish_kb import (...)` 块（缩进 4，每行 ≤ width）。"""
    lines = []
    current = "    "
    for item in names:
        token = item + ","
        if len(current) + len(token) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = "    "
        current += token + " "
    if current.strip():
        lines.append(current.rstrip())
    return HEADER + "\n" + "\n".join(lines) + "\n)\n"


def block_in_fish(text: str) -> tuple[int, int, str]:
    """在 `fish.py` 里定位整段 `from fish_kb import (...)`：返回 (起行, 止行, 原文)。"""
    lines = text.split("\n")
    start = None
    for idx, line in enumerate(lines):
        if line.startswith(f"from {MODULE} import"):
            start = idx
            break
    if start is None:
        raise SystemExit(f"fish.py 里找不到 `from {MODULE} import`（拆包没做完？）")
    depth = 0
    for idx in range(start, len(lines)):
        depth += lines[idx].count("(") - lines[idx].count(")")
        if depth <= 0 and idx > start:
            return start, idx, "\n".join(lines[start:idx + 1]) + "\n"
        if depth == 0:
            return start, idx, "\n".join(lines[start:idx + 1]) + "\n"
    raise SystemExit("fish.py 里的 `from fish_kb import` 括号没有闭合")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成/校验 fish.py 的 fish_kb 再导出清单")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只校验（不一致 exit 1）")
    mode.add_argument("--write", action="store_true", help="把生成的清单写回 fish.py")
    mode.add_argument("--print", dest="do_print", action="store_true", help="只打印")
    args = ap.parse_args()

    names = exported_names()
    expected = render(names).replace("\n", "\r\n")
    text = FISH.read_bytes().decode("utf-8")
    start, end, actual = block_in_fish(text)
    actual_crlf = actual.replace("\r\n", "\n").replace("\n", "\r\n")

    same = actual_crlf == expected
    if args.do_print:
        print(expected.replace("\r\n", "\n"), end="")
        return 0
    if args.write:
        if same:
            print(f"已是最新：{len(names)} 个名字")
            return 0
        lines = text.split("\n")
        new_text = "\n".join(lines[:start]) + "\n" + \
            expected.replace("\r\n", "\n").rstrip("\n") + "\n" + "\n".join(lines[end + 1:])
        FISH.write_bytes(new_text.encode("utf-8"))
        print(f"已写回：{len(names)} 个名字（第 {start + 1}-{end + 1} 行）")
        return 0

    # 默认 = --check（判据用）
    if same:
        print(f"OK：fish.py 的再导出清单与 fish_kb 的导出面一致（{len(names)} 个名字）")
        return 0
    have = set(actual.replace("(", " ").replace(")", " ").replace(",", " ")
               .replace("\n", " ").split())
    import ast as _ast
    try:
        tree = _ast.parse(actual)
        have = {a.name for node in tree.body if isinstance(node, _ast.ImportFrom)
                for a in node.names}
    except SyntaxError:
        pass
    want = set(names)
    print("FAIL：再导出清单与 fish_kb 导出面不一致")
    print(f"  缺（fish 侧会静默消失）：{sorted(want - have) or '（无）'}")
    print(f"  多（fish 侧指向不存在的名字）：{sorted(have - want) or '（无）'}")
    print(f"  fish.py 里的块在第 {start + 1}-{end + 1} 行")
    return 1


if __name__ == "__main__":
    sys.exit(main())
