#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish · PostToolUse hook（第 2 层入口引导）
=========================================
当 AI **直接读取或操作** fish 的存储（data/ 目录、fish.py、sqlite）时，
在工具结果后追加一句提示，把它引回 MCP 工具。

设计原则（重要）：
  - **只提醒，不阻止**。人也要能读这些文件。
  - **每会话只提醒一次**，避免刷屏。
  - 失败绝不影响会话。

协议依据（2026-10-22 核对 CodeBuddy Hooks 文档）：
  PostToolUse 支持 additionalContext（追加，不替换原结果）
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）

DATA = HERE / "data"
AUDIT = DATA / "_audit"
# 命中这些特征 = 在绕路
MARKERS = ("fish.py", "mcp_server.py", "data\\projects", "data/projects",
           "index.sqlite", "_audit", "projects\\ANYI", "projects/ANYI")


def notify_once(session_id: str) -> bool:
    """每会话只提醒一次。返回 True 表示本次应提醒。"""
    if not session_id:
        return True
    try:
        d = AUDIT / "posttool-notified"
        d.mkdir(parents=True, exist_ok=True)
        flag = d / f"{session_id}.flag"
        if flag.exists():
            return False
        flag.write_text("1", encoding="utf-8")
        return True
    except Exception:
        return True


def _emit(text: str) -> None:
    """输出 additionalContext（委托统一出口：编码只在一处决定）。"""
    hook_io.emit("PostToolUse", text)


def main():
    try:
        req = json.load(sys.stdin)
    except Exception:
        return

    ti = req.get("tool_input") or {}
    target = " ".join(str(v) for v in ti.values() if isinstance(v, str))
    if not any(m in target for m in MARKERS):
        return

    sid = req.get("session_id") or ""
    if not notify_once(sid):
        return

    # 留痕（用于度量"绕路"比例）
    try:
        AUDIT.mkdir(parents=True, exist_ok=True)
        import datetime
        now = datetime.datetime.now().astimezone()
        with open(AUDIT / "detours.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(timespec="seconds"),
                "tool": req.get("tool_name"),
                "session": sid,
                "target": target[:200],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    _emit(
        "（大肥鱼生命系统提示：你刚才直接读了 fish 的存储文件。"
        "查询记忆请用 recall / resolve_code，写入请用 remember，"
        "看上下文用 neighbors——它们会正确维护编码、事件与衰减。"
        "本条提示每会话只出现一次。）"
    )


if __name__ == "__main__":
    main()
