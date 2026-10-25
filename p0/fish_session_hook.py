#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish · SessionStart hook（WorkBuddy / CodeBuddy 侧）
===================================================
把「权威当下」注入到每次会话开始。规范依据（2026-10-22 核对）：
CodeBuddy Hooks 文档 § SessionStart 决策控制：
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "..."}}

两种模式（用环境变量 FISH_HOOK_MODE 切换）：
  marker  → 只输出一行固定标记，用于确认 hook 是否被触发（默认）
  inject  → 输出真实的注入包（L0+L1+L2，硬预算）

stdout 只允许输出 hook 协议要求的 JSON；日志走 stderr。

## 2026-10-25（EVAL-261025-19）：身份来源从"硬编码 FISH_ACTOR"改为"会话键查库"

旧实现用 `FISH_ACTOR`（缺省硬编码 `workbuddy-main`）给子进程定身份——
于是**任何会话的注入包都宣称自己是 workbuddy-main**，而该会话真正的 token
可能属于另一个公民。评审把它列为 EVAL-19。

新实现只做两件事：
  1. 用 payload 的 `session_id` 拼出会话键（`codebuddy:<id>`，与 MCP/CLI 同口径），
     查 `session_tokens` / `citizens_all` 里**已经有**的公民；
  2. 查到就把 `--citizen <id>` 传给 inject；查不到就**诚实降级**（输出可执行提示），
     绝不编一个身份。

**为什么不在这里调 `fish session start`**：`start_session` 会轮换（吊销）同一
session_key 的旧凭证——hook 每次会话开始都签一次，会把 MCP 侧刚签发的 token
一起吊销。"查身份"不该有写副作用。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）

PY = sys.executable
FISH = HERE / "fish.py"
# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖（与 fish_push_hook 同口径，便于隔离测试）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
# ack 探针已验证通过（2026-10-22 05:13，两个会话均输出 FISH-HOOK-ACK）
# 现默认 inject：注入真实的「权威当下」
MODE = os.environ.get("FISH_HOOK_MODE", "inject")


def _emit(text: str) -> None:
    """输出 additionalContext（委托统一出口：编码只在一处决定）。"""
    hook_io.emit("SessionStart", text)


def _session_citizen(req):
    """按 payload 的 session_id 查**已有**的会话公民（只读，不签发）。

    返回 (citizen_id | None, 原因)。
    """
    sid = (req.get("session_id") or "").strip()
    if not sid:
        return None, "payload 里没有 session_id"
    key = "codebuddy:" + sid
    try:
        import fish
        conn = fish.connect(DATA)
        try:
            row = conn.execute(
                "SELECT citizen_id FROM session_tokens WHERE session_key=? "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY issued_at DESC LIMIT 1",
                (key, fish.now_utc())).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT citizen_id FROM citizens_all WHERE host_session_key=? "
                    "AND status='active' ORDER BY created_at LIMIT 1",
                    (key,)).fetchone()
            return (row[0] if row else None), None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 查身份失败不能拖垮会话
        return None, f"{type(exc).__name__}: {exc}"


def build_pack(citizen_id):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    cmd = [PY, str(FISH), "--root", str(DATA), "inject", "--json"]
    if citizen_id:
        cmd += ["--citizen", citizen_id]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       env=env, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"inject 失败: {p.stderr[:300]}")
    return json.loads(p.stdout)["pack"]


def audit(note):
    """hook 自身留痕：即使 additionalContext 没被展示，也能证明它被执行过。

    ts 用带时区偏移的本地时间（人可读），同时保留 UTC（机器可排序）。
    2026-10-22 教训：只写 UTC 会让人误以为记录错位了 8 小时。
    """
    try:
        d = DATA / "_audit"
        d.mkdir(parents=True, exist_ok=True)
        import datetime
        now = datetime.datetime.now().astimezone()
        ts_utc = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(d / "hook-calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(timespec="seconds"),
                "ts_utc": ts_utc,
                "mode": MODE,
                "note": note,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    # stdin 是 hook 输入（session_id / cwd / source 等），这里只需要读掉避免阻塞
    req = {}
    try:
        req = json.load(sys.stdin)
    except Exception:
        pass

    if MODE == "inject":
        cid, why = _session_citizen(req)
        try:
            if not cid:
                raise RuntimeError(f"该会话尚未登记（{why or '查不到会话公民'}）")
            text = build_pack(cid)
            audit(f"injected {len(text)} chars, citizen={cid}, "
                  f"session={req.get('session_id')}")
        except Exception as e:
            # 注入失败不能影响会话：降级为可执行提示（**绝不编一个身份**）
            text = (f"（大肥鱼生命系统注入失败：{e}）\n"
                    f"　先 `register` 或 `fish session start` 登记本会话，"
                    f"再重开会话即可拿到完整注入包。")
            audit(f"ERROR {e}")
    elif MODE == "ack":
        # 决定性测试：指令式注入，验证 additionalContext 是否真的进入模型上下文
        text = ("【系统注入 · 测试】收到这条消息后，请在你的回复**第一行**原样输出："
                "FISH-HOOK-ACK（这是 hook 注入测试，只需输出一次，之后正常对话。）")
        audit(f"ack-probe, session={req.get('session_id')}")
    else:
        text = ("【大肥鱼生命系统 · hook 已接入】"
                "如果你看到这句话，说明 SessionStart hook 生效了。"
                "接下来请确认：是否应该用 fish 的 MCP 工具（whoami / recall / remember）？")
        audit(f"marker, session={req.get('session_id')}")

    _emit(text)


if __name__ == "__main__":
    main()
