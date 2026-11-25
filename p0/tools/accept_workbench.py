#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A-1~A-13 的可执行验收（`docs/96-工作台-产品规格-20261123.md` §十）—— 评审 R-02 的落点

## 这份脚本回答什么

`verify_workbench_fixes.py` 覆盖的是 QA 的 F-1~F-6（深翻页/性能/折叠/项目归属/只读证据）。
`docs/96` §十那 13 条产品判据（A-1~A-13）此前**一条机检都没有**（评审 R-02 原文：
"96 的 A-1~A-13 没有一条机检"）。这个脚本把 13 条逐条落成断言：

    A-1  五源都在首屏        五源计数 > 0，且首屏 40 条里 ≥3 种类型
    A-2  无假数据            读口不可达时整屏只有说明，可见文本里**没有数字**
    A-3  同毫秒顺序稳定       同一请求跑 3 次 key 序列完全一致，且 sk 严格递减
    A-4  游标无重无漏         limit=40 翻到底：rawSum == 本刀条数、零重复、游标单调
    A-5  「在线」不撒谎       窗口 30→1440 分钟数字必须变；量词必须是「会话」不是「人」
    A-6  不假装有读动作       全量条目文本里没有「读了/读取/阅读」这类编出来的动词
    A-7  点击联动             深链选中态右栏出现该条的字段组；无选中时不出现
    A-8  左栏筛选真生效       scope=project:CZHY 的每条 project 都是 CZHY（再点取消=all 会混）
    A-9  折叠可展开           折叠条带得动明细（count==len(items) 且可展开）
    A-10 空态四句不同         四句两两不同；①在渲染后的页面里，②③④在构建产物里
    A-11 首屏与滚动性能       首屏服务端调用中位 <1s；虚拟滚动下 DOM 行节点 <200 而流里有上千条
    A-12 每个数字有出处       页签一每块都有「来源+口径」入口（ⓘ），机检 DOM 计数 ≥5
    A-13 宿主来自会话键前缀   库内 host 列与前缀不一致时，界面/接口必须用前缀；大小写归一

## 变异自证（判据必须能红）

每条能变异的判据都配一个**变异运行**：把同一断言喂给一个已知为坏的输入，要求它红；
红不了就说明这条判据没有判别力，本脚本会把它记为 `MUTATION-FAIL`（不是 PASS）。
变异只发生在进程内存里（猴子补丁 / 构造字符串），**不改源码、不动库**。

## 用法

    python -X utf8 p0\\tools\\accept_workbench.py --api http://127.0.0.1:8790
    python -X utf8 p0\\tools\\accept_workbench.py --api http://127.0.0.1:8790 --db <库路径> --static http://127.0.0.1:8799
    python -X utf8 p0\\tools\\accept_workbench.py --api ... --mutate a08     # 故意破坏一条 → 必须红

`--static` 缺失或浏览器起不来时，DOM 类判据（A-2/A-7/A-11 的 DOM 半边/A-12）报 **SKIP**
并写明"需要什么"，**不冒充通过**（SKIP 与 PASS 是两件事）。

退出码：全 PASS 0；有 FAIL 1；全 SKIP 也算 1（"没验"不等于"通过"）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
if _P0 not in sys.path:
    sys.path.insert(0, _P0)

DEFAULT_DB = r"I:\app\fish-data\data\index.sqlite"
PROJECT = "CZHY"
UI_DIR = os.path.join(_REPO, "ui")
CODES_PATH = os.path.join(UI_DIR, "codes.json")
DIST_DIR = os.path.join(UI_DIR, "dist")

#: 空态文案的唯一真相（`ui/codes.json`）。A-2/A-10 用它，所以放模块级。
CODES: dict = json.load(open(CODES_PATH, encoding="utf-8")) if os.path.exists(CODES_PATH) else {}

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]

RESULTS: list[tuple[str, str, str]] = []


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(cid: str, status: str, detail: str) -> None:
    RESULTS.append((cid, status, detail))
    print(f"{status:<4}  {cid}  {detail}")


