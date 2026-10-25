#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · PostToolUse 推送 hook（P1-a · D 批，2026-10-25）

职责
----
工具调用后，**每 N 次最多一次**，在结果后追加一条候选（`16` §5.7 的「工具后」档）。

与 `fish_posttool_hook.py` 的分工（重要，不要合并）
---------------------------------------------------
  · 那个管**绕路提醒**：你直接读了 `data/` 或 sqlite 时，把你引回 MCP 工具；
  · 这个管**推送**：把 `build_push_candidates` 选出的那一条递给你。
两者都是 `additionalContext`，但触发条件与内容完全不同。合并会有两个后果：
  ① "读文件提醒"与"推送候选"互相污染（一个每会话一次，一个每 N 次一次，节流逻辑打架）；
  ② 任一处出错就连累另一处——而它们各自都该独立失败。

三条纪律
--------
  1. **失败静默**：绝不影响工具调用。工具结果是主流程，推送只是锦上添花；
     让推送把工具打挂，AI 会直接放弃整个 hook（这比没有推送严重得多）。
  2. **频率闸前置**：不到点就立刻返回，**连 fish 都不 import**——
     每次工具调用都起一个 Python 进程已是成本，不能再叠加 import 整个 6600 行模块。
  3. **无身份不猜**：查不到 `session_key` 对应的公民就返回。
     "不知道是谁"不等于"谁都可以"。

配置（`~/.dsh/hooks.json`）
--------------------------
```json
"PostToolUse": [{"hooks": [{"type": "command",
  "command": "python \"I:/.../p0/fish_push_hook.py\"", "timeout": 10}]}]
```
⚠️ hook 在**启动时**装配 ⇒ 改完必须重启桌面端才生效。
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）

# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖。
# 为什么留这个口：hook 是**独立进程**，没有参数可以传目录。不留口就只能拿生产库试错，
# 而"用生产库验证一个会写库的 hook"是错的顺序（应该是副本先过、再上生产）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
COUNTER_DIR = DATA / "_audit" / "posttool-push"


def _dbg(msg: str) -> None:
    """诊断输出（仅 FISH_HOOK_DEBUG=1 时）。默认静默——生产环境不许往 stderr 写噪音。"""
    if os.environ.get("FISH_HOOK_DEBUG"):
        try:
            sys.stderr.write("[fish_push_hook] " + msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def _emit(text: str) -> None:
    """输出 additionalContext（追加，不替换工具结果）。委托统一出口。"""
    hook_io.emit("PostToolUse", text)


def _bump(session_id: str, every_n: int) -> int:
    """计数 +1，返回本次计数。失败返回 -1（调用方据此静默）。"""
    try:
        COUNTER_DIR.mkdir(parents=True, exist_ok=True)
        f = COUNTER_DIR / f"{(session_id or 'anon').replace(':', '_')}.n"
        n = 0
        if f.exists():
            try:
                # .lstrip("\ufeff")：PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写 BOM，
                # `int("\ufeff9")` 直接抛 ValueError → 计数永远归零 → 推送永远不触发。
                # 这是本机实测踩到的坑（2026-10-25），容错一下比要求"用 python 写文件"可靠。
                n = int(f.read_text(encoding="utf-8").strip().lstrip("\ufeff") or "0")
            except (ValueError, OSError):
                n = 0
        n += 1
        f.write_text(str(n), encoding="utf-8")
        return n
    except OSError:
        return -1


def _heartbeat(raw: str) -> None:
    """在**最开头**写一个心跳文件（时间戳 + payload 前 200 字符）。

    为什么需要它：本 hook 有六处 `return`（json 解析失败 / 无 session_id / 频率闸不到点 /
    无身份 / 无候选 / 异常），任何一处都会让"没有输出"和"根本没被执行"长得一模一样。
    心跳把这两种情况分开——这是诊断"hook 到底跑没跑"的唯一可靠办法。
    心跳写不进去就算了，不影响任何逻辑。
    """
    try:
        COUNTER_DIR.mkdir(parents=True, exist_ok=True)
        import datetime
        (COUNTER_DIR / "_heartbeat").write_text(
            datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            + " | " + (raw or "")[:200], encoding="utf-8")
    except Exception:
        pass


def main():
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    _heartbeat(raw)
    try:
        req = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        _dbg("bad payload: %r" % (e,))
        return

    session_id = req.get("session_id") or ""
    if not session_id:
        return

    # ── 频率闸（前置）：先算"到没到点"，不到就别去加载 fish ──────────────
    # 默认 10，可被 config 覆盖；但读 config 要连库，所以这里用**保守默认**，
    # 真正的值是 best-effort：连上库之后才读（见下）。两个值不一致时以库为准。
    try:
        n = _bump(session_id, 10)
    except Exception:
        return
    if n < 0:
        return
    if n % 10 != 0:      # 粗闸：先按 10 判，真正的 every_n 在下面复核
        return

    sys.path.insert(0, str(HERE))
    try:
        import fish
    except Exception as e:
        _dbg("import fish failed: %r" % (e,))
        return

    try:
        from pathlib import Path as _P
        root = DATA
        conn = fish.connect(root)
        try:
            every_n = int(fish.get_config(conn, "push.after_tool_every_n_calls") or 10)
            if every_n <= 0 or n % every_n != 0:
                return
            if not fish.get_config(conn, "push.enabled"):
                return

            key = "dsh:" + session_id
            row = conn.execute(
                "SELECT citizen_id FROM session_tokens "
                "WHERE session_key=? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY issued_at DESC LIMIT 1",
                (key, fish.now_utc())).fetchone()
            if not row:
                return                     # 无身份不猜
            citizen_id = row[0]

            # 当前项目：优先档期，其次 workspace_map(cwd)，都没有就不指定
            project = None
            try:
                cwd = req.get("cwd") or ""
                if cwd:
                    project = fish.resolve_project_by_cwd(conn, cwd)
            except Exception:
                project = None

            cands = fish.build_push_candidates(
                conn, root, citizen_id=citizen_id, project=project,
                session_key=key, limit=6)
            picked = fish.select_push_candidates(conn, cands, limit=1)
            if not picked:
                return
            c = picked[0]
            _emit(f"（fish 提示：{c['code']} {c['title']}（{c['reason']}）"
                  f"——工具后推送，每 {every_n} 次调用最多一条。）")
            # 记一条 push_log：工具后推送也要进度量（否则这类永远算不出采纳率）。
            # channel 必须是 `append_push_log` 白名单里的值：inject / hook / tool_after / situational
            # （2026-10-25 实测：写成 after_tool 会被明确拒绝——有这个校验才没静默写脏数据）。
            try:
                fish.append_push_log(
                    conn, [c["code"]], channel="tool_after",
                    citizen_id=citizen_id, session_key=key,
                    candidate_id=c["code"],
                    budget_tokens=int(fish.get_config(conn, "push.after_tool_budget_tokens")))
            except Exception as e:
                _dbg("append_push_log failed: %r" % (e,))
        finally:
            conn.close()
    except Exception as e:
        _dbg("push hook failed: %r" % (e,))
        return                             # 失败静默：绝不影响工具调用


if __name__ == "__main__":
    main()
