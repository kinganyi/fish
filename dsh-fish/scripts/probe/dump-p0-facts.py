#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 p0 抽取 spec 生成所需的**事实**（一次性，但可重复执行）。

为什么用 import 而不是正则解析：工具定义（18 个 inputSchema）是嵌套很深的 Python
字面量，正则解析出来的东西"看起来对"但很容易漏字段/丢 `required`；而 mcp_server.py
本身零第三方依赖，直接 import 拿到的就是**生产在跑的那一份**。

输出：stdout 一份 JSON（UTF-8 钉死，避免 Windows cp936 把中文写成 GBK 字节）。

用法：python scripts/probe/dump-p0-facts.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 只抽这些配置前缀：进注入包的预算与阈值（其余 56 个键与本批无关）。
CONFIG_PREFIXES = ("inject.", "push.", "decay.")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
        sys.stderr.reconfigure(encoding="utf-8", errors="strict")
    except AttributeError:  # pragma: no cover
        pass

    here = Path(__file__).resolve()
    dsh_fish = here.parent.parent.parent          # dsh-fish/
    p0 = dsh_fish.parent / "p0"
    if not (p0 / "fish.py").exists() or not (p0 / "mcp_server.py").exists():
        print(f"找不到 fish 源码目录: {p0}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(p0))

    import fish          # noqa: E402
    import mcp_server    # noqa: E402

    config = {}
    for key, spec in fish.CONFIG_DEFAULTS.items():
        if not str(key).startswith(CONFIG_PREFIXES):
            continue
        default, typ, allowed = spec
        config[key] = {"default": default, "type": typ, "allowed": list(allowed) if allowed else None}

    payload = {
        "python": sys.version.split()[0],
        "sources": {
            "fish": str(p0 / "fish.py"),
            "mcp": str(p0 / "mcp_server.py"),
        },
        "serverInfo": mcp_server.SERVER_INFO,
        "tools": mcp_server.TOOLS,
        "resources": mcp_server.RESOURCES,
        "tokenArg": mcp_server.TOKEN_ARG,
        "injectionSections": list(fish.INJECTION_SECTIONS),
        "injectionSectionAliases": dict(fish._SECTION_ALIASES),
        "injectionSectionNote": fish.INJECTION_SECTIONS_NOTE,
        "injectionTailLabels": dict(fish._TAIL_SECTION_LABELS),
        "injectionTailFullContains": fish._TAIL_FULL_CONTAINS,
        "injectionTailWorstAsOf": fish._INJECTION_TAIL_WORST_AS_OF,
        "truncationCountKeys": ["rules", "push", "board", "l2", "attention", "lessons"],
        "config": config,
        "memTypes": list(fish.KINDS) if hasattr(fish, "KINDS") else [],
        "decayTypes": ["episode", "fact", "lesson", "artifact", "rule", "identity"],
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
