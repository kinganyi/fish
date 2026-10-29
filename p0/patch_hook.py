#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SessionStart hook 写进 WorkBuddy 全局 settings.json。先备份，再写，再回读校验。"""
import datetime
import json
import shutil
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = Path(os.environ.get("FISH_WORKBUDDY_CFG",
                          Path.home() / ".workbuddy" / "settings.json"))
PY = os.environ.get("FISH_PYTHON", sys.executable)
HOOK = str(HERE / "fish_session_hook.py")

ENTRY = {
    "hooks": [{
        "type": "command",
        "command": f'"{PY}" "{HOOK}"',
        "timeout": 15,
    }]
}


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CFG.with_name(f"{CFG.name}.bak-{ts}-before-fishhook")
    shutil.copy2(CFG, bak)
    assert bak.stat().st_size == CFG.stat().st_size
    print(f"[1/4] 已备份 → {bak.name} ({bak.stat().st_size} B)")

    obj = json.loads(CFG.read_text(encoding="utf-8"))
    hooks = obj.setdefault("hooks", {})
    before = list(hooks)
    hooks.setdefault("SessionStart", [])
    # 幂等：已有 fish hook 就不重复加
    exists = any("fish_session_hook.py" in (h.get("command") or "")
                 for grp in hooks["SessionStart"] for h in (grp.get("hooks") or []))
    if not exists:
        hooks["SessionStart"].append(ENTRY)
    print(f"[2/4] 原有 hooks 段：{before}；SessionStart 组数：{len(hooks['SessionStart'])}"
          f"（{'已存在，未重复添加' if exists else '已新增'}）")

    CFG.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("[3/4] 已写入")

    check = json.loads(CFG.read_text(encoding="utf-8"))
    assert "hooks" in check and "SessionStart" in check["hooks"]
    assert check["hooks"]["SessionStart"], "SessionStart 为空"
    print("[4/4] 回读校验通过")
    print("\nSessionStart 配置：")
    print(json.dumps(check["hooks"]["SessionStart"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
