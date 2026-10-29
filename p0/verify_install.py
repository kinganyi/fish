#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 发布占位（2026-10-29）：路径里的 `<用户名>` 是占位符，跑之前改成你自己的。
"""
端到端验证：完全按 WorkBuddy 的方式启动 fish —— 从 mcp.json 读 command/args/env，
不硬编码任何路径。这样验的是「配置本身可用」，而不是「我知道路径」。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

CFG = Path(r"C:\Users\<用户名>\.workbuddy\mcp.json")


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))["mcpServers"]["fish"]
    cmd = [cfg["command"]] + list(cfg.get("args") or [])
    env = {**os.environ, **(cfg.get("env") or {})}
    print(f"启动命令：{cmd[0]}")
    print(f"参数：{cmd[1:]}")
    print(f"环境：{cfg.get('env')}")
    print()

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         env=env, bufsize=1)

    def call(method, params=None, rid=1):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            msg["params"] = params
        p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        if not line:
            err = p.stderr.read()[:800]
            raise RuntimeError(f"无响应。stderr:\n{err}")
        return json.loads(line)

    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "workbuddy-sim", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    checks = []

    r = call("server/discover", {"_meta": meta}, 1)
    ok = bool(r.get("result", {}).get("supportedVersions"))
    checks.append(("server/discover", ok, r.get("result", {}).get("supportedVersions")))
    print(f"[{'PASS' if ok else 'FAIL'}] server/discover → {r.get('result', {}).get('supportedVersions')}")

    r = call("tools/list", {"_meta": meta}, 2)
    tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
    ok = len(tools) == 7
    checks.append(("tools/list", ok, tools))
    print(f"[{'PASS' if ok else 'FAIL'}] tools/list → {len(tools)} 个：{tools}")

    r = call("tools/call", {"_meta": meta, "name": "whoami", "arguments": {}}, 3)
    text = r.get("result", {}).get("content", [{}])[0].get("text", "")
    ok = "公民" in text or "安逸" in text or "resident" in text
    checks.append(("whoami", ok, text.strip().splitlines()[:2]))
    print(f"[{'PASS' if ok else 'FAIL'}] whoami → {text.strip().splitlines()[:2]}")

    r = call("tools/call", {
        "_meta": meta, "name": "remember",
        "arguments": {"project": "ANYI", "title": "WorkBuddy MCP 接入验证",
                      "body": "由 verify_install.py 端到端写入", "mem_type": "fact",
                      "tags": ["mcp", "install"]}}, 4)
    text = r.get("result", {}).get("content", [{}])[0].get("text", "")
    code = text.splitlines()[0] if text else ""
    ok = bool(code) and code.startswith("ANYI-") and len(code.split("-")) == 3
    checks.append(("remember", ok, code))
    print(f"[{'PASS' if ok else 'FAIL'}] remember → {code}")

    r = call("resources/read", {"_meta": meta, "uri": "fish://me"}, 5)
    txt = r.get("result", {}).get("contents", [{}])[0].get("text", "")
    ok = "三条铁律" in txt
    checks.append(("resources/read fish://me", ok, len(txt)))
    print(f"[{'PASS' if ok else 'FAIL'}] resources/read → {len(txt)} 字符")

    p.stdin.close()
    p.wait(timeout=10)
    print()
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"结果：{passed}/{len(checks)} 通过")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
