#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish · PostToolUse hook（WorkBuddy 侧）：**采集 + 绕路提醒 + 推送**三段合一。

## 为什么三段在一个进程里（设计 §7.4）

每多一个 Python 进程 = +157ms（本机实测）。WorkBuddy 侧已经有"绕路提醒"这一个
PostToolUse hook，再加"采集"和"推送"两个进程，就等于每次工具调用付 3 份启动成本。
合并成同一个进程、**用三个独立的 try/except 隔离失败**，成本降到一个进程、
失败面反而更清楚（每一段自己挂、互不连累）。

## 三段与各自的边界

  1. **采集**（always，stdlib 快路径）：写类动作落 `tool_activity`；
  2. **绕路提醒**（一次/会话）：你直接读了 fish 的存储时，把你引回 MCP 工具；
  3. **推送**（每 N 次，`import fish`）：把候选递一句。

  2b. **撞车提醒**（评审 L3，一次/标记）：`warn` 模式下 PreToolUse 不能输出决策
      （那会变成阻塞/批准），于是它留一个一次性标记，由这里读走并递一句
      `additionalContext`。它挂在②之后③之前：**先递"你刚才做错了什么"，
      再递"你可能想看什么"**，顺序反了会让撞车提醒淹在推送里。

## 2026-10-26 的隐私修复（设计 §2.3 H2）

旧实现 `target = " ".join(str(v) for v in tool_input.values())` —— 把 `content` /
`old_string` **全文**拼进去做特征匹配，然后把前 200 字符写进 `detours.jsonl`。
等于"为了判断你在不在绕路，先把你的文件内容抄进日志"。现在：
  · 特征匹配只看**路径类字段 + 命令**（`content` / `old_string` 永不参与）；
  · 日志只写 `tool / path / session / ts`，**不含任何内容**。

## 2026-10-26 QA Round 1（E1.8）：**匹配与落盘分离**

上一版仍把 `command` 当"路径类字段"拼进 `target`，然后把这个 `target`
原样写进 `detours.jsonl` —— 于是 `fish.py session end --token fsh_…` 这种
**文档化命令**会把凭证抄进本地审计文件（设计 §10.3 明令不记 command/token）。

修法是把两个用途**拆成两个函数**，而不是"少取几个字段"：

  · `detour_match_text()` —— **只在内存里**用于判断"是不是在绕路"，
    含 `command` / `cmd` / `pattern` / `glob`，**永不落盘**；
  · `detour_target()` —— **唯一落盘形态**，只认路径类字段；没有路径时
    Bash 折成 `bash:<解释器名或 len=N>#<sha8>`，pattern/glob 折成
    `pattern:len=N#<sha8>`。两者都不含原文，只留不可反推的短摘要。

落盘字段永远是封闭的五元组 `{ts, tool, path, session, host}`
（`test_activity_hooks.PrivacyTest.test_detour_log_never_writes_command` 钉住）。

## 2026-10-26 QA Round 2（P2 边界）：**"长得像名字"不是判据，"在词表里"才是**

上一版给首位 token 留了一条"原样落盘"的口子：只要它全由 ASCII `字母数字 . _ -`
组成、长度 ≤ 40，就当成解释器名原样写进 `bash:<名>#<sha8>`。

这条口子对 `python` / `git` 成立，**对凭证形态同样成立**：
`fsh_EDGECASE1234567890` 没有一个字符落在白名单之外，于是审计文件里落下
`bash:fsh_EDGECASE1234567890#<sha8>` —— 凭证形态原文照样进了本地文件。

根因是判据选错了维度：**字符集判据只能回答"这串字像不像名字"，回答不了
"这串字是谁给的"**。现在改成固定命令表（`_SAFE_COMMANDS`）查表：

  · 只有查表命中才保留名字，且落盘写的是**表里的常量**（小写、去 `.exe`），
    不是命令里的字节（`PYTHON.EXE` → `python`）；
  · 查不到 → 一律 `bash:len=N#<sha8>`；
  · 结论：审计行里的每个字节都来自本文件（常量 + 长度数字 + 8 位哈希），
    **没有任何一段是入参的原文**，因此不可能夹带凭证或内容。

协议依据：PostToolUse 支持 `additionalContext`（追加，不替换原结果）。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hook_io  # noqa: E402 —— 统一 stdout 出口（编码只在一处决定）
import fish_activity  # noqa: E402 —— 采集/心跳/计数的共享核心（不 import fish）

