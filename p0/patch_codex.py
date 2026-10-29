#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fish 写进 Codex 的 config.toml。先备份，再追加，再用 tomllib 校验。

Codex 配置格式依据（2026-10-22 核对）：
  https://mintlify.wiki/openai/codex/configuration/mcp-servers
  [mcp_servers.<name>] + command/args + [mcp_servers.<name>.env]
注意：Codex 桌面版把 CODEX_HOME 设在 I:\app\codex，配置不在 ~/.codex/。
"""
import datetime
import shutil
import tomllib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
PY = r"C:\Users\15596\AppData\Local\Programs\Python\Python311\python.exe"
SERVER = str(HERE / "mcp_server.py")
DATA = str(HERE / "data")
PY = os.environ.get("FISH_PYTHON", sys.executable)

BLOCK = f"""

# ── 大肥鱼生命系统（2026-10-22 接入）─────────────────────────────────
# 跨宿主记忆：Codex 与 WorkBuddy 共用同一份记忆库
# FISH_ACTOR 决定"谁在读写"，Codex 用自己的名字，不冒用 workbuddy-main
[mcp_servers.fish]
command = '{PY}'
args = ['{SERVER}', '--root', '{DATA}']
startup_timeout_sec = 30
tool_timeout_sec = 120

[mcp_servers.fish.env]
PYTHONIOENCODING = "utf-8"
FISH_ACTOR = "codex-main"
"""


def main():
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CFG.with_name(f"{CFG.name}.bak-{ts}-before-fish")
    shutil.copy2(CFG, bak)
    assert bak.stat().st_size == CFG.stat().st_size
    print(f"[1/4] 已备份 → {bak.name} ({bak.stat().st_size} B)")

    text = CFG.read_text(encoding="utf-8")
    if "[mcp_servers.fish]" in text:
        print("[2/4] 已存在 [mcp_servers.fish]，跳过追加（避免重复）")
    else:
        if not text.endswith("\n"):
            text += "\n"
        CFG.write_text(text + BLOCK, encoding="utf-8")
        print("[2/4] 已追加 [mcp_servers.fish]")

    # 3. 用标准库 tomllib 校验（Python 3.11+）
    raw = CFG.read_text(encoding="utf-8")
    try:
        obj = tomllib.loads(raw)
    except Exception as e:
        print(f"[3/4] ✗ TOML 解析失败：{e}")
        print("      回滚：copy /Y", bak, CFG)
        return 1
    servers = obj.get("mcp_servers", {})
    print(f"[3/4] TOML 合法，mcp_servers = {list(servers)}")

    fish = servers.get("fish", {})
    print(f"[4/4] fish 配置：command={fish.get('command', '')[:40]}... "
          f"env={list(fish.get('env', {}))}")
    assert fish.get("env", {}).get("FISH_ACTOR") == "codex-main", "FISH_ACTOR 未写入"
    print("\n完成。重启 Codex 后应能看到 fish 的 8 个工具。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
