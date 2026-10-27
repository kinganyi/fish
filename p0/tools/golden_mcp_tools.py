#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成/刷新 `tools/golden_mcp_tools.json`：18 个**旧** MCP 工具的 description+schema。

为什么要有这份 golden（判据 🟡-17，2026-10-27 三关复审）
----------------------------------------------------
旧判据只锁了三个工具的 `required` 字段：改一句描述、把 `parameters` 里的
`description` 改掉、甚至把整个参数挪进另一个工具，**都不会红**。而 MCP 的工具描述
是模型唯一的契约（它照着描述决定调什么），"描述悄悄变了"就是行为变了。

用法（只在**有意**改旧工具时跑，并把 diff 一起提交）：

    python tools/golden_mcp_tools.py            # 刷新
    python -m unittest test_corpus_search.McpCorpusSurfaceTest   # 复核

新增第 19+ 个工具（如 `corpus`）不进 golden：判据锁的是"旧的 18 个一字不动"，
新工具本来就该能演进。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
P0 = HERE.parent
GOLDEN = HERE / "golden_mcp_tools.json"

OLD_TOOLS = (
    "remember", "recall", "neighbors", "resolve_code", "read_body", "add_project",
    "supersede", "whoami", "register", "rename_citizen", "close_project",
    "reopen_project", "inject", "healthcheck", "session_start", "board", "kb", "kb_write",
)


def build() -> dict:
    sys.path.insert(0, str(P0))
    import mcp_server                    # noqa: PLC0415 —— 延迟导入：脚本要能被 lint 读
    by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
    missing = [name for name in OLD_TOOLS if name not in by_name]
    if missing:
        raise SystemExit("旧工具不见了：%s（判据会红，先查 TOOLS 表）" % ", ".join(missing))
    return {name: {"description": by_name[name]["description"],
                   "inputSchema": by_name[name]["inputSchema"]} for name in OLD_TOOLS}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if "--check" in argv:
        current = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
        if current != text:
            print("golden 与现有工具表不一致（有意改动就重跑本脚本并提交 diff）")
            return 1
        print("golden 一致（18 个旧工具）")
        return 0
    GOLDEN.write_text(text, encoding="utf-8", newline="\n")
    print("已写 %s（%d 个工具，%d 字节）" % (GOLDEN, len(data), len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