# 数据目录：默认本目录下的 data；可用 FISH_DATA 覆盖。
# 2026-10-26 修：与其它 hook 同口径（原来写死 HERE/"data"，测试无法隔离）。
DATA = Path(os.environ.get("FISH_DATA") or (HERE / "data"))
AUDIT = DATA / "_audit"
# 命中这些特征 = 在绕路（只看**路径类**入参 + 命令，不看内容）。
MARKERS = ("fish.py", "mcp_server.py", "data\\projects", "data/projects",
           "index.sqlite", "_audit", "projects\\ANYI", "projects/ANYI")
# ── 两个字段表：**用途不同，绝不许混用**（QA Round 1 · E1.8）──────────────
# 匹配用（只在内存里拼，永不落盘）：命令/搜索模式只用来判断"在不在绕路"。
_MATCH_FIELDS = ("file_path", "path", "notebook_path", "command", "cmd",
                 "pattern", "glob")
# 落盘用（设计 §10.3：审计只记路径）：**只允许真路径类字段**。
_LOG_PATH_FIELDS = ("file_path", "path", "notebook_path")
# Bash 命令的落盘形态来自这里：`bash:<词表里的命令名|len=N>#<sha8>`。
_BASH_FIELDS = ("command", "cmd")
# 搜索模式（不是路径）的落盘形态：`<field>:len=N#<sha8>`。
_PATTERN_FIELDS = ("pattern", "glob")
# ── 落盘时可保留名字的**固定命令表**（QA Round 2 · P2 边界）──────────────────
# 为什么不能用"字符集白名单"（旧的 `_is_safe_name`）：`fsh_EDGECASE1234567890`
# 全由 ASCII 字母数字组成、长度 22，字符集判据**放行**，凭证形态于是原文落盘。
# 现在只认这张表——而且落盘写的是**表里的常量**（`python`），不是命令里的字节
# （`PYTHON.EXE` 也落成 `python`），所以审计行里不出现任何入参原文。
_SAFE_COMMANDS = frozenset({
    # 解释器 / 运行时
    "python", "python3", "py", "node", "npx", "npm", "pnpm", "yarn", "deno",
    "go", "cargo", "java", "dotnet", "ruby", "perl", "php",
    # 版本控制
    "git", "gh",
    # 检索 / 文本
    "grep", "rg", "find", "findstr", "sed", "awk", "sort", "uniq",
    "cat", "type", "head", "tail", "wc", "diff",
    # 网络 / 传输
    "curl", "wget", "ssh", "scp", "rsync", "ping",
    # 文件系统
    "ls", "dir", "cp", "copy", "mv", "move", "rm", "del", "mkdir", "rmdir",
    "touch", "tar", "zip", "unzip",
    # shell / 宿主
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "echo", "cd",
})
# Windows 可执行后缀：`python.exe` 与 `python` 视作同一个命令。
# **只去这一个后缀、不做其它归一化**——`fsh_EDGECASE1234567890.exe` 因此仍进不了词表。
_EXE_SUFFIX = ".exe"


def _dbg(msg: str) -> None:
    """诊断输出（仅 FISH_HOOK_DEBUG=1）。默认静默。"""
    if os.environ.get("FISH_HOOK_DEBUG"):
        try:
            sys.stderr.write("[fish_posttool_hook] " + msg + "\n")
            sys.stderr.flush()
        except Exception:      # noqa: BLE001
            pass


def _host_from_argv(argv) -> str:
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return (argv[i + 1] or "").strip().lower() or "workbuddy"
        if arg.startswith("--host="):
            return (arg.split("=", 1)[1] or "").strip().lower() or "workbuddy"
    return "workbuddy"


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
    except Exception:      # noqa: BLE001
        return True


def _emit(text: str) -> None:
    """输出 additionalContext（委托统一出口：编码只在一处决定）。"""
    hook_io.emit("PostToolUse", text)


