#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Python 能否驱动 DSH SDK 运行时 —— 最小验证（2026-11-22）。

只回答一个问题：**别的语言（Python）能不能通过 JSON-RPC over stdio 跟
DSH 的 Harness 运行时说上话。**

协议来自 `packages/sdk/protocol/src/types.ts`：
  · 三个客户端请求：`initialize` / `session/prompt` / `shutdown`
  · 帧格式：按换行分帧的 JSON-RPC 2.0
  · stdout 只承载 JSON-RPC，诊断走 stderr

判据：
  J1 `initialize` 返回 `serverInfo.name == "deepseek-harness-sdk-runtime"`
  J2 返回体是合法 JSON-RPC 2.0（有 jsonrpc/id/result 三件套）
  J3 `shutdown` 后进程以 0 退出

变异自证见同目录 test_sdk_runtime_probe.py。
"""

import json
import os
import subprocess
import sys
import threading
import time

CLI = r"I:\app\DSH-Home\checkout\apps\cli\lib\bin.js"
PROFILE = "sdktest"
DSH_HOME = r"I:\app\DSH-Home\home"
SERVER_NAME = "deepseek-harness-sdk-runtime"


def _send(proc, obj):
    """按换行分帧写一帧。"""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()


def _recv(proc, timeout=90.0):
    """读一帧；超时返回 None。stderr 由后台线程单独收集。"""
    result = {}

    def reader():
        try:
            raw = proc.stdout.readline()
            result["raw"] = raw
        except Exception as exc:                      # noqa: BLE001
            result["err"] = repr(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    raw = result.get("raw") or b""
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"__parse_error__": raw.decode("utf-8", "replace")[:400]}


def main():
    env = dict(os.environ, DSH_HOME=DSH_HOME)
    proc = subprocess.Popen(
        ["node", CLI, "--profile", PROFILE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=r"I:\app\DSH-Home\checkout", env=env,
    )
    stderr_buf = []
    threading.Thread(target=lambda: stderr_buf.append(proc.stderr.read()), daemon=True).start()

    try:
        # ---- 握手
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"cwd": os.getcwd(), "provider": "deepseek-official",
                                "model": "deepseek-flash"}})
        reply = _recv(proc, timeout=90)

        if reply is None:
            print("  J0 失败：initialize 无响应（超时或进程已退出）")
            print("  stderr 尾部：")
            err = (stderr_buf[0] if stderr_buf else b"").decode("utf-8", "replace")
            for l in err.split("\n")[-15:]:
                if l.strip():
                    print("    " + l[:140])
            return 2

        print("  initialize 原始回包：")
        print("    " + json.dumps(reply, ensure_ascii=False)[:400])

        # ---- J2 JSON-RPC 形状
        ok_shape = (reply.get("jsonrpc") == "2.0" and "id" in reply
                    and ("result" in reply or "error" in reply))
        print("  J2 JSON-RPC 形状: %s" % ("PASS" if ok_shape else "FAIL"))

        # ---- J1 serverInfo
        info = (reply.get("result") or {}).get("serverInfo") or {}
        name = info.get("name")
        ok_name = (name == SERVER_NAME)
        print("  J1 serverInfo.name = %r -> %s" % (name, "PASS" if ok_name else "FAIL"))

        if reply.get("error"):
            print("  ⚠ 服务端返回错误：%s" % json.dumps(reply["error"], ensure_ascii=False)[:300])

        # ---- J3 shutdown
        if ok_shape:
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "shutdown"})
            _recv(proc, timeout=30)
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = -1
            print("  J3 shutdown 后退出码 = %s -> %s" % (code, "PASS" if code == 0 else "FAIL"))
        return 0 if (ok_shape and ok_name) else 1
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:                          # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
