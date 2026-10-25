#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 PostToolUse 入口引导 hook 写进 WorkBuddy 全局 settings.json。先备份。"""
import datetime
import json
import shutil
from pathlib import Path

CFG = Path(r"C:\Users\15596\.workbuddy\settings.json")
PY = "C:/Users/15596/AppData/Local/Programs/Python/Python311/python.exe"
HOOK = "I:/app/DSH-Home/workspace/learn/大肥鱼生命系统/p0/fish_posttool_hook.py"

ENTRY = {
    "matcher": "Read|Bash|Glob|Grep|Write|Edit",
    "hooks": [{
        "type": "command",
        "command": f'"{PY}" "{HOOK}"',
        "timeout": 10,
    }]
}


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CFG.with_name(f"{CFG.name}.bak-{ts}-before-posttool")
    shutil.copy2(CFG, bak)
    print(f"[1/4] 已备份 → {bak.name}")

    obj = json.loads(CFG.read_text(encoding="utf-8"))
    hooks = obj.setdefault("hooks", {})
    hooks.setdefault("PostToolUse", [])
    exists = any("fish_posttool_hook.py" in (h.get("command") or "")
                 for grp in hooks["PostToolUse"] for h in (grp.get("hooks") or []))
    if not exists:
        hooks["PostToolUse"].append(ENTRY)
    print(f"[2/4] PostToolUse 组数：{len(hooks['PostToolUse'])}"
          f"（{'已存在' if exists else '已新增'}）")

    CFG.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    check = json.loads(CFG.read_text(encoding="utf-8"))
    assert "PostToolUse" in check["hooks"], "写入失败"
    assert "SessionStart" in check["hooks"], "原有 SessionStart 丢失"
    print("[3/4] 回读校验通过（SessionStart 与 PostToolUse 并存）")
    print("[4/4] 当前 hooks：", list(check["hooks"]))
    print("\nPostToolUse 配置：")
    print(json.dumps(check["hooks"]["PostToolUse"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