# ── HTTP 小工具（只读）──────────────────────────────────────────────────
def http_json(url: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── DOM 探针：无头浏览器 + CDP（本机 Edge 的 --dump-dom 输出 0 字节）─────────
def find_browser() -> str | None:
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    return None


class _Ws:
    """极简 WebSocket 客户端（只为 CDP 的 `Runtime.evaluate` 服务，无第三方依赖）。

    为什么自己写：本机没有 websocket-client；而 `--dump-dom` 在 Edge 158 上输出 0 字节。
    CDP 的 HTTP `/json/*` 面不提供求值能力，`Runtime.evaluate` 只能走 WS。
    """

    def __init__(self, url: str, timeout: float = 25.0) -> None:
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手时连接断开")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0]
        if b" 101 " not in status:
            raise RuntimeError("WS 握手失败：" + status.decode("latin1"))

    def send(self, text: str) -> None:
        data = text.encode("utf-8")
        header = bytearray([0x81])
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        while True:
            if len(self.buf) >= 2:
                opcode = self.buf[0] & 0x0F
                length = self.buf[1] & 0x7F
                offset = 2
                if length == 126:
                    if len(self.buf) < 4:
                        self.buf += self.sock.recv(65536)
                        continue
                    length = struct.unpack(">H", self.buf[2:4])[0]
                    offset = 4
                elif length == 127:
                    if len(self.buf) < 10:
                        self.buf += self.sock.recv(65536)
                        continue
                    length = struct.unpack(">Q", self.buf[2:10])[0]
                    offset = 10
                if len(self.buf) < offset + length:
                    self.buf += self.sock.recv(65536)
                    continue
                payload = self.buf[offset:offset + length]
                self.buf = self.buf[offset + length:]
                if opcode == 0x8:
                    raise RuntimeError("WS 被对端关闭")
                if opcode in (0x1, 0x2):
                    return payload.decode("utf-8", "replace")
                continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接断开")
            self.buf += chunk

    def call(self, msg_id: int, method: str, params: dict | None = None) -> dict:
        self.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.recv_text())
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} 失败：{message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


#: 应用页的标题（`ui/index.html` 的 `<title>`）——页面身份的唯一锚点（P1-4）。
APP_TITLE = "fish 现场"

#: 一次页面探测要取的量（都是机检判据的输入）。
_PROBES = {
    "text": "document.body ? document.body.innerText : ''",
    "rows": "document.querySelectorAll('[data-row]').length",
    "origins": "document.querySelectorAll('details summary[aria-label$=\"来源与口径\"]').length",
    # 页面身份（P1-4）：浏览器错误页也有 `body.innerText`，光看文本会把
    # "嗯… 无法访问此页面… ERR_CONNECTION_REFUSED" 当成应用页 → 假红。
    "title": "document.title",
    "rootKids": "(document.getElementById('root') || {childElementCount: -1}).childElementCount",
    # 列表**持有**多少条（`VirtualList` 容器上的 `data-count`）：A-11 的机检口径要用它。
    # 少了这个数，"DOM 行节点 < 200" 在只载入 40 条时永远成立 → 判据没有判别力（P1-2）。
    "count": "(() => { const el = document.querySelector('[role=list]');"
             " return el && el.hasAttribute('data-count')"
             " ? Number(el.getAttribute('data-count')) : null; })()",
}

#: 滚到底触发无限加载（A-11 要"载入 ≥200 条"才谈得上"虚拟滚动在起作用"）。
_SCROLL_BOTTOM = ("(() => { const el = document.querySelector('[role=list]');"
                  " if (!el) return -1; el.scrollTop = el.scrollHeight;"
                  " return el.scrollTop; })()")


class _PageNotApp(RuntimeError):
    """读到的不是应用页（浏览器错误页 / 空白页）——这不是判据失败，是托管不可达（P1-4）。"""


def _page_identity(probe: dict) -> tuple[bool, str]:
    """页面身份判据：标题对得上，或 `#root` 里挂着节点——两者都否就不是应用页。

    为什么不能只看文本（P1-4）：Edge 的错误页也有 `document.body.innerText`
    （"嗯… 无法访问此页面… ERR_CONNECTION_REFUSED"，实测 chars=94）。旧探针把它当应用页，
    于是 8799 一停就有 5 条 DOM 判据报 FAIL——那是**假红**：环境不可达不等于产品不达标。
    改成 SKIP 之后，"没验"与"不合格"才分得清。
    """
    title = (probe.get("title") or "").strip()
    kids = probe.get("rootKids")
    kids = int(kids) if isinstance(kids, (int, float)) else -1
    if title == APP_TITLE or kids > 0:
        return True, ""
    head = (probe.get("text") or "").strip().replace("\n", " ")[:80]
    return False, (f"静态托管不可达：读到的不是应用页"
                   f"（title={title!r}，#root 子节点={kids}，文本前 80 字={head!r}）")


def dom_probe(url: str, wait_ms: int = 6000, attempts: int = 3,
              scroll_rounds: int = 0) -> dict:
    """渲染一个页面并取回 `{text, rows, origins, title, rootKids, count}`。

    起不来抛 `RuntimeError`（调用方判 SKIP）；读到的不是应用页抛 `_PageNotApp`
    （同为 RuntimeError 子类，但**不重试**——托管都不可达了，重试三次只是白等）。

    为什么要重试：本机无头 Edge 偶发"调试端口 15 秒内没起来"（连续跑多条判据时更容易撞上），
    一次失败就判 SKIP 会让"环境抖一下"看起来像"功能没有"。三次都起不来才算真起不来。

    `scroll_rounds` > 0 时先滚到底 N 轮，把无限加载（每轮 40 条）跑起来再读数——A-11 用。
    """
    errors = []
    for attempt in range(1, attempts + 1):
        try:
            return _dom_probe_once(url, wait_ms, scroll_rounds)
        except _PageNotApp:
            raise
        except RuntimeError as exc:
            errors.append(f"第{attempt}次：{exc}")
            time.sleep(1.5)
    raise RuntimeError("；".join(errors))


def _dom_probe_once(url: str, wait_ms: int, scroll_rounds: int = 0) -> dict:
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到无头浏览器（Edge/Chrome）")
    profile = tempfile.mkdtemp(prefix="fish-wb-accept-")
    port = _free_port()
    proc: subprocess.Popen | None = None
    ws: _Ws | None = None
    try:
        proc = subprocess.Popen(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={profile}",
             f"--remote-debugging-port={port}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    pages = [t for t in json.loads(resp.read().decode()) if t.get("type") == "page"]
                    if pages:
                        target = pages[0]
                        break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.25)
        if not target:
            raise RuntimeError("CDP 目标没起来（调试端口绑不上）")
        ws = _Ws(target["webSocketDebuggerUrl"])
        ws.call(1, "Page.enable")
        ws.call(2, "Runtime.enable")
        ws.call(3, "Page.navigate", {"url": url})
        time.sleep(wait_ms / 1000.0)
        for _ in range(scroll_rounds):
            # 一轮 = 一次无限加载（40 条）：先滚到底，再等它请求 + 合并 + 重渲染。
            ws.call(4, "Runtime.evaluate",
                    {"expression": _SCROLL_BOTTOM, "returnByValue": True})
            time.sleep(0.6)
        out = {}
        for index, (name, expression) in enumerate(_PROBES.items(), start=10):
            result = ws.call(index, "Runtime.evaluate",
                             {"expression": expression, "returnByValue": True})
            out[name] = result.get("result", {}).get("value")
        ok_page, why = _page_identity(out)
        if not ok_page:
            raise _PageNotApp(why)
        if not (out.get("text") or "").strip():
            raise RuntimeError("页面文本为空")
        return out
    finally:
        # P2-5：关浏览器与删临时 profile **都**要在 finally 里，且要"确认死透"再删。
        # 顺序有讲究：先请浏览器自己 `Browser.close`（它会释放 profile 里的文件句柄），
        # 再 taskkill 兜底，最后才删目录——只 taskkill 时目录常常删不掉（实测残留 60 个）。
        if ws is not None:
            _browser_close(ws)
            ws.close()
        if proc is not None:
            _kill_tree(proc)
        if not _rmtree_retry(profile):
            print(f"# 残留：临时 profile 删不掉 {profile}", file=sys.stderr)


def _free_port() -> int:
    """拿一个当前空闲的本地端口（临时读口/假读口都绑这个）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _kill_tree(proc: subprocess.Popen) -> None:
    """无头浏览器会派生一堆子进程；只 terminate 父进程会留下占端口的幽灵。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    # taskkill 之后等一会儿：句柄释放是异步的，立刻删 profile 会删不掉。
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        time.sleep(0.5)


def _browser_close(ws: "_Ws") -> None:
    """先请浏览器**自己退**（CDP `Browser.close`），再 taskkill 兜底（P2-5）。

    为什么不能只 taskkill：它把进程砍了，但 profile 里的文件句柄由内核异步回收，
    紧接着 `rmtree` 常常失败（实测残留 60 个 `%TEMP%\fish-wb-*`）。
    `Browser.close` 会走正常退出路径、自己释放 profile。对端在回复前就关连接，
    所以这里把"连接断开"也当成功。
    """
    try:
        ws.call(9, "Browser.close")
    except (RuntimeError, OSError):
        pass


#: 删不掉的临时目录（老实记账：收尾一起报，不静默吞）
_RESIDUE: list[str] = []


def _rmtree_retry(path: str, attempts: int = 8, delay: float = 0.4) -> bool:
    """删临时 profile：刚退出的浏览器还会短暂持有文件句柄，秒删会失败，必须重试。"""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        time.sleep(delay)
    _RESIDUE.append(path)
    return False


# ── 环境残留自检（P2-5）──────────────────────────────────────────────────
def _residue_dirs() -> list[str]:
    """本工具链的临时 profile **目录**（`%TEMP%\\fish-wb-*`）——本脚本自己的必须为 0。

    只认目录：`%TEMP%` 里还有别的工具留下的 `fish-wb-*.log` 文件（不是 profile），
    把它们算进来会让残留读数虚高，也会让"删不掉"清单里出现一堆普通文件。
    """
    temp = os.environ.get("TEMP") or tempfile.gettempdir()
    if not os.path.isdir(temp):
        return []
    return sorted(name for name in os.listdir(temp)
                  if name.startswith("fish-wb-") and os.path.isdir(os.path.join(temp, name)))


def _msedge_count() -> int | None:
    """整机 `msedge.exe` 进程数（含用户自己开的浏览器）——**只作前后对比**的粗略读数。

    不用 WMI 取命令行：本机实测一次 `Get-CimInstance Win32_Process` 要 ~14 s，
    跑两遍就把验收拖慢半分钟。命令行的精确读数放在 `--sweep-residue` 里单独做。
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return sum(1 for line in (out.stdout or "").splitlines() if '"msedge.exe"' in line)


def _browsers_with_fishwb(details: bool = False):
    """命令行里带 `fish-wb` 的 Edge 进程（精确读数，慢：本机 ~14 s，只在自检/清理时跑）。

    `details=False` → `[pid, ...]`；`details=True` → `[(pid, 已运行秒数, 命令行), ...]`。
    为什么要带"已运行秒数"：判陈旧只能看**进程启动时间**——上一轮遗留的浏览器会一直写
    profile，目录 mtime 永远"新鲜"，按目录判会把真正的残留一直留住（实测 11 个老浏览器
    被这么留住过）。我们的探针寿命是秒级，所以"起于 30 分钟前"一定是残留。
    """
    if os.name != "nt":
        return None
    script = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              "Where-Object { $_.CommandLine -like '*fish-wb*' } | "
              "ForEach-Object { \"$($_.ProcessId)|$([int]((Get-Date) - $_.CreationDate).TotalSeconds)|$($_.CommandLine)\" }")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                              "-EncodedCommand", encoded],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    found = []
    for line in (out.stdout or "").splitlines():
        pid_text, _, rest = line.partition("|")
        age_text, _, cmdline = rest.partition("|")
        if not pid_text.strip().isdigit():
            continue
        age = int(age_text) if age_text.strip().isdigit() else -1
        found.append((int(pid_text), age, cmdline))
    return found if details else [pid for pid, _, _ in found]


def _residue_report() -> dict:
    """残留读数：临时目录 + msedge 总数（快）；带 `fish-wb` 的无头浏览器（慢，可选）。"""
    return {"dirs": _residue_dirs(), "msedge": _msedge_count()}


def _print_residue(tag: str, report: dict) -> None:
    dirs = report["dirs"]
    print(f"# 残留自检[{tag}] %TEMP%\\fish-wb-* = {len(dirs)} 个 · "
          f"msedge.exe = {report['msedge'] if report['msedge'] is not None else '判不到'}"
          + (f" · 删不掉的：{_RESIDUE}" if _RESIDUE else ""))


def _sweep_residue(older_than_sec: int = 1800) -> dict:
    """清掉上一轮跑剩的东西（P2-5 的收尾）：无头浏览器进程 + 陈旧的临时 profile。

    安全性：只清**命令行里带 `fish-wb`** 的 Edge，而且只清**启动于 `older_than_sec` 秒前**
    的那些——正在跑的那一轮（含别人的）不动。要不要杀只看进程启动时间，不看 profile 目录
    的 mtime（老浏览器会一直写目录，mtime 永远新鲜，按它判会把残留全留住）。
    用户的浏览器 profile 里不含 `fish-wb`，不会命中。
    """
    found = _browsers_with_fishwb(details=True)
    if found is None:
        return {"killed": [], "keptFresh": [], "dirsRemoved": [],
                "note": "取不到进程命令行（非 Windows 或 PowerShell 不可用）"}
    killed, kept = [], []
    for pid, age, _cmdline in found:
        if 0 <= age < older_than_sec:
            kept.append(pid)
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        killed.append(pid)
    time.sleep(0.8)
    temp = os.environ.get("TEMP") or tempfile.gettempdir()
    removed = []
    for name in _residue_dirs():
        path = os.path.join(temp, name)
        try:
            if (time.time() - os.path.getmtime(path)) < older_than_sec:
                continue
        except OSError:
            continue
        if _rmtree_retry(path, attempts=3, delay=0.2):
            removed.append(name)
    return {"killed": killed, "keptFresh": kept, "dirsRemoved": removed}


def visible_numbers(text: str) -> list[str]:
    """把「命令里的数字」排除后的数字——A-2 的机检口径（命令里的 `p0` 不算读数）。"""
    without_command = text.replace(CODES["unreachable"], "")
    return re.findall(r"\d", without_command)


# ── 上下文 ───────────────────────────────────────────────────────────────
def build_ctx(args) -> dict:
    import fish_workbench_api as A

    A._CURRENT_DB = args.db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    conn = A.open_conn(args.db)
    codes = json.load(open(CODES_PATH, encoding="utf-8"))
    return {"A": A, "conn": conn, "now": now, "since": since, "args": args,
            "codes": codes, "api": args.api.rstrip("/"), "static": (args.static or "").rstrip("/"),
            "db": args.db}


# ── 通用断言（返回 (ok, detail)；变异运行时同一个函数吃坏输入）───────────────
def _stream(ctx, **kwargs) -> dict:
    A = ctx["A"]
    params = {"scope_kind": "all", "project": None, "board_id": None, "win": "24h",
              "since": ctx["since"], "limit": 40, "num_cursor": None, "direction": "older"}
    params.update(kwargs)
    return A.collect_stream(ctx["conn"], **params)


def walk_pages(ctx, limit: int = 40, max_pages: int = 200) -> dict:
    """翻到底，返回覆盖读数（与 verify 的 R1 同口径）。"""
    A = ctx["A"]
    out = _stream(ctx, limit=limit)
    return _walk_from(ctx, out, limit, max_pages)


def _walk_from(ctx, out: dict, limit: int, max_pages: int) -> dict:
    A = ctx["A"]
    counts_sql, params = A._count_sql("all", None, None, ctx["since"])
    total = sum(int(r[0]) for r in ctx["conn"].execute(counts_sql, params).fetchall())
    items = out["data"]["items"]
    cursor = out["data"]["cursor"]
    keys: set[str] = set()
    raw = dup = pages = 0
    monotonic = True
    while True:
        pages += 1
        for item in items:
            if item["key"] in keys:
                dup += 1
            keys.add(item["key"])
            raw += item["fold"]["count"] if item.get("fold") else 1
        if not items or not cursor or pages >= max_pages:
            break
        nxt = _stream(ctx, limit=limit, num_cursor=cursor)
        if nxt["data"]["cursor"] and not (nxt["data"]["cursor"] < cursor):
            monotonic = False
        items, cursor = nxt["data"]["items"], nxt["data"]["cursor"]
    return {"pages": pages, "rawSum": raw, "total": total, "dup": dup, "monotonic": monotonic}


# ── A-1 ─────────────────────────────────────────────────────────────────
def a01(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    sql, params = A._count_sql("all", None, None, ctx["since"])
    rows = ctx["conn"].execute(sql, params).fetchall()
    counts = dict(zip(("tool", "post", "push", "event"), (int(r[0]) for r in rows)))
    first = _stream(ctx)["data"]["items"]
    first_cls = {item["cls"] for item in first}
    memory = ctx["conn"].execute(
        "SELECT COUNT(*) FROM events WHERE ts>=? AND type IN ('memory.recorded',"
        "'memory.accessed','memory.retired')", (ctx["since"],)).fetchone()[0]
    system = ctx["conn"].execute(
        "SELECT COUNT(*) FROM events WHERE ts>=? AND type NOT IN ('memory.recorded',"
        "'memory.accessed','memory.retired')", (ctx["since"],)).fetchone()[0]
    ok = (all(v > 0 for v in counts.values()) and memory > 0 and system > 0
          and len(first_cls) >= 3)
    detail = json.dumps({"counts": counts, "memoryEvents": int(memory),
                         "systemEvents": int(system), "firstPageCls": sorted(first_cls)},
                        ensure_ascii=False)
    return ok, detail


def a01_mut(ctx) -> tuple[bool, str]:
    """变异：把 board_posts 从合流里摘掉（评审原文的做法）→ 五源断言必须红。"""
    A = ctx["A"]
    original = A._count_sql

    def broken(*args, **kwargs):
        sql, params = original(*args, **kwargs)
        parts = sql.split(" UNION ALL ")
        # 计数串按 tool/post/push/event 四段排；第 2 段就是 board_posts，换成恒 0。
        # `scope=all` 时每段正好一个 `since` 参数，所以同步删掉第 2 个参数（否则绑定数不匹配）。
        if len(parts) >= 2 and len(params) >= 4:
            parts[1] = "SELECT 0"
            params = [p for index, p in enumerate(params) if index != 1]
        return " UNION ALL ".join(parts), params

    A._count_sql = broken
    try:
        ok, detail = a01(ctx)
    finally:
        A._count_sql = original
    return ok, detail


# ── A-2 ─────────────────────────────────────────────────────────────────
def a02(ctx) -> tuple[bool, str]:
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（需要一个静态托管地址，如 http://127.0.0.1:8799）")
    probe = dom_probe(f"{static}/?api=http://127.0.0.1:9", wait_ms=5000)
    text = probe["text"]
    has_banner = CODES["unreachable"][:6] in text
    numbers = visible_numbers(text)
    ok = has_banner and not numbers and "重试" in text
    return ok, json.dumps({"hasBanner": has_banner, "visibleNumbers": numbers,
                           "chars": len(text)}, ensure_ascii=False)


def a02_mut(ctx) -> tuple[bool, str]:
    """变异：给整屏文案里塞一个数字（评审原文的"标题里塞 1,234"）→ 必须红。"""
    text = CODES["unreachable"] + " 今天有 1,234 条行为"
    numbers = visible_numbers(text)
    # 返回的是**同一条断言**（"可见文本里没有数字"）在坏输入下的结果：必须为 False。
    return (not numbers), json.dumps({"visibleNumbers": numbers,
                                      "note": "变异方向：数字必须被检出 → 断言必须红"},
                                     ensure_ascii=False)


# ── A-3 ─────────────────────────────────────────────────────────────────
def _sk_strictly_descending(items: list[dict]) -> bool:
    keys = [item["sk"] for item in items if item.get("sk")]
    return all(a > b for a, b in zip(keys, keys[1:]))


def a03(ctx) -> tuple[bool, str]:
    runs = []
    for _ in range(3):
        out = _stream(ctx, limit=40)
        runs.append([item["key"] for item in out["data"]["items"]])
    same = runs[0] == runs[1] == runs[2]
    order_ok = _sk_strictly_descending(_stream(ctx, limit=40)["data"]["items"])
    ok = same and order_ok and len(runs[0]) > 0
    detail = json.dumps({"returned": len(runs[0]), "identical3Runs": same,
                         "skStrictlyDescending": order_ok}, ensure_ascii=False)
    return ok, detail


class _MutationSkip(RuntimeError):
    """变异**没有作用对象**——不是判据失败，也不是通过：必须显式 SKIP 并给原因。

    当前没有哪条变异会抛它了（A-3 改成自带固定 fixture 后，作用对象不再依赖数据运气；
    其余变异本来就是构造坏输入）。留着这个类别是因为"没自证"与"自证失败"必须能分开记：
    以后加变异只要抛它，main 就会记 `MUTATION-SKIP`（不计通过、也不计判别力失败）。
    """


def _same_second_groups(items: list[dict]) -> int:
    """页内「同秒并列」的组数（>0 才说明"打散同秒"有作用对象）。"""
    counts: dict[str, int] = {}
    for item in items:
        key = item.get("ts") or ""
        if key:
            counts[key] = counts.get(key, 0) + 1
    return sum(1 for value in counts.values() if value > 1)


def _rotate_same_second(rows: list, rotation: int) -> list:
    """把**同一秒的并列行**在排序键上打散，返回排序的输入行（KI-1 的修法）。

    排序键是 `sk = "<ts>|<9-rk>|<pk 12 位>"`，`collect_stream` 在 `_fetch_batch` **之后**
    `accumulated.sort(key=lambda row: row["sk"], reverse=True)`。所以"打散"必须改
    **排序的输入行**；改投影层（`_stub_item` 的 sk）永远改不了顺序——那正是 A-3 变异
    恒绿的根因（排序在投影之前）。

    具体做法：同一 `ts` 的组内把**第三段（pk，唯一决定并列顺序的分量）**按 `rotation`
    轮转，于是"同秒谁先谁后"随运行变化。用轮转而不是 `random`：第 1/2/3 次运行给出
    确定性不同的三个顺序，判据**稳定必红**；纯随机在 4 个并列组时约有 1/192 的概率
    三次撞成同一顺序而假绿。
    """
    out = [dict(row) for row in rows]
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(out):
        key = row.get("ts") or ""
        if key:
            groups.setdefault(key, []).append(index)
    for _key, indexes in groups.items():
        if len(indexes) < 2:
            continue
        pks = [int(out[index]["pk"]) for index in indexes]
        shift = rotation % len(pks)
        rotated = pks[shift:] + pks[:shift]
        for position, (index, fake) in enumerate(zip(indexes, rotated)):
            row = out[index]
            # `*1000 + position` 让组内次序唯一（跨源 pk 可能相等），排序才可比较。
            row["sk"] = f"{row['ts']}|{9 - int(row['rk'])}|{fake * 1000 + position:012d}"
    return out


def a03_mut(ctx) -> tuple[bool, str]:
    """变异：在**排序输入**上打散同秒并列 → 3 次顺序必须不再一致。

    旧变异打在投影层（`_stub_item` 的 `sk`）：排序发生在 `accumulated` 上，而投影在
    排序**之后**才跑，所以改投影永远不会改变顺序 → 变异恒绿、判据没有判别力
    （QA/评审在 docs/104 KI-1 的定位）。后来把打散注入 `_fetch_batch` 的返回行，
    让 `collect_stream` 自己的 `sort(key=sk)` 去吃这份被打散的行。

    **P2-3**：作用对象不再依赖真实数据的运气（106 收尾那次首屏没有同秒并列 →
    `MUTATION-SKIP`）。这里自带一个固定 fixture（`_make_tie_fixture`：2 行同 ts、同源
    → 同 rk → 顺序只由 pk 决定），打散一定有用武之地，变异**无条件可红**。
    """
    A = ctx["A"]
    tmpdir = tempfile.mkdtemp(prefix="fish-wb-tie-")
    conn = None
    try:
        fixture = os.path.join(tmpdir, "tie.sqlite")
        info = _make_tie_fixture(ctx["db"], fixture, ctx["since"])
        conn = A.open_conn(fixture)
        # fixture 是"另一个库"：ctx 的 conn 还指着真库，所以给 `_stream` 一份新上下文。
        fctx = {"A": A, "conn": conn, "since": ctx["since"]}
        original = A._fetch_batch
        state = {"rotation": 1}

        def scrambled(inner_conn, **kwargs):
            return _rotate_same_second(original(inner_conn, **kwargs), state["rotation"])

        A._fetch_batch = scrambled
        try:
            runs = []
            ties = 0
            for index in range(3):
                state["rotation"] = index + 1
                items = _stream(fctx, limit=40)["data"]["items"]
                runs.append([item["key"] for item in items])
                ties = max(ties, _same_second_groups(items))
        finally:
            A._fetch_batch = original
    finally:
        if conn is not None:
            conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
    if ties == 0:
        # 固定 fixture 都造不出同秒并列，说明 fixture 本身坏了——这是**变异跑不通**，
        # 不是"数据没并列所以跳过"，所以抛 RuntimeError（main 记 MUTATION-SKIP 且计入失败面）。
        raise RuntimeError("固定 fixture 里没有同秒并列（fixture 构造失效，不是数据运气问题）")
    ok = runs[0] == runs[1] == runs[2]
    return ok, json.dumps({"identicalUnderMutation": ok,
                           "sameSecondGroups": ties,
                           "fixture": info,
                           "first6PerRun": [run[:6] for run in runs],
                           "note": "变异方向：固定 fixture 的同秒并列被打散 → 3 次顺序必须不同"},
                          ensure_ascii=False)


# ── A-4 ─────────────────────────────────────────────────────────────────
def a04(ctx) -> tuple[bool, str]:
    result = walk_pages(ctx, limit=40)
    ok = (result["dup"] == 0 and result["monotonic"]
          and result["rawSum"] == result["total"])
    return ok, json.dumps(result, ensure_ascii=False)


def a04_mut(ctx) -> tuple[bool, str]:
    """变异：把游标放回并集外（FISH_WB_CURSOR_PUSHDOWN=0）→ 覆盖必须明显不足。"""
    out = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child", "walk",
         "--db", ctx["db"]],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
        env={**os.environ, "FISH_WB_CURSOR_PUSHDOWN": "0"})
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"变异子进程没有输出：{out.stderr[-400:]}")
    result = json.loads(lines[-1])
    ok = (result["dup"] == 0 and result["rawSum"] == result["total"])
    return ok, json.dumps(result, ensure_ascii=False)


# ── A-5 ─────────────────────────────────────────────────────────────────
def _motion(ctx, window_min: int | None) -> dict:
    A = ctx["A"]
    original = A.MOTION_WINDOW_MIN
    if window_min is not None:
        A.MOTION_WINDOW_MIN = window_min
    try:
        return A.collect_stats(ctx["conn"], days=1, top=5)["data"]["motion"]
    finally:
        A.MOTION_WINDOW_MIN = original


def a05(ctx) -> tuple[bool, str]:
    base = _motion(ctx, 30)
    wide = _motion(ctx, 1440)
    ok = (base["quantifier"] == "会话" and base["windowMinutes"] == 30
          and base["inMotion"] != wide["inMotion"])
    return ok, json.dumps({"inMotion30": base["inMotion"], "inMotion1440": wide["inMotion"],
                           "quantifier": base["quantifier"],
                           "windowMinutes": base["windowMinutes"]}, ensure_ascii=False)


def a05_mut(ctx) -> tuple[bool, str]:
    """变异：把「在跑」写死成 1（评审原文"写死 1 → 红"）→ 两窗口必须不再有差。"""
    A = ctx["A"]
    original = A.collect_stats

    def pinned(*args, **kwargs):
        result = original(*args, **kwargs)
        result["data"]["motion"]["inMotion"] = 1
        return result

    A.collect_stats = pinned
    try:
        base, wide = _motion(ctx, 30), _motion(ctx, 1440)
    finally:
        A.collect_stats = original
    ok = base["inMotion"] != wide["inMotion"]
    return ok, json.dumps({"inMotion30": base["inMotion"], "inMotion1440": wide["inMotion"]},
                          ensure_ascii=False)


# ── A-6 ─────────────────────────────────────────────────────────────────
READ_WORDS = ("读了", "读取", "阅读", "打开文件", "查看了文件")


def _assert_no_read_verbs(items: list[dict]) -> list[str]:
    hits = []
    for item in items:
        text = item.get("text") or ""
        for word in READ_WORDS:
            if word in text:
                hits.append(f"{item.get('key')}:{word}")
        if item.get("src") == "read":
            hits.append(f"{item.get('key')}:src=read")
    return hits


def a06(ctx) -> tuple[bool, str]:
    items: list[dict] = []
    out = _stream(ctx, limit=40)
    pages = 1
    while pages <= 8:
        items.extend(out["data"]["items"])
        cursor = out["data"]["cursor"]
        if not cursor or not out["data"]["items"]:
            break
        out = _stream(ctx, limit=40, num_cursor=cursor)
        pages += 1
    hits = _assert_no_read_verbs(items)
    ok = not hits and len(items) > 0
    return ok, json.dumps({"scanned": len(items), "hits": hits[:5]}, ensure_ascii=False)


def a06_mut(ctx) -> tuple[bool, str]:
    """变异：给一条工具动作编上"读取"（评审原文"给 bash 条目加读取 → 红"）。"""
    fake = [{"key": "tool:mock", "src": "tool", "text": "某人 读取了配置文件 .env"},
            {"key": "tool:mock2", "src": "tool", "text": "某人 执行了命令"}]
    hits = _assert_no_read_verbs(fake)
    # 同一条断言在坏输入下的结果：必须为 False（有编出来的"读"就红）。
    return (not hits), json.dumps({"hits": hits}, ensure_ascii=False)


# ── A-7 ─────────────────────────────────────────────────────────────────
#: 页签二的字段组（96 §八 的八类字段，机器可检的是这些**标签**是否出现）。
LEDGER_LABELS = ["这一条", "这个人", "他的账本", "类型", "操作面 / 目标", "结果", "时间",
                 "会话", "宿主", "在不在跑", "正在干什么", "声明面", "实际面"]


def _ledger_labels_present(text: str) -> list[str]:
    return [label for label in LEDGER_LABELS if label in text]


def a07(ctx) -> tuple[bool, str]:
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（A-7 要在浏览器里看选中态的右栏）")
    # 取一个**能进详情**的 key：`fold:*` 是折叠条的显示键，`/api/entry` 只解析
    # tool|post|push|event:<id>（折叠条本身点开会展开、不会拿组键去请求详情）。
    page = http_json(f"{ctx['api']}/api/stream?win=24h&limit=20")
    candidates = [item["key"] for item in page["data"]["items"] if not item.get("fold")]
    if not candidates:
        raise RuntimeError("首屏 20 条全是折叠条，取不到可进详情的 key")
    key = candidates[0]
    url = f"{static}/?api={ctx['api']}#/w?win=24h&sel={key}&tab=ledger"
    probe = dom_probe(url, wait_ms=7000)
    present = _ledger_labels_present(probe["text"])
    ok = len(present) >= 10
    return ok, json.dumps({"key": key, "labelsPresent": len(present),
                           "missing": [x for x in LEDGER_LABELS if x not in present]},
                          ensure_ascii=False)


def a07_mut(ctx) -> tuple[bool, str]:
    """变异：指向一个不存在的 key（等于"没选中"）→ 字段组必须不出现。"""
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static")
    url = f"{static}/?api={ctx['api']}#/w?win=24h&sel=tool:99999999&tab=ledger"
    probe = dom_probe(url, wait_ms=7000)
    present = _ledger_labels_present(probe["text"])
    ok = len(present) >= 10
    return ok, json.dumps({"labelsPresentUnderMutation": len(present)}, ensure_ascii=False)


# ── A-8 ─────────────────────────────────────────────────────────────────
def a08(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
    items = out["data"]["items"]
    total = out["data"]["counts"]["total"]
    wrong = [item["key"] for item in items if item["project"] != PROJECT]
    posts = [item for item in items if item["src"] == "post"]
    posts_ok = bool(posts) and all(item["project"] == PROJECT for item in posts)
    # R-04 的第二半边：facets 的 CZHY 计数必须**包含**这些登记，且与流水"本刀 N 条"一致
    # （四处用同一个表达式，才不会出现"筛得到行、左栏数字却少一块"）。
    facets = A.collect_facets(ctx["conn"], win="24h", since=ctx["since"])
    czhy = next((p for p in facets["data"]["projects"] if p["name"] == PROJECT), None)
    facet_ok = (czhy is not None and czhy["count"] == total
                and czhy["bySource"].get("post", 0) > 0)
    ok = bool(items) and not wrong and posts_ok and facet_ok
    return ok, json.dumps({"returned": len(items), "posts": len(posts), "total": total,
                           "wrongProject": wrong[:5],
                           "facetCount": None if czhy is None else czhy["count"],
                           "facetBySource": None if czhy is None else czhy["bySource"],
                           "facetEqualsStreamTotal": facet_ok}, ensure_ascii=False)


def a08_mut(ctx) -> tuple[bool, str]:
    """变异：把看板登记的项目归属**改回 null**（评审 R-04 原文的现场）→ 必须红。

    为什么把变异放在**显示列**而不是把 SQL 整段改回 NULL：评审实测的症状是
    "`scope=project:CZHY` 的 30 行里 25 行 `project=None`"——**行还在、项目是空**。
    如果连作用域谓词一起改回 NULL，行会直接筛不出来，那就变成另一个 bug（"筛不到"），
    测不出原症状。这里只把 post 条目的项目字段抹成 None，作用域与计数都不动，
    正好复现"筛得到、显示不出来"。
    """
    A = ctx["A"]
    original = A.make_item

    def blanked(row, labels, board_names, *args, **kwargs):
        # W5 起 `make_item` 多了 lineage 参数（第 4 位）；变异器必须**透传**，
        # 否则 A-8 变异跑不起来 → MUTATION-SKIP（判据没被自证，不能算通过）。
        item = original(row, labels, board_names, *args, **kwargs)
        if item.get("src") == "post":
            item["project"] = None
        return item

    A.make_item = blanked
    try:
        ok, mutated_detail = a08(ctx)
        out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
        posts = [item for item in out["data"]["items"] if item["src"] == "post"]
        blank = [item["key"] for item in posts if item["project"] is None]
    finally:
        A.make_item = original
    return ok, json.dumps({"note": "看板登记的 project 改回 null（行仍在、项目为空）",
                           "postsReturned": len(posts),
                           "postsWithNullProject": len(blank),
                           "mutatedCheck": json.loads(mutated_detail)},
                          ensure_ascii=False)


# ── A-9 ─────────────────────────────────────────────────────────────────
def a09(ctx) -> tuple[bool, str]:
    out = _stream(ctx, scope_kind="project", project=PROJECT, limit=100)
    folds = [item for item in out["data"]["items"] if item.get("fold")]
    if not folds:
        out = _stream(ctx, limit=100)
        folds = [item for item in out["data"]["items"] if item.get("fold")]
    ok = bool(folds) and all(
        item["fold"]["count"] >= 3 and len(item["fold"]["items"]) >= 3
        and all(sub.get("key") for sub in item["fold"]["items"]) for item in folds)
    detail = json.dumps({"groups": len(folds),
                         "counts": [item["fold"]["count"] for item in folds[:5]],
                         "detailLengths": [len(item["fold"]["items"]) for item in folds[:5]],
                         "truncated": [bool(item["fold"].get("truncated")) for item in folds[:5]]},
                        ensure_ascii=False)
    return ok, detail


def a09_mut(ctx) -> tuple[bool, str]:
    """变异：`FISH_WB_FOLD_MIN=9999`（折叠关掉）→ 必须一条折叠组都没有。"""
    out = subprocess.run(
        [sys.executable, "-X", "utf8", os.path.abspath(__file__), "--child", "folds",
         "--db", ctx["db"]],
        capture_output=True, text=True, encoding="utf-8", timeout=600,
        env={**os.environ, "FISH_WB_FOLD_MIN": "9999"})
    lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"变异子进程没有输出：{out.stderr[-400:]}")
    result = json.loads(lines[-1])
    return bool(result["groups"]), json.dumps(result, ensure_ascii=False)


# ── A-10 ────────────────────────────────────────────────────────────────
def _four_distinct(codes: dict) -> list[str]:
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    return [f"{i}" for i, sentence in enumerate(sentences) if not sentence.strip()]


def a10(ctx) -> tuple[bool, str]:
    codes = ctx["codes"]
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    unique = len(set(sentences)) == 4
    bundle = ""
    for name in os.listdir(os.path.join(DIST_DIR, "assets")) if os.path.isdir(
            os.path.join(DIST_DIR, "assets")) else []:
        if name.endswith(".js"):
            bundle += open(os.path.join(DIST_DIR, "assets", name), encoding="utf-8").read()
    in_bundle = [key for key in ("emptyDb", "filteredEmpty", "windowEmpty")
                 if codes[key] in bundle]
    in_page = False
    if ctx["static"]:
        # 静态托管不可达 / 读到的不是应用页 → 让它抛（主流程记 SKIP）。
        # 不能像旧代码那样 `except RuntimeError: in_page = False`——那会把"没验"变成"不合格"，
        # 正是 P1-4 要止住的那类假红。
        probe = dom_probe(f"{ctx['static']}/?api=http://127.0.0.1:9", wait_ms=5000)
        in_page = codes["unreachable"][:6] in probe["text"]
    ok = unique and not _four_distinct(codes) and len(in_bundle) == 3 and in_page
    return ok, json.dumps({"fourDistinct": unique, "inBundle": in_bundle,
                           "unreachableInPage": in_page}, ensure_ascii=False)


def a10_mut(ctx) -> tuple[bool, str]:
    """变异：把两句写成同一句 → "四句不同"必须红。"""
    codes = dict(ctx["codes"])
    codes["windowEmpty"] = codes["filteredEmpty"]
    sentences = [codes["emptyDb"], codes["filteredEmpty"], codes["windowEmpty"],
                 codes["unreachable"]]
    return len(set(sentences)) == 4, json.dumps(
        {"uniqueUnderMutation": len(set(sentences))}, ensure_ascii=False)


# ── A-11 ────────────────────────────────────────────────────────────────
#: 虚拟滚动的机检口径（一处定义：正常态与变异吃**同一份**判据，改口径只改这里）。
A11_ROWS_MAX = 200       # DOM 行节点上限（96 §十）
A11_LOAD_MIN = 200       # 至少要载入这么多条，"虚拟滚动"才有意义
A11_SCROLL_ROUNDS = 5    # 每轮 = 无限加载 40 条 → 初始 40 + 5×40 = 240 条
A11_WARM_MAX_MS = 300.0  # HTTP 热态单请求上限（**能失败**）
A11_INPROC_MAX_MS = 1000.0


def _a11_rows_verdict(rows, count) -> dict:
    """A-11 的 DOM 半边：**唯一**的行数判据（正常态与变异都吃这一份）。

    三句话缺一不可：
    - `0 < rows`：零行不是"虚拟滚动生效"，是页面坏了（P1-2 补的下界——旧判据
      `rows is not None and rows < 200` 让空白页/错误页都能 PASS）；
    - `rows < 200`：96 §十 的硬上限；
    - `count >= 200` 且 `rows < count`：DOM 里的行**严格少于**列表持有的条数，且列表
      确实载了足够多条——这才叫"只渲染一部分"。少了这半句，列表只载 40 条时
      `< 200` 恒成立，判据对"关掉虚拟滚动"毫无判别力（40 行也 < 200）。
    """
    reasons: list[str] = []
    if rows is None:
        reasons.append("DOM 行数取不到")
    else:
        try:
            rows = int(rows)
        except (TypeError, ValueError):
            reasons.append(f"DOM 行数不是数字：{rows!r}")
            rows = None
        else:
            if rows <= 0:
                reasons.append("DOM 行数是 0（空白页不能算虚拟滚动通过）")
            if rows >= A11_ROWS_MAX:
                reasons.append(f"DOM 行 {rows} ≥ {A11_ROWS_MAX}（没在虚拟化）")
    if count is None:
        reasons.append("列表持有条数取不到（页面上没有 [role=list] 的 data-count）")
    else:
        try:
            count = int(count)
        except (TypeError, ValueError):
            reasons.append(f"data-count 不是数字：{count!r}")
            count = None
        else:
            if count < A11_LOAD_MIN:
                reasons.append(f"列表只持有 {count} 条 < {A11_LOAD_MIN}（载入不够，判据不成立）")
            if rows is not None and rows >= count:
                reasons.append(f"DOM 行 {rows} ≥ 列表持有 {count}（全量渲染，虚拟滚动没起作用）")
    return {"ok": not reasons, "reason": "；".join(reasons) or None, "rows": rows, "count": count}


def _http_timed(url: str, timeout: float = 180.0) -> dict:
    """打一次 HTTP 并记「请求时刻 + 耗时」——A-11 的 P1-3 半边（HTTP 路径的真实读数）。"""
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = time.perf_counter()
    error = None
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {"at": at, "ms": round((time.perf_counter() - started) * 1000, 1), "error": error}


def _http_warm(api: str, samples: int = 6) -> dict:
    """HTTP **热态**：真服务上连打 N 次，丢掉第一次（可能撞上预热线程/缓存冷），取中位。

    为什么这条必须有（P1-3）：A-11 原来只报"进程内函数中位"（32.8 / 96.5 ms），
    那不是 UI 走的路。QA 在 HTTP 上量到的冷态是 14.06 / 9.90 / 4.01 / 6.74 / 0.96 s，
    热态 0.10 s——两档差两个数量级，混成一句"首屏 < 1s"就是失真。
    """
    runs = [_http_timed(f"{api}/api/stream?win=24h&limit=40") for _ in range(samples)]
    body = runs[1:] or runs
    times = [r["ms"] for r in body if not r["error"]]
    return {"samplesMs": [r["ms"] for r in runs],
            "medianMs": round(statistics.median(times), 1) if times else None,
            "errors": [r["error"] for r in runs if r["error"]],
            "firstAt": runs[0]["at"], "lastAt": runs[-1]["at"]}


def _http_cold(db_path: str) -> dict:
    """HTTP **冷态**：新起一个读口，记三个时刻（进程拉起 → 端口可连 → 第一次请求完成）。

    "冷"= **这个进程**的第一次 HTTP 请求。就绪探测用 socket 连端口、**不发请求**
    （用 `/api/health` 探测本身就是一次预热，会把冷态读数洗掉）。
    冷态**只记录、不设硬阈值**（启动成本是进程级，阈值会随机器漂）；能失败的半边放在热态。
    """
    port = _free_port()
    spawn_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", os.path.join(_P0, "fish_workbench_api.py"),
         "--db", db_path, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready_at = None
        for _ in range(240):     # 最长等 60 s
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready_at = time.perf_counter()
                    break
            except OSError:
                time.sleep(0.25)
        if ready_at is None:
            return {"ok": False, "error": "冷启动的读口 60 s 内没有监听端口", "spawnAt": spawn_at,
                    "spawnToListMs": None, "coldMs": None}
        listen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        first = _http_timed(f"http://127.0.0.1:{port}/api/stream?win=24h&limit=40")
        stats_first = _http_timed(f"http://127.0.0.1:{port}/api/stats?days=1")
        cold = first["ms"]
        if first["error"]:
            verdict = f"判不到：{first['error']}"
        elif cold < 1000:
            verdict = "可接受（<1 s）"
        elif cold < 3000:
            verdict = "偏慢（1–3 s，首屏能感知）"
        else:
            verdict = "很慢（≥3 s：冷启动必须常驻或预热）"
        return {"ok": first["error"] is None,
                "spawnAt": spawn_at, "listenAt": listen_at,
                "spawnToListMs": round((ready_at - started) * 1000, 1),
                "coldMs": cold, "coldRequestAt": first["at"],
                "statsColdMs": stats_first["ms"],
                "firstResponseAfterSpawnMs": round(((ready_at - started) * 1000) + cold, 1),
                "verdict": verdict}
    finally:
        _kill_tree(proc)


def a11(ctx) -> tuple[bool, str]:
    times = []
    for _ in range(5):
        started = time.perf_counter()
        _stream(ctx, limit=40)
        times.append((time.perf_counter() - started) * 1000)
    median = statistics.median(times)
    page = http_json(f"{ctx['api']}/api/stream?win=24h&limit=40")
    total = page["data"]["counts"]["total"] or 0
    warm = _http_warm(ctx["api"])
    cold = _http_cold(ctx["db"])
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（A-11 的 DOM 半边要数浏览器里真正的行节点）")
    # 先滚 5 轮把列表撑到 ~240 条，再数 DOM：只载 40 条时"<200"永远成立，测不出东西。
    probe = dom_probe(f"{static}/?api={ctx['api']}", wait_ms=7000,
                      scroll_rounds=A11_SCROLL_ROUNDS)
    verdict = _a11_rows_verdict(probe["rows"], probe["count"])
    ok = (median < A11_INPROC_MAX_MS
          and warm["medianMs"] is not None and warm["medianMs"] <= A11_WARM_MAX_MS
          and cold.get("ok") is True
          and total >= 200
          and verdict["ok"])
    detail = json.dumps({"inprocMedianMs": round(median, 1),
                         "inprocMin": round(min(times), 1),
                         "inprocMax": round(max(times), 1),
                         "httpWarm": warm, "httpCold": cold,
                         "total": total, "domRows": probe["rows"],
                         "listHeld": probe["count"],
                         "rowsVerdict": verdict,
                         "target": (f"服务端中位 <{A11_INPROC_MAX_MS:.0f}ms；HTTP 热态中位 "
                                    f"≤{A11_WARM_MAX_MS:.0f}ms；载入 ≥{A11_LOAD_MIN} 条后 "
                                    f"0 < DOM 行 < {A11_ROWS_MAX} 且少于持有条数；"
                                    "冷态只记录不设阈值（COLD）")},
                        ensure_ascii=False)
    return ok, detail


def a11_mut(ctx) -> tuple[bool, str]:
    """变异：**真的关掉虚拟滚动**（`?novirtual=1` → VirtualList 全量渲染）→ 行数判据必须红。

    P1-2 原文：旧变异 `fake_rows < 200` 是自造数字，根本没关虚拟滚动。现在走产品自己的
    开关（`ui/src/components/VirtualList.tsx` 读 `?novirtual=1` 时全量渲染），
    走的是同一段渲染代码、同一个 DOM 探针、同一份 `_a11_rows_verdict`。

    第二条反例：把页面指向一个死读口（整页 0 行）→ **下界**必须红
    （旧判据 `rows is not None and rows < 200` 会让 0 行 PASS）。
    """
    static = ctx["static"]
    if not static:
        raise RuntimeError("缺 --static（这条变异要在浏览器里关虚拟滚动）")
    novirtual = dom_probe(f"{static}/?api={ctx['api']}&novirtual=1", wait_ms=7000,
                          scroll_rounds=A11_SCROLL_ROUNDS)
    blank = dom_probe(f"{static}/?api=http://127.0.0.1:9", wait_ms=5000)
    got_novirtual = _a11_rows_verdict(novirtual["rows"], novirtual["count"])
    got_blank = _a11_rows_verdict(blank["rows"], blank["count"])
    # 变异自证的期望：两条坏输入都必须让判据**红**。
    # 返回的是"判据在坏输入下是否仍然绿"——绿了就是 MUTATION-FAIL。
    ok = got_novirtual["ok"] or got_blank["ok"]
    detail = json.dumps({
        "novirtual": {"rows": novirtual["rows"], "listHeld": novirtual["count"],
                      "verdict": got_novirtual},
        "blankPage": {"rows": blank["rows"], "listHeld": blank["count"],
                      "verdict": got_blank},
        "note": "变异方向：关掉虚拟滚动 / 零行页面 → 行数判据都必须红"},
        ensure_ascii=False)
    return ok, detail


# ── A-12 ────────────────────────────────────────────────────────────────
def a12(ctx) -> tuple[bool, str]:
    if not ctx["static"]:
        raise RuntimeError("缺 --static（A-12 要数页签一里的 ⓘ 入口）")
    probe = dom_probe(f"{ctx['static']}/?api={ctx['api']}", wait_ms=7000)
    origins = probe["origins"] or 0
    ok = origins >= 5
    return ok, json.dumps({"originEntrances": origins, "need": ">=5（页签一五块每块一个）",
                           "textLen": len(probe["text"])}, ensure_ascii=False)


def a12_mut(ctx) -> tuple[bool, str]:
    """变异：设 DOM 里一个 ⓘ 都没有（等于"删任一处"）→ 必须红。"""
    origins = 0
    return origins >= 5, json.dumps({"originEntrances": origins}, ensure_ascii=False)


# ── A-13 ────────────────────────────────────────────────────────────────
def a13(ctx) -> tuple[bool, str]:
    A = ctx["A"]
    norm = {"ZCode:abc": "zcode", "zcode:x": "zcode", "codebuddy:zz": "codebuddy",
            "dsh-desktop:q": "dsh-desktop"}
    norm_ok = all(A.host_from_session(key) == value for key, value in norm.items())
    row = ctx["conn"].execute(
        "SELECT id, session_key, host FROM tool_activity "
        "WHERE session_key <> '' AND host IS NOT NULL AND host <> '' "
        "AND lower(host) <> lower(substr(session_key, 1, instr(session_key, ':') - 1)) "
        "ORDER BY seen_at DESC LIMIT 1").fetchone()
    entry_ok = False
    detail = {}
    if row:
        entry = A.collect_entry(ctx["conn"], f"tool:{int(row['id'])}")
        if entry:
            api_host = entry["data"]["actor"]["host"]
            prefix = A.host_from_session(row["session_key"])
            entry_ok = api_host == prefix and api_host != (row["host"] or "").lower()
            detail = {"rowId": int(row["id"]), "dbHost": row["host"], "prefix": prefix,
                      "apiHost": api_host}
    ok = norm_ok and entry_ok
    detail["normalization"] = norm
    return ok, json.dumps(detail, ensure_ascii=False)


def a13_mut(ctx) -> tuple[bool, str]:
    """变异：宿主改回库内 host 列 → 必须红。"""
    A = ctx["A"]
    original = A.host_from_session
    A.host_from_session = lambda key: "workbuddy"  # 冒充"用库内 host 列"
    try:
        ok, _ = a13(ctx)
    finally:
        A.host_from_session = original
    return ok, json.dumps({"note": "宿主被写死成库内 host（workbuddy）"}, ensure_ascii=False)


# ── F-7 / F-8：状态构造与「partial 已删」的证据（--f78）────────────────────
def _copy_schema_only(source_db: str, target_db: str, drop: tuple[str, ...] = (),
                      with_views: bool = True) -> dict:
    """把真库的**表结构 + 视图**复制成一个空库（不带一行数据），可再删掉几张表来造「缺」。

    为什么要连 VIEW 一起复制（P1-1）：真库里 `citizens_all` 是 **VIEW**，而读口的
    `STATS_TABLES` 守卫用 `table_exists`（认 table+view）。只复制表的空库副本缺这个 view
    → `/api/stats` 回 `availability.state="missing"` → 前端 `isEmptyDb(facets, stats)`
    在 stats 为 null 时返回 false → 三处"库是空的"退化成"窗口内没行为"。
    也就是说：F-7「空态」那条判据当时是**被 fixture 判死的**，不是产品坏了。
    所以 fixture 必须与真库**同构**：表 + 视图一起复制。

    `with_views=False` 是**反向判据**的开关：造一个"缺视图"的副本，用来证明这条判据
    真的有判别力——不复制视图，空态文案必须消失（判据必须红）。

    跳过 FTS5 虚拟表：`CREATE VIRTUAL TABLE` 依赖 fts5 模块，复制过去既没必要也可能失败
    （读写口只用它在别的视图里，工作台四个端点都不碰）。

    返回 `{"tables": [...], "views": [...], "failed": [...]}`：跑坏了要看得见，不静默吞。
    """
    src = sqlite3.connect("file:" + source_db.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        rows = list(src.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table','view') AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, rowid"))
    finally:
        src.close()
    dst = sqlite3.connect(target_db)
    copied: dict = {"tables": [], "views": [], "failed": []}
    try:
        for kind, name, sql in rows:
            # `sqlite_%` 是引擎内部对象（sqlite_sequence / sqlite_stat1）：不许手工建，跳过。
            if name.startswith("sqlite_") or name in drop or "VIRTUAL" in sql.upper():
                continue
            if kind == "view" and not with_views:
                continue
            try:
                dst.execute(sql)
                copied["tables" if kind == "table" else "views"].append(name)
            except sqlite3.Error as exc:
                copied["failed"].append(f"{kind}:{name}:{exc}")
        dst.commit()
    finally:
        dst.close()
    return copied


#: P2-3：同秒并列的固定 fixture 条数（同源 → 同 rk → 顺序只由第三段 pk 决定）
TIE_FIXTURE_ROWS = 2


def _make_tie_fixture(source_db: str, target_db: str, since: str) -> dict:
    """造一个**必有同秒并列**的库：从真库抄 2 行 `tool_activity`，把 ts 改成同一个值。

    为什么必须造数据（P2-3）：`a03_mut` 的打散对象是"同一 `ts` 的多行"，而真实库里有没有
    同秒并列是**数据的运气**（106 收尾那次首屏就没有 → `MUTATION-SKIP`：没自证）。判据的
    判别力不该建立在运气上，所以变异自带一份固定输入。

    为什么选 tool 源：它的 `rk` 固定是 0——`sk = ts|9-rk|pk`，同 ts 且同 rk 时先后只能由
    第三段 pk 决定，轮转 pk 一定改变顺序；跨源（rk 不同）时 rk 先决定先后，打散就没有
    作用对象（这也是"同秒但不同源"仍然测不出东西的原因）。

    为什么用 `INSERT ... SELECT *` 而不是手写 INSERT：列清单跟着真库走，NOT NULL / 默认值
    / 新加列都不会因为 fixture 手抄而漂。
    """
    copied = _copy_schema_only(source_db, target_db)
    conn = sqlite3.connect(target_db)
    try:
        conn.execute("ATTACH DATABASE ? AS src", (source_db,))
        conn.execute(
            "INSERT INTO main.tool_activity SELECT * FROM src.tool_activity "
            "WHERE seen_at >= ? ORDER BY seen_at DESC LIMIT ?",
            (since, TIE_FIXTURE_ROWS))
        conn.execute(
            "UPDATE main.tool_activity SET seen_at = "
            "(SELECT MAX(seen_at) FROM main.tool_activity)")
        conn.commit()
        rows = conn.execute(
            "SELECT id, seen_at, session_key FROM tool_activity ORDER BY id").fetchall()
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT seen_at) FROM tool_activity").fetchone()[0]
    finally:
        conn.close()
    return {"copiedTables": len(copied["tables"]), "copiedViews": copied["views"],
            "failed": copied["failed"],
            "rows": [{"id": int(r[0]), "ts": r[1], "session": r[2]} for r in rows],
            "distinctTs": int(distinct)}


def _start_api(db_path: str, port: int) -> subprocess.Popen:
    """起一个**临时**读口（指向副本库）；起来后返回进程，由调用方负责杀。"""
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", os.path.join(_P0, "fish_workbench_api.py"),
         "--db", db_path, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            http_json(f"http://127.0.0.1:{port}/api/health", timeout=2)
            return proc
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    _kill_tree(proc)
    raise RuntimeError(f"临时读口没起来（db={db_path} port={port}）")


def _start_fake(mode: str) -> tuple[ThreadingHTTPServer, int]:
    """假读口（FakeProvider）：`error` = 所有请求回 400 错误信封；`slow` = 永远不返回。

    这两条**不是**产品能力，只为把 `Block` 的 `error` 与 `loading` 两个分支构造出来
    （QA F-7 要求"每个 state 至少被构造一次"）。它们跑在本进程的临时端口上，跑完关掉。
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if mode == "slow":
                time.sleep(45)   # 前端没有超时，请求挂着 → 整屏停在「正在读…」
                return
            body = json.dumps({
                "ok": False, "code": "fake_provider",
                "hint": "假读口：这条路径永远失败（只用于状态构造测试）",
                "action": "换成真读口：python p0/fish_workbench_api.py",
                "schema": "fish-workbench/1", "asOf": iso_now(),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            origin = self.headers.get("Origin")
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.end_headers()

        def log_message(self, *args):  # 静音
            return

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="fake-provider").start()
    return server, port


def _strip_ts_comments(source: str) -> str:
    """去掉 `//` 行注释与 `/* … */` 块注释——判据要判的是**代码**，不是注释里引用的示例。

    这条不是"放宽标准"：注释里出现 `availability === 'partial'` 这句话是在解释"它已被删"，
    真拿它当证据会得出反的结论（第一次跑就是这样误红了自己的说明文字）。
    """
    without_block = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


def _partial_leftovers(ctx) -> list[str]:
    """F-8：`partial` 必须是**整条删干净**，不是只在 UI 里藏起来。"""
    hits = []
    A = ctx["A"]
    if hasattr(A.Sources, "state"):
        hits.append("api:Sources.state() 仍在（会产出 partial）")
    types_src = _strip_ts_comments(
        open(os.path.join(UI_DIR, "src", "api", "types.ts"), encoding="utf-8").read())
    union = re.search(r"export type AvailState\s*=([\s\S]*?)\n\n", types_src)
    if union and "'partial'" in union.group(1):
        hits.append("ui:AvailState 仍声明 partial")
    boundary = _strip_ts_comments(
        open(os.path.join(UI_DIR, "src", "components", "AsyncBoundary.tsx"),
             encoding="utf-8").read())
    for line_no, line in enumerate(boundary.splitlines(), 1):
        if re.search(r"===\s*'partial'", line):
            hits.append(f"AsyncBoundary.tsx:{line_no} 仍在比较 partial")
    return hits


def _stats_state(api: str) -> dict:
    """读 `/api/stats` 的 availability 与累计条数——P1-1「空态为什么消失」的直接证据。"""
    try:
        payload = http_json(f"{api}/api/stats?days=1", timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    avail = payload.get("availability") or {}
    data = payload.get("data") or {}
    tools = (data.get("tools") or {}) if isinstance(data, dict) else {}
    return {"state": avail.get("state"), "detail": avail.get("detail"),
            "cumulative": tools.get("cumulative")}


def run_states(ctx, args) -> int:
    """F-7 的"每个 state 至少被构造一次" + F-8 的"partial 已删"证据。

    P1-1 的落点在这里：`empty` 那个 fixture 现在与真库**同构**（表 + 视图），
    并额外跑一条**反向判据**——复制表但不复制视图，空态文案必须消失
    （证明"复制视图"这件事本身有判别力，不是把 FAIL 改成 SKIP 蒙过去）。
    """
    static = ctx["static"]
    if not static:
        print("SKIP  F-7 状态构造  缺 --static（需要一个静态托管地址）")
        return 1
    if not os.path.isdir(DIST_DIR):
        print("SKIP  F-7 状态构造  缺 ui/dist（先 `cd ui; npm run build`）")
        return 1

    tmpdir = tempfile.mkdtemp(prefix="fish-wb-states-")
    procs: list[subprocess.Popen] = []
    servers: list[ThreadingHTTPServer] = []
    results: list[tuple[str, str, str]] = []
    mutation_ok = True
    try:
        empty_db = os.path.join(tmpdir, "empty.sqlite")
        copied = _copy_schema_only(ctx["db"], empty_db)
        missing_db = os.path.join(tmpdir, "missing.sqlite")
        _copy_schema_only(ctx["db"], missing_db, drop=("push_log",))
        # 反向判据的 fixture：同样的表，**故意不复制视图**。
        noview_db = os.path.join(tmpdir, "noview.sqlite")
        copied_noview = _copy_schema_only(ctx["db"], noview_db, with_views=False)
        print(f"# fixture 复制（P1-1）：同构副本 表 {len(copied['tables'])} / "
              f"视图 {len(copied['views'])} 个（{copied['views']}）"
              f" · 缺视图副本 表 {len(copied_noview['tables'])} / "
              f"视图 {len(copied_noview['views'])} 个"
              + (f" · 建库失败项 {copied['failed']}" if copied["failed"] else ""))
        empty_port = _free_port()
        procs.append(_start_api(empty_db, empty_port))
        missing_port = _free_port()
        procs.append(_start_api(missing_db, missing_port))
        noview_port = _free_port()
        procs.append(_start_api(noview_db, noview_port))
        error_server, error_port = _start_fake("error")
        servers.append(error_server)
        slow_server, slow_port = _start_fake("slow")
        servers.append(slow_server)

        cases = [
            ("ok", f"{static}/?api={ctx['api']}", 7000, "今日板"),
            ("empty", f"{static}/?api=http://127.0.0.1:{empty_port}", 7000, CODES["emptyDb"][:12]),
            ("missing", f"{static}/?api=http://127.0.0.1:{missing_port}", 7000, "该区数据未接"),
            ("error", f"{static}/?api=http://127.0.0.1:{error_port}", 7000, "这一块没读出来"),
            ("loading", f"{static}/?api=http://127.0.0.1:{slow_port}", 4000, CODES["loading"]),
        ]
        for name, url, wait_ms, marker in cases:
            try:
                probe = dom_probe(url, wait_ms=wait_ms)
            except _PageNotApp as exc:
                # P1-4：读到的不是应用页（浏览器错误页）→ SKIP，不冒充 FAIL。
                results.append((name, "SKIP", str(exc)))
                print(f"SKIP  F-7 状态 {name:<8} {exc}")
                continue
            except RuntimeError as exc:
                results.append((name, "FAIL", f"页面起不来：{exc}"))
                print(f"FAIL  F-7 状态 {name:<8} 页面起不来：{exc}")
                continue
            ok = marker in probe["text"]
            payload: dict = {"marker": marker, "chars": len(probe["text"]),
                             "domRows": probe["rows"]}
            if name == "empty":
                # 空态的根因证据：stats 必须是 ok/empty 且累计 0，不是 missing。
                payload["stats"] = _stats_state(f"http://127.0.0.1:{empty_port}")
            detail = json.dumps(payload, ensure_ascii=False)
            results.append((name, "PASS" if ok else "FAIL", detail))
            print(f"{'PASS' if ok else 'FAIL'}  F-7 状态 {name:<8} {detail}")

        # ── P1-1 反向判据：不复制 VIEW → 空态必须消失（新判据必须有判别力）──
        try:
            noview_probe = dom_probe(f"{static}/?api=http://127.0.0.1:{noview_port}",
                                     wait_ms=7000)
            noview_stats = _stats_state(f"http://127.0.0.1:{noview_port}")
            marker_in_noview = CODES["emptyDb"][:12] in noview_probe["text"]
            # 两件事都得成立：① stats 真的退化成 missing（fixture 确实缺了东西）；
            # ② 空态文案真的消失（判据在坏输入下会红）。
            degraded = noview_stats.get("state") == "missing"
            mutation_ok = degraded and not marker_in_noview
            verdict = "MUTATION OK" if mutation_ok else "MUTATION-FAIL"
            print(f"{verdict}  P1-1 反向判据（缺视图 → 空态必须红）  "
                  + json.dumps({"statsState": noview_stats.get("state"),
                                "statsDetail": noview_stats.get("detail"),
                                "emptyMarkerSeen": marker_in_noview,
                                "chars": len(noview_probe["text"])},
                               ensure_ascii=False))
        except (_PageNotApp, RuntimeError) as exc:
            mutation_ok = False
            print(f"MUTATION-FAIL  P1-1 反向判据跑不起来：{type(exc).__name__}: {exc}")

        leftovers = _partial_leftovers(ctx)
        ok = not leftovers
        results.append(("partial", "PASS" if ok else "FAIL",
                        json.dumps(leftovers, ensure_ascii=False)))
        print(f"{'PASS' if ok else 'FAIL'}  F-8 partial 已删   "
              f"{json.dumps({'leftovers': leftovers}, ensure_ascii=False)}")
    finally:
        for proc in procs:
            _kill_tree(proc)
        for server in servers:
            server.shutdown()
            server.server_close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for _, status, _ in results if status == "PASS")
    skipped = sum(1 for _, status, _ in results if status == "SKIP")
    failed = sum(1 for _, status, _ in results if status == "FAIL")
    print(f"\n# F-7/F-8 小结 {passed}/{len(results)} PASS · {failed} FAIL · {skipped} SKIP"
          f" · P1-1 反向变异 {'成立' if mutation_ok else '不成立'}")
    return 0 if (failed == 0 and skipped == 0 and mutation_ok) else 1


# ── 判据表 ───────────────────────────────────────────────────────────────
CHECKS = [
    ("A-1", a01, a01_mut),
    ("A-2", a02, a02_mut),
    ("A-3", a03, a03_mut),
    ("A-4", a04, a04_mut),
    ("A-5", a05, a05_mut),
    ("A-6", a06, a06_mut),
    ("A-7", a07, a07_mut),
    ("A-8", a08, a08_mut),
    ("A-9", a09, a09_mut),
    ("A-10", a10, a10_mut),
    ("A-11", a11, a11_mut),
    ("A-12", a12, a12_mut),
    ("A-13", a13, a13_mut),
]


def child(mode: str, db: str) -> int:
    """变异子进程：在**另一个进程**里跑同一段读数（环境变量在 import 时生效）。"""
    import fish_workbench_api as A

    A._CURRENT_DB = db
    now = A.now_dt()
    _, since = A.parse_window("24h", now)
    conn = A.open_conn(db)
    try:
        if mode == "walk":
            ctx = {"A": A, "conn": conn, "since": since}
            print(json.dumps(_walk_from(ctx, _stream(ctx, limit=40), 40, 200),
                             ensure_ascii=False))
            return 0
        if mode == "folds":
            out = A.collect_stream(conn, scope_kind="project", project=PROJECT,
                                   board_id=None, win="24h", since=since, limit=100,
                                   num_cursor=None, direction="older")
            folds = [item for item in out["data"]["items"] if item.get("fold")]
            print(json.dumps({"groups": len(folds)}, ensure_ascii=False))
            return 0
    finally:
        conn.close()
    raise SystemExit(f"unknown child mode: {mode}")


def _norm_id(raw: str) -> str:
    """`A-2` / `a02` / `A2` 视为同一条（命令行里两种写法都顺手）。"""
    digits = re.sub(r"\D", "", raw or "")
    return f"A{int(digits)}" if digits else (raw or "").strip().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="工作台 A-1~A-13 可执行验收（只读）")
    parser.add_argument("--api", default="http://127.0.0.1:8790")
    parser.add_argument("--static", default="http://127.0.0.1:8799")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--mutate", default="", help="故意让某条判据红（如 a08），用于自证")
    parser.add_argument("--only", default="", help="只跑某一条（如 a02）")
    parser.add_argument("--f78", action="store_true",
                        help="额外跑 F-7 状态构造（ok/empty/missing/error/loading）与 F-8 证据")
    parser.add_argument("--residue-browsers", action="store_true",
                        help="残留自检顺带数「命令行里带 fish-wb 的 Edge」（慢：本机 ~14s）")
    parser.add_argument("--sweep-residue", action="store_true",
                        help="开跑前清掉上一轮的残留（只杀带 fish-wb 的 Edge、只删 >30 分钟的临时 profile）")
    parser.add_argument("--child", default="", help="内部用：变异子进程")
    args = parser.parse_args()

    if args.child:
        return child(args.child, args.db)

    print(f"# 判定时刻 {iso_now()} · 库 {args.db} · API {args.api} · 静态 {args.static}")
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(args.db), timezone.utc)
        print(f"# 库 mtime {mtime.strftime('%Y-%m-%dT%H:%M:%SZ')} · "
              f"size {os.path.getsize(args.db)}")
    except OSError:
        pass
    dist_index = os.path.join(DIST_DIR, "index.html")
    if os.path.exists(dist_index):
        dist_mtime = datetime.fromtimestamp(os.path.getmtime(dist_index), timezone.utc)
        print(f"# dist/index.html mtime {dist_mtime.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    # ── P2-5：残留自检（跑之前先记一笔，跑完再记一笔；两次读数才看得出"有没有收拾干净"）
    if args.sweep_residue:
        swept = _sweep_residue()
        print(f"# 残留清理（P2-5）：杀掉陈旧的无头浏览器 {len(swept['killed'])} 个"
              f"（PID {swept['killed'][:10]}）· 留住仍在跑的 {len(swept.get('keptFresh') or [])} 个"
              f" · 删掉陈旧临时 profile {len(swept['dirsRemoved'])} 个"
              + (f" · {swept['note']}" if swept.get("note") else ""))
    before = _residue_report()
    _print_residue("跑前", before)
    if args.residue_browsers:
        pids = _browsers_with_fishwb()
        print(f"# 残留自检[跑前·精确] 命令行带 fish-wb 的 Edge = "
              f"{len(pids) if pids is not None else '判不到'}"
              + (f"（前 10 个 PID {pids[:10]}）" if pids else ""))

    ctx = build_ctx(args)
    if args.f78:
        try:
            code = run_states(ctx, args)
        finally:
            ctx["conn"].close()
        _print_residue("跑后", _residue_report())
        return code

    mutation_fail = 0
    mutation_skip = 0
    passed = skipped = failed = 0
    try:
        for cid, check, mutate in CHECKS:
            if args.only and _norm_id(args.only) != _norm_id(cid):
                continue
            if args.mutate and _norm_id(args.mutate) != _norm_id(cid):
                continue
            if args.mutate:
                # --mutate：直接把这条判据的**变异运行**结果当正式读数（应为红）
                try:
                    ok, detail = mutate(ctx)
                except _MutationSkip as exc:
                    record(cid, "SKIP", f"变异没有作用对象：{exc}")
                    skipped += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    record(cid, "SKIP", f"变异跑不起来：{type(exc).__name__}: {exc}")
                    skipped += 1
                    continue
                record(cid, "FAIL" if not ok else "PASS",
                       f"【故意破坏】{detail}")
                if not ok:
                    failed += 1
                else:
                    passed += 1
                continue
            try:
                ok, detail = check(ctx)
            except RuntimeError as exc:
                record(cid, "SKIP", f"{exc}")
                skipped += 1
                continue
            except Exception as exc:  # noqa: BLE001
                record(cid, "FAIL", f"判据自身抛错：{type(exc).__name__}: {exc}")
                failed += 1
                continue
            record(cid, "PASS" if ok else "FAIL", detail)
            passed += 1 if ok else 0
            failed += 0 if ok else 1
            # 变异自证：断言喂给坏输入必须红（红不了 = 这条判据没有判别力）
            if mutate is not None:
                try:
                    mutated_ok, mutated_detail = mutate(ctx)
                except _MutationSkip as exc:
                    # 变异没有作用对象（如数据里没有同秒并列）：显式 SKIP 并给原因。
                    # **不算通过**——「没自证」与「自证成立」是两件事，混起来就是假绿。
                    record(cid, "MUTATION-SKIP",
                           f"变异无作用对象，本次未自证（不是通过）：{exc}")
                    mutation_skip += 1
                    continue
                except Exception as exc:  # noqa: BLE001
                    record(cid, "MUTATION-SKIP", f"变异跑不起来：{type(exc).__name__}: {exc}")
                    mutation_fail += 1
                    continue
                if mutated_ok:
                    record(cid, "MUTATION-FAIL",
                           f"变异后仍然绿 → 判据没有判别力：{mutated_detail}")
                    mutation_fail += 1
                else:
                    print(f"      ↳ 变异自证 OK（坏输入下必红）：{mutated_detail}")
    finally:
        ctx["conn"].close()

    _print_residue("跑后", _residue_report())
    if args.residue_browsers or _RESIDUE:
        pids = _browsers_with_fishwb()
        print(f"# 残留自检[跑后·精确] 命令行带 fish-wb 的 Edge = "
              f"{len(pids) if pids is not None else '判不到'}"
              + (f"（PID {pids}）" if pids else "")
              + (f" · 本脚本删不掉的 profile：{_RESIDUE}" if _RESIDUE else ""))

    total = passed + failed + skipped
    print(f"\n# 汇总 {passed}/{total} PASS · {failed} FAIL · {skipped} SKIP · "
          f"变异自证失败 {mutation_fail} 条 · 变异自证 SKIP {mutation_skip} 条（未自证，不算通过）")
    return 0 if (failed == 0 and skipped == 0 and mutation_fail == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
