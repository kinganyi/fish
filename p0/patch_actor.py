#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 fish 的 MCP 配置补 FISH_ACTOR，让身份在进程重启后自动恢复。先备份再写。"""
import datetime
import json
import shutil
from pathlib import Path

CFG = Path(r"C:\Users\15596\.workbuddy\mcp.json")
ACTOR = "workbuddy-main"


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CFG.with_name(f"{CFG.name}.bak-{ts}-before-fishactor")
    shutil.copy2(CFG, bak)
    assert bak.stat().st_size == CFG.stat().st_size
    print(f"[1/3] 已备份 → {bak.name}")

    obj = json.loads(CFG.read_text(encoding="utf-8"))
    fish = obj["mcpServers"]["fish"]
    fish.setdefault("env", {})["FISH_ACTOR"] = ACTOR
    CFG.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[2/3] 已写入 FISH_ACTOR={ACTOR}")

    check = json.loads(CFG.read_text(encoding="utf-8"))
    env = check["mcpServers"]["fish"]["env"]
    assert env["FISH_ACTOR"] == ACTOR
    print(f"[3/3] 回读校验通过：env={env}")


if __name__ == "__main__":
    main()
