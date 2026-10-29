#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fish 写进 WorkBuddy 的 mcp.json —— 先备份，再写，再回读校验。"""
import datetime
import json
import shutil
import os
import sys
from pathlib import Path

# 2026-10-29（发布前脱敏）：路径不再硬编码本机绝对路径。
# 环境变量优先（便于在不标准的环境里跑），否则用"脚本所在目录 / 家目录"这两个
# 任何克隆都能算出来的值——别人 clone 下来直接能跑，本机也照旧。
HERE = Path(__file__).resolve().parent
CFG = Path(os.environ.get("FISH_WORKBUDDY_CFG",
                          Path.home() / ".workbuddy" / "mcp.json"))
PY = os.environ.get("FISH_PYTHON", sys.executable)
SERVER = str(HERE / "mcp_server.py")
DATA = str(HERE / "data")

FISH = {
    "type": "stdio",
    "command": PY,
    "args": [SERVER, "--root", DATA],
    "env": {"PYTHONIOENCODING": "utf-8"},
    "description": "大肥鱼生命系统：记忆编码 / 身份 / 邻近回忆",
    "timeout": 30000,
}


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CFG.with_name(f"{CFG.name}.bak-{ts}-before-fish")

    # 1. 备份（保留元数据）
    shutil.copy2(CFG, bak)
    assert bak.stat().st_size == CFG.stat().st_size, "备份大小不一致，中止"
    print(f"[1/4] 已备份 → {bak.name} ({bak.stat().st_size} B)")

    # 2. 读原文件
    text = CFG.read_text(encoding="utf-8")
    obj = json.loads(text)
    before = list(obj.get("mcpServers", {}))
    print(f"[2/4] 原有 server：{before}")

    # 3. 写入 fish（已存在则覆盖）
    obj.setdefault("mcpServers", {})["fish"] = FISH
    CFG.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[3/4] 已写入 fish（共 {len(obj['mcpServers'])} 个 server）")

    # 4. 回读校验
    check = json.loads(CFG.read_text(encoding="utf-8"))
    assert "fish" in check["mcpServers"], "回读失败：fish 不存在"
    assert all(k in check["mcpServers"] for k in before), "回读失败：原有 server 丢失"
    assert check["mcpServers"]["fish"]["command"] == PY
    print("[4/4] 回读校验通过")
    print("\n当前配置：")
    for name, cfg in check["mcpServers"].items():
        print(f"  - {name:16} type={cfg.get('type', '(推断)'):14} "
              f"disabled={cfg.get('disabled', False)}")


if __name__ == "__main__":
    main()