def _sha8(text: str) -> str:
    """短摘要（8 位十六进制）：**不可反推原文**，只用来把"同一条命令/同一次搜索"对上。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:8]


def _canonical_command(head: str) -> str:
    """首位 token → 固定词表里的**常量**；不在表里就返回空串（调用方退化成 `len=N`）。

    三步都只拿它做**查找**、不拿它做输出：去引号 → 取 basename → 小写 + 去 `.exe`。
    返回值必然是 `_SAFE_COMMANDS` 的元素，所以调用方写进审计的是本文件的常量，
    命令里的字节一个也带不出去（QA Round 2 · P2：`fsh_EDGECASE1234567890` 就是
    "看着像名字"但绝不能原样落盘的那类 token）。
    """
    word = (head or "").strip().strip("'\"")
    if not word:
        return ""
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(_EXE_SUFFIX):
        name = name[:-len(_EXE_SUFFIX)]
    return name if name in _SAFE_COMMANDS else ""


def _bash_log_label(command: str) -> str:
    """Bash 的**落盘**形态：`bash:<命令名|len=N>#<sha8>`（设计 §10.3 的"路径或摘要"）。

    为什么允许命令名：`python` / `grep` / `git` 不含参数、泄露不了什么，
    审计上却很有用（"这条是 python 干的"）。但**允许的范围是固定词表**
    （`_SAFE_COMMANDS`），不是"看起来像名字"——旧判据正是把凭证形态
    `fsh_EDGECASE1234567890` 当成了名字（QA Round 2 · P2）。
    为什么必须带 sha8：丢了原文之后，"这两条是不是同一条命令"仍要能对上，
    而 8 位哈希 + 高熵命令串不可反推，也不能当字典重建的锚。
    """
    text = (command or "").strip()
    if not text:
        return ""
    name = _canonical_command(text.split()[0])
    label = name if name else f"len={len(text)}"
    return f"bash:{label}#{_sha8(text)}"


def detour_match_text(tool_input) -> str:
    """绕路**判定**用文本：含命令/搜索模式，**只在本进程内存里存在，绝不落盘**。

    这是"匹配与落盘分离"的前一半（QA Round 1 · E1.8）：判定要看命令（`python fish.py`
    就是绕路），但同一个字符串不能顺手写进审计——所以判定与落盘用两个函数，
    落盘口（`detour_target`）**不接收**这个字符串。
    """
    if not isinstance(tool_input, dict):
        return ""
    parts = []
    for field in _MATCH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip()[:200])
    return " ".join(parts)


def detour_target(tool_input) -> str:
    """绕路审计的 **path 字段（唯一落盘形态）**：只认路径类字段 + 两种脱敏折法。

    优先级与形态：
      1. 真路径类字段（`file_path` / `path` / `notebook_path`）→ 原样（截 200 字）；
      2. 有 Bash 命令但没路径 → `bash:<词表命令名|len=N>#<sha8>`（**不含参数**）；
      3. 只有搜索模式 → `<field>:len=N#<sha8>`（模式是自由文本，不是路径）。

    返回空串表示"没什么可安全落盘的"——调用方据此**不写审计行**，而不是写空。
    """
    if not isinstance(tool_input, dict):
        return ""
    paths = []
    for field in _LOG_PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip()[:200])
    if paths:
        return " ".join(paths)[:200]
    for field in _BASH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return _bash_log_label(value)
    for field in _PATTERN_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            return f"{field}:len={len(value)}#{_sha8(value)}"
    return ""


def _hits_marker(tool_input) -> bool:
    """这一次调用有没有碰到 fish 的存储（判定**只看内存文本**）。"""
    text = detour_match_text(tool_input)
    return bool(text) and any(marker in text for marker in MARKERS)


def maybe_detour(req, host) -> None:
    """绕路提醒（一次/会话）+ 一条**脱敏**审计行。

    匹配与落盘分离（QA Round 1 · E1.8）：
      · 判定用 `_hits_marker()`（可含 command）；
      · 落盘 path 只来自 `detour_target()`（路径类 / `bash:…#sha8`）。
    因此**命令里带凭证也不会进文件**；连"没什么可安全落盘的"也宁可少写一行。
    """
    tool_input = req.get("tool_input")
    if not _hits_marker(tool_input):
        return
    target = detour_target(tool_input)
    if not target:              # 没有可安全落盘的形态 → 宁可不写（也不消耗一次提醒）
        return
    sid = str(req.get("session_id") or "")
    if not notify_once(sid):
        return
    try:
        AUDIT.mkdir(parents=True, exist_ok=True)
        with open(AUDIT / "detours.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": fish_activity.utc_now(),
                "tool": req.get("tool_name"),
                "path": target,          # 路径类或脱敏摘要，**没有内容/命令原文**（H2/E1.8）
                "session": sid,
                "host": host,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    _emit(
        "（大肥鱼生命系统提示：你刚才直接读了 fish 的存储文件。"
        "查询记忆请用 recall / resolve_code，写入请用 remember，"
        "看上下文用 neighbors——它们会正确维护编码、事件与衰减。"
        "本条提示每会话只出现一次。）"
    )


def maybe_warn_pending(req, host) -> None:
    """把 PreToolUse 欠的**撞车提醒**（`warn` 模式）递一句给模型（评审 L3）。

    为什么由这里递（而不是 PreToolUse 硬输出决策）：
      · `warn` 的语义就是**不阻塞**——输出 `permissionDecision` 会把它变成批准请求，
        在 approvals disabled 的宿主里直接等于"拒写"（那正是默认值改成 warn 的原因）；
      · 可 PostToolUse 的 `additionalContext` 是**追加**：不改变工具结果、不阻塞，
        而它出现在模型眼前的位置正好是"这次工具调用之后"；
      · 同一次调用就闭环：PreToolUse 留标记 → 工具执行 → 这里读走并递一句。
    只递一次：`take_warn_pending` 读走即写 `delivered_at`；过旧（>15 分钟）不递。
    """
    session_key = fish_activity.make_session_key(host, req.get("session_id"))
    pending = fish_activity.take_warn_pending(DATA, session_key)
    if not pending:
        return
    text = fish_activity.warn_notice_text(pending)   # 措辞只有一份（共享核心）
    if text:
        _emit(text)


def maybe_push(req, host) -> None:
    """每 N 次工具调用最多推一条候选（与 DSH 侧同一条推送管线）。"""
    sid = str(req.get("session_id") or "")
    n = fish_activity.bump_counter(DATA, session_id=sid, event="posttool-push", every_n=10)
    if n < 0 or n % 10 != 0:
        return
    try:
        import fish
    except Exception as exc:      # noqa: BLE001
        _dbg("import fish failed: %r" % (exc,))
        return
    conn = None
    try:
        conn = fish.connect(DATA)
        every_n = int(fish.get_config(conn, "push.after_tool_every_n_calls") or 10)
        if every_n <= 0 or n % every_n != 0:
            return
        if not fish.get_config(conn, "push.enabled"):
            return
        key = fish_activity.make_session_key(host, sid)
        citizen_id = fish_activity.resolve_citizen(conn, key)
        if not citizen_id:
            return                      # 无身份不猜
        project = None
        cwd = req.get("cwd") or ""
        if cwd:
            try:
                project = fish.resolve_project_by_cwd(conn, cwd)
            except Exception:           # noqa: BLE001
                project = None
        cands = fish.build_push_candidates(
            conn, DATA, citizen_id=citizen_id, project=project, session_key=key,
            limit=6, timing="tool_after")
        picked = fish.select_push_candidates(conn, cands, limit=1)
        if not picked:
            return
        c = picked[0]
        _emit(f"（fish 提示：{c['code']} {c['title']}（{c['reason']}）"
              f"——工具后推送，每 {every_n} 次调用最多一条。）")
        try:
            fish.append_push_log(
                conn, [c["code"]], channel="tool_after", citizen_id=citizen_id,
                session_key=key, candidate_id=c["code"],
                budget_tokens=int(fish.get_config(conn, "push.after_tool_budget_tokens")))
        except Exception as exc:        # noqa: BLE001
            _dbg("append_push_log failed: %r" % (exc,))
    except Exception as exc:      # noqa: BLE001
        _dbg("push failed: %r" % (exc,))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:      # noqa: BLE001
                pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host = _host_from_argv(argv)
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001
        pass
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:      # noqa: BLE001
        return
    try:
        req = json.loads(raw) if raw.strip() else {}
    except ValueError:
        fish_activity.heartbeat(DATA, event="PostToolUse", host=host,
                                payload_len=len(raw), bad_payload=True)
        return
    if not isinstance(req, dict):
        return

    fish_activity.heartbeat(
        DATA, event="PostToolUse", host=host,
        tool_name=str(req.get("tool_name") or ""),
        session=fish_activity.make_session_key(host, req.get("session_id")),
        payload_len=len(raw))

    # ── ① 采集（always）─────────────────────────────────────────────
    conn = None
    try:
        conn = fish_activity.connect(DATA)
        fish_activity.handle_post_tool_use(
            conn, host=host, payload=req, data_dir=DATA,
            snapshot_path=DATA / fish_activity.SNAPSHOT_RELPATH)
    except Exception as exc:      # noqa: BLE001 —— 采集失败不影响提醒与推送
        _dbg("collect failed: %r" % (exc,))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:      # noqa: BLE001
                pass

    # ── ② 绕路提醒（一次/会话）──────────────────────────────────────
    try:
        maybe_detour(req, host)
    except Exception as exc:      # noqa: BLE001
        _dbg("detour failed: %r" % (exc,))

    # ── ②b 撞车提醒（`warn` 模式唯一可见通道；欠一句就递一句）───────
    try:
        maybe_warn_pending(req, host)
    except Exception as exc:      # noqa: BLE001
        _dbg("warn pending failed: %r" % (exc,))

    # ── ③ 推送（每 N 次）────────────────────────────────────────────
    try:
        maybe_push(req, host)
    except Exception as exc:      # noqa: BLE001
        _dbg("push failed: %r" % (exc,))


if __name__ == "__main__":
    try:
        main()
    except Exception:      # noqa: BLE001 —— 兜底：hook 永远不能影响工具结果
        pass
