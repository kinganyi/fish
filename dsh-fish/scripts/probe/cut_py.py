#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Python 侧切词输出器（先验假设 #2 / 步骤 5 变异的对拍基准）。

**不复制** fish 的切词实现：直接 import `p0/fish.py` 的 `index_tokens`，
保证"被对拍的 Python 侧"就是生产环境跑的那一份（复制一份出来对拍等于自己证自己）。

输入：JSON 文件，形如 `["查询串1", "查询串2", ...]`
输出：stdout 一份 JSON，形如 `{"module": "...", "tokens": [[...], ...]}`

用法：python scripts/probe/cut_py.py <queries.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    # Windows 控制台默认编码是 cp936：不显式改 UTF-8 的话，中文字符串会被写成 GBK
    # 字节，而读端按 UTF-8 解码 → 满屏乱码的"差异"。这是对拍环境问题，不是切词差异，
    # 所以必须在这里钉死编码（否则整份对拍报告都是假红）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="strict")
    except AttributeError:  # pragma: no cover - 3.7 之前没有 reconfigure
        pass
    if len(argv) < 2:
        print("用法: python cut_py.py <queries.json>", file=sys.stderr)
        return 2
    queries_path = Path(argv[1]).resolve()
    if not queries_path.exists():
        print(f"找不到输入文件: {queries_path}", file=sys.stderr)
        return 2

    here = Path(__file__).resolve()
    repo_root = here.parent.parent.parent          # dsh-fish/
    p0 = repo_root.parent / "p0"                   # 大肥鱼生命系统/p0
    if not (p0 / "fish.py").exists():
        print(f"找不到 p0/fish.py: {p0}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(p0))

    from fish import index_tokens  # noqa: WPS433 —— 运行时导入是本脚本的目的

    queries = json.loads(queries_path.read_text(encoding="utf-8"))
    if not isinstance(queries, list):
        print("输入必须是字符串数组", file=sys.stderr)
        return 2
    payload = {
        "module": str((p0 / "fish.py")),
        "python": sys.version.split()[0],
        "tokens": [index_tokens(q) for q in queries],
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
