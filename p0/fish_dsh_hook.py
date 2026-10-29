#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish · DSH SessionStart hook —— 自动签发会话凭证并注入上下文
================================================================

为什么需要它
------------
DSH 的 MCP 子进程环境被 `scrubbedParentEnv()` 删掉所有 `DSH_*`
（依据：`checkout/packages/mcp/mcp-client/README.zh.md:138`），所以 MCP 侧
拿不到真实会话身份。`fish session start` 只能在能读到环境的一方（CLI）跑。

而 hook 是唯一把"会话开始时拿到的 session_id"和"会话上下文"连起来的地方
（依据：`hooks-claude-code` README「桥接从 session_id、transcript_path、cwd 与
hook_event_name 的基础字段……构建每个事件的 stdin payload」）。

所以本脚本做一件事：
    stdin(JSON，含 session_id) → 调 fish session start → 输出 additionalContext(token)

与旧 hook 的关系
----------------
本文件是 **DSH 专用**，与 `fish_session_hook.py`（WorkBuddy 用、做注入包）**并存不冲突**：
两者读同一份 fish 数据，但负责不同的事（一个注入内容，一个签发凭证）。

输入（stdin，JSON）
-------------------
    {"session_id": "dsh-xxx", "cwd": "...", "hook_event_name": "SessionStart", ...}

输出（stdout，JSON）
--------------------
    {"additionalContext": "..."}   ← harness 折叠进会话上下文

失败策略
--------
**绝不破坏会话启动**（hooks-claude-code 设计理念「每个失败点都受控」）：
任何异常都只输出一条简短提示（不注入 token），**退出码恒为 0**。

凭证纪律
--------
token 明文只出现在 stdout 的 additionalContext 里（用户 2026-10-24 确认接受
"落进会话日志"这一代价，缓解手段 = 24h TTL + 可吊销）。**本脚本不写任何文件、不打日志。**
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）

FISH = HERE / "fish.py"

# 凭证有效期（小时）。用户 2026-10-24 拍板：24h。
TOKEN_TTL_HOURS = 24


def log_child_stderr(tag, text):
    """子进程 stderr 落盘（hook 里 stdout 是契约面，不能污染；但也不能丢）。"""
    if not (text or "").strip():
        return
    try:
        import datetime
        d = DATA / "_audit"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook-stderr.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "tag": tag,
                "stderr": text[-4000:],
            }, ensure_ascii=False) + "\n")
    except Exception as exc:       # noqa: BLE001
        sys.stderr.write(f"[hook] hook-stderr 落盘失败（已忽略）：{exc!r}\n")


def _emit(text: str) -> None:
    """唯一正常出口：委托 `hook_io.emit`（2026-10-25 收敛到统一出口）。

    协议与编码纪律的完整依据见 `hook_io.py` 模块 docstring。这里保留两次
    事故的一句话摘要，避免读者非要跳文件才懂为什么这样写：
      · 顶层 `{"additionalContext": ...}` 会被 DSH 静默丢弃——必须包
        `hookSpecificOutput` 且 `hookEventName` 对得上；
      · `ensure_ascii=False` 的中文被 DSH 按非 UTF-8 读 → 乱码。

    守护：`test_hook_encoding.py`（行为测试 + 静态扫描）。
    """
    hook_io.emit("SessionStart", text)


def _citizen_exists(citizen_id: str) -> bool:
    """该公民是否存在于**当前库**（`_data_root()/index.sqlite`，只读）。

    为什么必须判：锚是**工作区级**文件，但 hook 也会在**测试/临时库**里跑
    （`FISH_ROOT` 指向临时 data）。临时库里没有锚里的公民 → 若仍传
    `--citizen`，`session start` 会报「公民不存在」，会话启动注入失败
    （2026-10-26 合并前基线实测 5 条红，就是这条）。所以「锚存在」还必须
    「公民在当前库存在」才生效；否则回退到按 `host_session_key` 归属的旧行为。
    """
    db = _data_root() / "index.sqlite"
    if not db.exists():
        return False
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            row = con.execute(
                "SELECT 1 FROM citizens_all WHERE citizen_id=? LIMIT 1",
                (citizen_id,)).fetchone()
            return bool(row)
        finally:
            con.close()
    except Exception:  # noqa: BLE001 —— 见 docstring
        return False


