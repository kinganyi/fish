#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印一次真实的 modern MCP 协议对话（server/discover + tools/call）。"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = str(HERE / "data")


def main():
    p = subprocess.Popen(
        [sys.executable, str(HERE / "mcp_server.py"), "--root", ROOT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", bufsize=1)
    n = 0

    def call(method, params=None):
        nonlocal n
        n += 1
        msg = {"jsonrpc": "2.0", "id": f"r{n}", "method": method}
        if params is not None:
            msg["params"] = params
        raw = json.dumps(msg, ensure_ascii=False)
        print("→ " + raw[:200] + ("…" if len(raw) > 200 else ""))
        p.stdin.write(raw + "\n")
        p.stdin.flush()
        resp = json.loads(p.stdout.readline())
        out = json.dumps(resp, ensure_ascii=False)
        print("← " + out[:300] + ("…" if len(out) > 300 else ""))
        print()
        return resp

    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientInfo": {"name": "demo", "version": "0.1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    print("=== 1. 探测（modern 客户端第一件事）===")
    call("server/discover", {"_meta": meta})

    print("=== 2. 列工具（带 per-request _meta）===")
    r = call("tools/list", {"_meta": meta})
    print(f"    工具数：{len(r['result']['tools'])}，ttlMs={r['result'].get('ttlMs')}\n")

    print("=== 3. 调工具（现代无状态：每个请求自带版本）===")
    call("tools/call", {
        "_meta": meta,
        "name": "remember",
        "arguments": {"project": "ANYI", "title": "MCP dual-era 对齐完成",
                      "body": "按 2026-07-28 规范实现 server/discover",
                      "mem_type": "fact", "tags": ["mcp", "spec"]},
    })

    print("=== 4. 读资源（注入包，宿主可预加载）===")
    r = call("resources/read", {"_meta": meta, "uri": "fish://me"})
    text = r["result"]["contents"][0]["text"]
    print("    注入包前 3 行：")
    for line in text.splitlines()[:3]:
        print("    " + line)

    p.stdin.close()
    p.wait(timeout=5)


if __name__ == "__main__":
    main()