def _identity_anchor(session_id: str = "") -> dict:
    """工作区级身份锚：`p0/fish_identity.json`（可选文件，不存在=旧行为）。

    结构：`{"citizen": "<citizen_id>", "sessions": ["dsh:session-…"]}`。
    `sessions` 是**会话白名单**：只有列出的会话才用这个公民；没列/不匹配 →
    回退按 `host_session_key` 归属。**这是必须的**：锚是工作区级文件，而一个
    工作区里通常有多个会话（开发/评估/插件测试…），没有白名单就会把**所有**
    会话都签给同一个公民（2026-10-26 实测：三个会话被绑到 fish-主AI-3，
    撞车检测因此「自己撞自己」）。
    坏 JSON / 读失败一律当作"没有锚"——身份解析失败不该拖垮会话启动。
    """
    try:
        p = HERE / "fish_identity.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        sessions = data.get("sessions")
        if isinstance(sessions, list) and sessions:
            key = ("dsh:" + session_id) if session_id else ""
            if not any(key == str(s).strip() for s in sessions):
                return {}
        cid = str(data.get("citizen") or "").strip()
        if cid and not _citizen_exists(cid):
            return {}
        return data
    except Exception:  # noqa: BLE001 —— 见 docstring
        return {}


def _debug_note(session_id: str, status: str, detail: str = "") -> None:
    """可选诊断留痕：只在 `FISH_HOOK_DEBUG=1` 时写一行（BUG-04 的解法）。

    为什么默认不写：凭证纪律（本脚本不落盘任何东西）。但完全零留痕的代价是
    "hook 到底跑没跑"无法只读判断。所以给一个显式开关。
    **内容绝不含 token**，只记 session_id 前缀与结果状态。
    """
    if os.environ.get("FISH_HOOK_DEBUG", "") != "1":
        return
    try:
        import datetime
        # 与 CLI 的 `--root` 同义（见 _data_root）：原来这里多拼了一层 `data`，
        # 打开调试开关后日志落到了 `FISH_ROOT/data/_audit`，与库所在目录不一致。
        d = _data_root() / "_audit"
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now().astimezone()
        with open(d / "hook-dsh.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(timespec="seconds"),
                "session_id": session_id[:40],
                "status": status,
                "detail": detail[:200],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 诊断留痕绝不能影响主流程
        pass


def _skip(reason: str) -> int:
    """降级：不注入凭证，但告诉人为什么（且退出码为 0）。"""
    _emit(f"[fish] {reason}（本次调用会被记为「未归属」；手动跑 `fish session start` 可补）")
    return 0


def _probe() -> str:
    """失败时的现场诊断：一句话说清"为什么写不进去"。

    2026-10-24 事故：真实 hook 报 `attempt to write a readonly database`，
    但在同机 shell 里跑同一命令却成功。差异不在代码而在**执行环境**，
    所以失败时必须把环境事实一起带出来，否则只能靠猜。
    """
    bits: list[str] = []
    try:
        bits.append(f"cwd={Path.cwd()}")
    except Exception:  # noqa: BLE001
        bits.append("cwd=?")
    bits.append(f"user={os.environ.get('USERNAME', '?')}")
    try:
        # 同一个 _data_root：探针查的必须是 CLI 真正会写的那个库，
        # 否则"db_exists=False"只说明探针找错了地方（比不报更误导）。
        data = _data_root()
        db = data / "index.sqlite"
        bits.append(f"db_exists={db.exists()}")
        if db.exists():
            bits.append(f"db_size={db.stat().st_size}")
        # 关键探针：目录能不能写（WAL 需要写目录）
        probe = data / "_hook_probe.tmp"
        try:
            probe.write_text("x", encoding="ascii")
            probe.unlink()
            bits.append("dir_writable=YES")
        except Exception as exc:  # noqa: BLE001
            bits.append(f"dir_writable=NO({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001
        bits.append(f"probe_error={type(exc).__name__}")
    return " ".join(bits)


def _data_root() -> Path:
    """fish 数据目录：与 CLI 的 `--root` **同一个含义**。

    为什么提到函数里：这个路径有两个消费者（诊断留痕、候选查询），各自手写一遍
    `FISH_ROOT / "data"` 与 `FISH_ROOT` 两种写法必然漂——实测已经漂过一次：
    `_debug_note` 原来拼的是 `FISH_ROOT/data`，而 CLI 收到的是 `FISH_ROOT` 本身，
    于是开了 FISH_HOOK_DEBUG 也找不到那几行日志（"看不见就只能猜"的又一例）。
    """
    base = os.environ.get("FISH_ROOT", "").strip()
    return Path(base) if base else (HERE / "data")


def _workspace_candidates(cwd: str) -> list:
    """cwd 之下已映射的项目（**只读**；失败一律当作"没有候选"）。

    为什么由 hook 自己查、而不是让 `session start` 传过来：
      CLI 的 stdout 是 `key: value` 契约面（多行列表塞进去会污染解析）；
      而这不是"启动必须先知道"的事实，是**诊断**——查不到就少几行字，
      绝不该让会话启动失败。

    为什么必须是只读连接（`mode=ro`）：
      ① 这是会话启动路径，真实事故里这里报过 `attempt to write a readonly database`；
      ② 本次只需要 SELECT，用只读连接从结构上保证"不会写"（判据 E 的自证方式）。
    """
    try:
        db = _data_root() / "index.sqlite"
        if not db.exists():
            return []
        import sqlite3
        sys.path.insert(0, str(HERE))
        import fish                    # 同目录模块：归一化与候选查询只允许一处实现
        # as_uri() 会对中文/空格做百分号编码，SQLite 的 URI 文件名会解开它。
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            return fish.list_workspace_candidates(conn, cwd)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 —— 诊断信息永远不许影响会话启动
        _debug_note("(candidates)", "lookup_failed", type(exc).__name__)
        return []


def _workspace_hint(cwd: str) -> str:
    """「项目未声明」时的那几行：**下面有映射列出来，没有就保持原样**。

    2026-10-24 的真实踩坑：注入包只写「项目 : （未声明）」，人看不出是
    ①hook 没传 cwd、②cwd 不在映射里、③解析算法坏了。②里最常见的是
    "我就在某个已映射项目的父目录里"（`workspace/learn` 下放着 8 个项目，
    每个都映射了，只有父目录没有）。列出候选 → 抄一条 `fish workspace set` 就修好。
    而 cwd 之下一条映射都没有时，再写这些只是噪音（判据 D 就是钉这一条）。
    """
    cands = _workspace_candidates(cwd)
    if not cands:
        # 文案与改动前**逐字一致**：无候选时不啰嗦。
        return (f"  工作区     : {cwd}\n"
                f"              ↳ 该路径不在 workspace_map 里，所以没定项目。\n"
                f"                要归属就声明一次（之后自动）：\n"
                f"                fish workspace set \"{cwd}\" <项目前缀>\n")
    lines = [f"  工作区     : {cwd}",
             "              ↳ 该路径不在 workspace_map 里。"
             "这个工作区下有已映射的项目（可声明为当前项目）："]
    for c in cands:
        note = f"（{c['note']}）" if c.get("note") else ""
        lines.append(f"                · {c['project']}  ← {c['path_prefix']}{note}")
    lines.append("                声明一次即可（之后自动）：")
    lines.append("                fish workspace set \"<上面某个具体子目录>\" <项目>")
    return "\n".join(lines) + "\n"


def main() -> int:
    # ── 1. 读 stdin ────────────────────────────────────────────────
    # BUG-05 防御：Windows 下 sys.stdin 的默认编码取决于代码页/环境，
    # 而 payload 里带中文路径（cwd 就是中文目录）。显式按 UTF-8 解码。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 非标准流时保持默认
        pass

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001 —— 解析失败不能拖垮会话
        _debug_note("(unparsed)", "json_error", type(exc).__name__)
        return _skip(f"hook 输入不是合法 JSON ({type(exc).__name__})")

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        _debug_note("(empty)", "no_session_id", raw[:120])
        return _skip("hook 输入里没有 session_id")

    if not FISH.exists():
        _debug_note(session_id, "no_fish_py", str(FISH))
        return _skip(f"找不到 {FISH}")

    # ── 2. 把 session_id 喂给 CLI（它读的是环境变量）───────────────
    # 必须**覆盖**而不是 setdefault：hook 进程可能继承了一个无关的
    # DSH_SESSION_ID（实测踩过——payload 被无视、身份串到调用者的会话上）。
    # 语义上 payload 才是"本次事件对应的会话"，它永远优先。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("DSH_SESSION_ID", "CODEBUDDY_SESSION_ID", "CLAUDE_SESSION_ID"):
        env[key] = session_id

    # ── 3. 调 session start（P1-b 已冻结的实现，只喂不改）──────────
    # 注意：--root 是**全局参数**，必须放在子命令 `session` 之前。
    # TTL 不在这里传：它由 config 的 `session.token_ttl_hours` 控制（默认 24h）。
    cmd = [sys.executable, str(FISH)]
    fish_root = os.environ.get("FISH_ROOT", "").strip()
    if fish_root:  # 测试用：把数据目录指到临时位置（生产不设此变量）
        cmd += ["--root", fish_root]
    cmd += ["session", "start"]
    # 2026-10-26（身份锚，EVAL-261025-19 同族）：hook 必须能签给「工作区指定的身份」。
    # 没有锚时它会按 host_session_key 找到/新建一个 `dsh-session-<id>` 公民——而用户
    # 指定的身份可能是 `fish-主AI-3`，于是每次重启就分裂成两个公民（看板/归属各看各的）。
    # 锚文件：p0/fish_identity.json（{"citizen": "<id>"} 或 {"alias": "<名字>"}，可选）。
    # 为什么是文件而不是环境变量：WorkBuddy/DSH 两宿主的 hook 命令都没有 env 字段，
    # 而工作区级文件是两宿主都能读到、且改动即时生效（hook 每次执行现读）的落点。
    anchor = _identity_anchor(session_id)
    if anchor.get("citizen"):
        cmd += ["--citizen", str(anchor["citizen"])]
    elif anchor.get("alias"):
        cmd += ["--alias", str(anchor["alias"])]
    # 2026-10-24 加：把 payload 里的 cwd 交给 CLI，由 workspace_map 解析出项目。
    # 为什么在这里传而不是在 hook 里查：多一次进程启动、多一处可能忘传；
    # CLI 里已经有 workspace_map 的读路径，直接复用（`cmd_session_start` 的 --cwd）。
    # payload 的 cwd 是**调用方真实工作目录**——这是 MCP 面拿不到的东西
    # （MCP server 是单进程，它的 process.cwd() 只是启动目录，不是 AI 在哪干活）。
    cwd = str(payload.get("cwd") or "").strip()
    if cwd:
        cmd += ["--cwd", cwd]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env, timeout=20,
        )
        log_child_stderr("fish_dsh_hook.py:proc", proc.stderr)
    except subprocess.TimeoutExpired:
        return _skip("session start 超时")
    except Exception as exc:  # noqa: BLE001
        return _skip(f"session start 调用失败 ({type(exc).__name__})")

    if proc.returncode != 0:
        stderr_full = (proc.stderr or "").strip()
        lines = stderr_full.splitlines()
        tail = lines[-1] if lines else f"exit={proc.returncode}"
        diag = _probe()
        _debug_note(session_id, "session_start_failed", f"{tail} | {diag}")
        return _skip(f"session start failed: {tail}\n[env] {diag}")

    # ── 4. 解析 key: value 输出 ───────────────────────────────────
    fields: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if key and val:
            fields[key] = val

    token = fields.get("token", "")
    if not token:
        _debug_note(session_id, "no_token", (proc.stdout or "")[:150])
        return _skip("session start 没有返回 token")

    # ── 5. 注入 ───────────────────────────────────────────────────
    who = fields.get("display_name") or fields.get("account_id") or "(未命名)"
    evidence = fields.get("evidence", "?")
    expires = fields.get("expires_at", "?")
    project = fields.get("project") or ""
    # ⚠️ CLI 用 `-` 当"没有"的占位符（`project: -`），直接 truthy 判断会把
    # 它当成有项目，于是"未声明"的诊断分支永远进不去（实测踩过：情形②③都没提示）。
    if project.strip() in ("-", "（未指定）", "（未声明）"):
        project = ""
    # 只记身份与证据级别，**不记 token**（凭证纪律）
    _debug_note(session_id, "issued", f"{who} / {evidence} / {project or '-'}")

    # 2026-10-24 加：「工作区」行 + 项目来源说明。
    # 起因：live 会话显示「项目 : -」时，光看注入内容无法判断是
    # ①hook 没传 cwd、②cwd 不在 workspace_map 里、还是 ③解析算法坏了。
    # 这三种情况的处置完全不同，但输出一模一样——**看不见就只能猜**。
    # 现在把 cwd 与它的解析结果一起印出来，一眼可分；②再细分为"下面有已映射的
    # 子目录"（列候选）与"一条都没有"（保持原样），因为前者的下一步动作是明确的。
    cwd_line = ""
    if cwd:
        if project:
            cwd_line = f"  工作区     : {cwd}\n"
        else:
            cwd_line = _workspace_hint(cwd)
    elif not project:
        cwd_line = "  工作区     : （hook 载荷里没有 cwd 字段）\n"

    _emit(
        "[fish] 本次会话凭证已自动签发，无需你操作\n"
        f"  token      : {token}\n"
        f"  身份       : {who}（证据级别 {evidence}）\n"
        f"  项目       : {project or '（未声明）'}\n"
        f"{cwd_line}"
        f"  有效期至   : {expires}\n"
        "\n"
        "调用 fish 的 MCP 工具时请带上 `token=<上面这串>`，否则写入会记为「未归属」。\n"
        "凭证丢了不要紧：重跑 `fish session start` 即可（公民不变，只换凭证）。"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 —— 兜底：hook 永远不能拖垮会话启动
        _emit("[fish] hook 内部异常（本次跳过；手动跑 `fish session start` 可补）")
        sys.exit(0)
