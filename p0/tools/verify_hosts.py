#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宿主接入层一期的机检 —— p0/tools/verify_hosts.py

## 判什么（九组，每组都能失败）

| # | 判据 | 怎么让它红（变异） |
|---|---|---|
| ① | `GET /api/hosts` 至少返回本机真实装了的 dsh/workbuddy/zcode/codex，且**与本进程现探的结果逐条对拍** | 把某 spec 的 `config_paths` 指到不存在的路径、家目录也去掉 → `installed=false`，列表里就少一个 |
| ② | 每项的 `configPath` **真存在**；为 null 时必须给原因（`missing` 或 `path_unresolved`）且 `fishPresence=none`（**不许「判不到」被报成「没有」**） | 把 `configPath` 换成不存在的路径 → 判据红 |
| ③ | `fishPresence` 与**逐字 grep 配置文本**的结论一致（不复用 `fish_hosts` 的结构解析：这是独立证据） | 把某项的 `fishPresence` 改成 `mcp`（真值是 both）→ 判据红 |
| ④ | `protocol` **取自** `fish_naming.SESSION_HOSTS`：非空值必须能被 `split_session_key()` 当成合法前缀（调应用自己的函数，不自建解析器） | 把某项 protocol 写成 `codex`（不在词表里）→ 判据红 |
| ⑤ | `POST /api/hosts/<id>/refresh` 返回 200，且**探测前后所有宿主配置文件逐字节不变**（sha256 + mtime 纳秒 + 字节数 + fish 写面白名单的目录清单） | 把比较函数的 before 里 mtime 改 1 ns → 判据红 |
| ⑥ | 一致性 warning **不阻断**：词表缺失时只产 warning，`/api/hosts` 仍出全量 | 从注册表里摘掉一个 `SESSION_HOSTS` 值 → warning 出现；不阻断由同一个断言保证 |
| ⑦ | `version` 必须是 **asar 里应用清单**的版本（**独立实现**重新读一遍对拍）；Electron 的 `version` 文件只许出现在 `electronVersion`，**任何宿主的 `version` 都不许等于它** | 把 `version` 换成 Electron 版本（DSH `44.7.0`）→ 判据红 |
| ⑧ | `push` 能力必须由**已装采集器脚本的源码**推出（独立正则读源码，与端点逐条对拍）；WorkBuddy 的 `fish_posttool_hook.py` 必须判「已接」；同一份脚本挂在两个宿主上结论必须一致 | 把 command 指向 `fish_session_hook.py`（无推送入口）→ push 必须从 `yes` 翻成 `no` |
| ⑨ | 占位符家目录（`<用户名>` / 未展开的 `%VAR%` / `$VAR`）→ `configState=path_unresolved` + `configPath=null` + 原因点名占位符（**不许** `abspath` 拼出 `I:\hooks.json` 这种编的路径） | 把判定函数喂给旧行为的读数（`missing` + `I:\hooks.json`）→ 必须判红 |

`fishPresence` 的三态在这里是四档：`both/hooks/mcp/none`——**`none` 是「没找到」，
不是「判不到」**；判不到（如配置读不出）由 `configState=unreadable` 单独表达，
路径没解析出来则是 `configState=path_unresolved`（⑨）。

⑦ 为什么另写一份 asar 读取：判据要的是**独立证据**。若这里改调
`fish_hosts.asar_package_version`，「偏移算法错了」时两边会一起错，判据就只剩自说自话。
两边独立算出同一个版本，那条读数才可信。

## 用法

    python -X utf8 p0\\tools\\verify_hosts.py --api http://127.0.0.1:8790
    python -X utf8 p0\\tools\\verify_hosts.py --api http://127.0.0.1:8790 --verbose

只读：不构建、不改库、不写任何宿主配置；唯一的写动作是那条 `POST refresh`（服务端也不写盘）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_P0 = os.path.dirname(_HERE)
_REPO = os.path.dirname(_P0)
for _path in (_P0, _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fish_hosts    # noqa: E402  被测层（对拍 + 变异用；**判据不靠它自证**）
import fish_naming   # noqa: E402  会话键前缀词表（④ 要调它自己的解析器）
import fish_workbench_api as WB  # noqa: E402  端点实现（⑥ 要在进程内验「不阻断」）

RESULTS: list[tuple[str, bool, str]] = []


def record(cid: str, ok: bool, detail: str) -> None:
    RESULTS.append((cid, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {cid}\n      {detail}")


# ── HTTP（只读；POST 只用于 refresh）────────────────────────────────────────
def http_json(url: str, *, method: str = "GET", timeout: float = 20.0) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也要把信封读出来（三段文案是证据）
        body = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"raw": body[:200]}
        return exc.code, payload


# ── ① 列表：本机真实装了的必须出现，且与现探结果逐条对拍 ──────────────────
WANTED = ("dsh", "workbuddy", "zcode", "codex")


def check_list(api: str, db: str, verbose: bool) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    ids = [item.get("id") for item in items]
    local = {item["id"]: item for item in fish_hosts.detect_all(db_path=db)}
    expected = [host_id for host_id in local if local[host_id]["installed"]]
    wanted_local = [host_id for host_id in WANTED if local.get(host_id, {}).get("installed")]
    wanted_ok = all(host_id in ids for host_id in wanted_local)
    order_ok = ids == [host_id for host_id in fish_hosts.REGISTRY if host_id in ids]
    match_ok = ids == expected
    installed_ok = all(item.get("installed") is True for item in items)
    ok = status == 200 and bool(items) and wanted_ok and order_ok and match_ok and installed_ok
    detail = (f"HTTP {status} · 本机已装 {len(expected)} 个 {expected} · 端点返回 {ids} · "
              f"本机真实的几个 {wanted_local} 全在={wanted_ok} · 顺序对={order_ok} · "
              f"逐条对拍={match_ok}")
    if not match_ok:
        detail += "（不一致=读口进程可能是旧代码，重启后重跑）"
    record("① /api/hosts 返回本机已装宿主且与现探对拍", ok, detail)
    if verbose:
        print("      逐项：" + json.dumps(
            [{k: item.get(k) for k in ("id", "installed", "fishPresence", "configPath")}
             for item in items], ensure_ascii=False))


# ── ② configPath：真存在，或 null + 原因 ────────────────────────────────────
def _config_path_ok(items: list[dict]) -> tuple[bool, str]:
    problems: list[str] = []
    for item in items:
        host_id = item.get("id")
        path = item.get("configPath")
        if path:
            if not os.path.isfile(path):
                problems.append(f"{host_id}: configPath 不存在 {path}")
        else:
            reason = item.get("configReason") or ""
            # `missing` = 真不存在；`path_unresolved` = 路径没解析出来（判不到）。
            # 两态都允许 configPath=null，但都必须给原因——「判不到」不许说成「没有」。
            if item.get("configState") not in ("missing", "path_unresolved") or not reason.strip():
                problems.append(f"{host_id}: 没有 configPath 却没给原因（判不到被报成了没有）")
            if item.get("fishPresence") != "none":
                problems.append(f"{host_id}: 没有 configPath 但 fishPresence="
                                f"{item.get('fishPresence')}")
    return not problems, "；".join(problems)


def check_config_paths(api: str) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    paths_ok, problems = _config_path_ok(items)
    ok = paths_ok and status == 200
    with_path = [item for item in items if item.get("configPath")]
    without = [item for item in items if not item.get("configPath")]
    detail = (f"HTTP {status} · 有 configPath {len(with_path)} 个（逐个 isfile）· "
              f"无 configPath {len(without)} 个"
              + (f"（{[i.get('id') for i in without]}：都带 configState/configReason 原因）"
                 if without else ""))
    if problems:
        detail += f" · 问题：{problems}"
    record("② 每项 configPath 真存在（或 null + 原因）", ok, detail)

    # 变异：不存在的路径必须被抓到（否则「存在」这条判据没有判别力）
    broken = [{"id": "mutant", "configPath": os.path.join(_REPO, "__no_such_config__.json"),
               "configState": "ready", "fishPresence": "none"}]
    broken_ok, _ = _config_path_ok(broken)
    record("②-变异 不存在的 configPath 必须红", not broken_ok,
           f"变异输入 → {'未被抓到（判据失效）' if broken_ok else '被抓到（判据有效）'}")


# ── ③ fishPresence：逐字 grep 配置文本（独立证据，不复用结构解析）───────────
_JSON_FISH_KEY = re.compile(r'^\s*"fish"\s*:', re.M)
_TOML_FISH_SECTION = re.compile(r"^\s*\[mcp_servers\.fish[.\]]", re.M)
_YAML_FISH_SERVER = re.compile(r"^\s*serverName:\s*fish\s*$", re.M)
_HOOK_SCRIPT_HINT = re.compile(r"fish_[a-z_]+\.py")


def _text_presence(item: dict) -> str:
    """从配置**文本**逐字判断 fish 接入通道 → `none/hooks/mcp/both`。

    为什么另写一份：③ 要的是**独立证据**。如果这里改调 `fish_hosts.inspect_config`，
    那么「解析器有 bug」时两边会一起错，判据就只剩自说自话。
    """
    hooks = mcp = False
    for config in item.get("configs") or []:
        path = config.get("path")
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if config.get("role") == "hooks" and _HOOK_SCRIPT_HINT.search(text):
            hooks = True
        if config.get("role") == "mcp":
            fmt = config.get("format")
            if fmt == "json" and _JSON_FISH_KEY.search(text):
                mcp = True
            elif fmt == "toml" and _TOML_FISH_SECTION.search(text):
                mcp = True
            elif fmt == "yaml" and _YAML_FISH_SERVER.search(text):
                mcp = True
    return "both" if hooks and mcp else "hooks" if hooks else "mcp" if mcp else "none"


def _presence_consistent(items: list[dict]) -> tuple[bool, str, list[dict]]:
    """逐项比对「端点给的 fishPresence」与「配置文本 grep 出来的通道」。"""
    rows = []
    for item in items:
        expected = _text_presence(item)
        rows.append({"id": item.get("id"), "api": item.get("fishPresence"),
                     "grep": expected, "match": expected == item.get("fishPresence")})
    bad = [row for row in rows if not row["match"]]
    detail = f"逐字 grep 结论 {json.dumps(rows, ensure_ascii=False)}"
    return not bad, detail, rows


def check_fish_presence(api: str, verbose: bool) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    ok, detail, rows = _presence_consistent(items)
    record("③ fishPresence 与配置文本逐字 grep 一致", ok and status == 200,
           f"HTTP {status} · {detail}")

    # 变异：把 dsh 的结论改成 mcp（真值是 both）→ 必须红
    mutated = [dict(row) for row in rows]
    hit = next((row for row in mutated if row["id"] == "dsh"), None)
    if hit is None:
        record("③-变异 篡改 fishPresence 必须红", False, "本机没有 dsh，变异无法构造")
        return
    hit["api"] = "mcp" if hit["grep"] != "mcp" else "hooks"
    mutant_ok = all(row["api"] == row["grep"] for row in mutated)
    record("③-变异 篡改 fishPresence 必须红", not mutant_ok,
           f"变异后 api={hit['api']} vs grep={hit['grep']} → "
           f"{'未被抓到（判据失效）' if mutant_ok else '被抓到（判据有效）'}")


# ── ④ protocol 取自 SESSION_HOSTS（调应用自己的解析器）──────────────────────
def _protocol_rows(items: list[dict]) -> list[dict]:
    rows = []
    for item in items:
        protocol = (item.get("protocol") or "").strip()
        if not protocol:
            rows.append({"id": item.get("id"), "protocol": "", "legal": True,
                         "note": "无会话键前缀（空值是显式声明，不是漏填）"})
            continue
        host, ident = fish_naming.split_session_key(f"{protocol}:probe")
        legal = bool(host == protocol and ident)
        rows.append({"id": item.get("id"), "protocol": protocol,
                     "legal": legal, "note": "" if legal else "不在 SESSION_HOSTS 里"})
    return rows


def check_protocol(api: str) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    vocab_ok = fish_hosts.PROTOCOLS == tuple(fish_naming.SESSION_HOSTS)
    rows = _protocol_rows(items)
    ok = status == 200 and vocab_ok and all(row["legal"] for row in rows)
    record("④ protocol 取自 SESSION_HOSTS 且是合法会话键前缀", ok,
           f"HTTP {status} · 词表同源={vocab_ok}（PROTOCOLS==SESSION_HOSTS）· "
           f"逐项 {json.dumps(rows, ensure_ascii=False)}")

    # 变异：把某项 protocol 写成词表外的值 → 必须红
    mutated = [{"id": "mutant", "protocol": "codex"}]
    mutant_ok = all(row["legal"] for row in _protocol_rows(mutated))
    record("④-变异 词表外 protocol 必须红", not mutant_ok,
           f"变异输入 protocol=codex → {'未被抓到（判据失效）' if mutant_ok else '被抓到（判据有效）'}")


# ── ⑤ refresh 不写盘：指纹（sha256 + mtime 纳秒 + 字节数 + 写面白名单）────────
def _fish_write_whitelist(paths: list[str]) -> dict[str, set[str]]:
    """fish 的写面白名单：注册表里的宿主配置文件名 + 原子写的临时名 `<name>.tmp`。

    为什么需要白名单（P2-3，2026-11-26）：WorkBuddy 的配置目录就是它的家目录
    `~/.workbuddy`，里面 `sessions/` / `logs/` 由宿主自己持续写入。旧实现
    `os.listdir(folder)` 把整目录条目都算进快照，于是这条判据在活宿主上周期性
    20/21——那是宿主在干活，不是 fish 的 refresh 写盘。判据只该盯 fish 会写的东西：
    `install_hooks` / `install_mcp` 只会写这些配置文件（原子替换用 `<name>.tmp`）。
    """
    out: dict[str, set[str]] = {}
    for path in paths:
        folder = os.path.dirname(path)
        name = os.path.basename(path)
        out.setdefault(folder, set()).update((name, name + ".tmp"))
    return out


def _dir_snapshot(paths: list[str]) -> dict[str, list[str]]:
    """配置目录里**只列 fish 会写的文件名**（白名单），不列宿主自写目录。"""
    out: dict[str, list[str]] = {}
    for folder, names in _fish_write_whitelist(paths).items():
        try:
            entries = os.listdir(folder)
        except OSError:
            entries = []
        out[folder] = sorted(name for name in entries if name in names)
    return out


def _diff_fingerprints(before: dict, after: dict) -> list[str]:
    problems: list[str] = []
    for path, item in before.items():
        other = after.get(path)
        if other is None:
            problems.append(f"消失：{path}")
            continue
        for key in ("sha256", "size", "mtimeNs"):
            if item[key] != other[key]:
                problems.append(f"{path} 的 {key} 变了（{item[key]} → {other[key]}）")
    for path in after:
        if path not in before:
            problems.append(f"新增：{path}")
    return problems


def check_refresh(api: str, verbose: bool) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    ids = [item.get("id") for item in items]

    before = fish_hosts.config_fingerprints()
    before_dirs = _dir_snapshot(list(before))
    codes: list[str] = []
    for host_id in ids:
        code, _body = http_json(f"{api}/api/hosts/{host_id}/refresh", method="POST")
        codes.append(f"{host_id}={code}")
    after = fish_hosts.config_fingerprints()
    after_dirs = _dir_snapshot(list(before))
    problems = _diff_fingerprints(before, after)
    dir_problems = [f"{folder} 目录清单变了" for folder in before_dirs
                    if before_dirs[folder] != after_dirs.get(folder)]
    ok = all(entry.endswith("=200") for entry in codes) and not problems and not dir_problems
    detail = (f"POST {codes} · 指纹 {len(before)} 份 · 逐字节相同={not problems} · "
              f"写面白名单不变={not dir_problems}")
    if problems:
        detail += f" · 差异：{problems[:4]}"
    if dir_problems:
        detail += f" · {dir_problems[:3]}"
    record("⑤ refresh 200 且配置文件逐字节不变", ok, detail)
    if verbose:
        print("      指纹：" + json.dumps(before, ensure_ascii=False)[:600])

    # 变异：把 before 里的一份 mtime 改 1ns → 比较函数必须报差异
    if before:
        probe = dict(next(iter(before.values())))
        tampered = dict(before)
        tampered[probe["path"]] = {**probe, "mtimeNs": probe["mtimeNs"] + 1}
        mutant_problems = _diff_fingerprints(tampered, after)
        record("⑤-变异 mtime 变化必须红", bool(mutant_problems),
               f"变异后差异 {mutant_problems}")
    else:
        record("⑤-变异 mtime 变化必须红", False, "本机没有任何宿主配置文件，变异无法构造")


# ── ⑥ 一致性 warning 不阻断 ────────────────────────────────────────────────
def check_consistency(api: str, db: str) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    data = (payload.get("data") or {})
    live = data.get("consistency") or {}
    live_ok = status == 200 and live.get("ok") is True and not live.get("warnings")

    # 变异：从注册表里摘掉一个 SESSION_HOSTS 值 → 必须产 warning，且端点仍出全量
    original = fish_hosts.HOSTS
    dropped = next((host_id for host_id in fish_naming.SESSION_HOSTS
                    if host_id in fish_hosts.REGISTRY and host_id not in ("dsh", "workbuddy",
                                                                          "zcode", "codex")),
                   None)
    try:
        if dropped is None:
            record("⑥-变异 词表缺失必须产 warning", False, "找不到可摘掉的词表值，变异无法构造")
            record("⑥ 一致性 warning 不阻断 /api/hosts", live_ok,
                   f"HTTP {status} · consistency={live}")
            return
        fish_hosts.HOSTS = tuple(spec for spec in original if spec.id != dropped)
        mutated = fish_hosts.consistency()
        # 「不阻断」= 同一个函数在 warning 状态下照样返回全量，不抛错
        still_serving = WB.api_hosts(db)
        mutated_items = (still_serving.get("data") or {}).get("items") or []
        warned = bool(mutated.get("warnings"))
        not_blocked = (still_serving.get("schema") == WB.SCHEMA and bool(mutated_items))
    finally:
        fish_hosts.HOSTS = original

    record("⑥ 一致性 warning 不阻断 /api/hosts", live_ok,
           f"HTTP {status} · consistency={live}")
    record("⑥-变异 词表缺失必须产 warning 且不阻断",
           warned and not_blocked,
           f"摘掉 {dropped} → warnings={mutated.get('warnings')} · "
           f"端点仍返回 {len(mutated_items)} 项（不抛错）")


# ── ⑦ 版本：应用版本来自 asar 的应用清单，Electron 版本不许冒充 ──────────────
def _split_version_source(source: str | None) -> tuple[str | None, str | None]:
    """把 `versionSource`（形如 `<asar>（asar 内 <inner>）`）拆成 `(asar 路径, 内部路径)`。"""
    if not source or "（asar 内 " not in source:
        return None, None
    head, _, tail = source.partition("（asar 内 ")
    return head.strip() or None, tail.rstrip("）").strip() or None


def _asar_version_independent(asar_path: str, inner_path: str) -> str | None:
    """**独立实现**的 asar 读取（**不复用** `fish_hosts.asar_package_version`）。

    判据要的是独立证据：如果这里改调被测函数，「偏移算法错了」时两边会一起错，
    判据就只剩自说自话。所以这里另写一份：按官方口径 `16 + headerSize` 读，
    再扫 0..7 字节位移兜底（本机实测有的 asar 头后面带填充）。两边独立算出同一个
    版本，那条读数才可信。
    """
    try:
        with open(asar_path, "rb") as handle:
            prefix = handle.read(16)
            if len(prefix) < 16:
                return None
            header_size = struct.unpack("<I", prefix[12:16])[0]
            if header_size <= 0:
                return None
            header = json.loads(handle.read(header_size).decode("utf-8"))
            node = header
            for part in inner_path.replace("\\", "/").split("/"):
                if part:
                    node = node["files"][part]
            offset, length = int(node["offset"]), int(node["size"])
            for shift in range(0, 8):
                handle.seek(16 + header_size + shift + offset)
                blob = handle.read(length)
                if len(blob) != length:
                    continue
                try:
                    document = json.loads(blob.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                value = document.get("version")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:  # noqa: BLE001  判据里「算不出来」= None，由调用方决定红不红
        return None
    return None


def _first_line(path: str) -> str | None:
    """读一个文本文件的第一行（Electron 的 `version` 文件就是这个形状）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return text.splitlines()[0].strip() if text else None


def _version_problems(items: list[dict], expected: dict[str, str],
                      electron: dict[str, str]) -> list[str]:
    """逐项对拍版本读数 → 问题清单（**空 = 通过**）。

    三条断言：
      ① `version` 必须等于**独立复算**的 asar 应用版本（`expected` 里有这项才参与）；
      ② `electronVersion` 必须等于 Electron `version` 文件的内容（读了就要读对）；
      ③ **任何**宿主的 `version` 都不许等于某个 Electron 版本——那就是「冒充」。
    """
    problems: list[str] = []
    electron_values = {value for value in electron.values() if value}
    for item in items:
        host_id = item.get("id")
        got = item.get("version")
        want = expected.get(host_id)
        if want is not None and got != want:
            problems.append(f"{host_id}: version={got!r} ≠ asar 应用清单 {want!r}")
        ev = electron.get(host_id)
        if ev is not None and item.get("electronVersion") != ev:
            problems.append(
                f"{host_id}: electronVersion={item.get('electronVersion')!r} ≠ version 文件 {ev!r}")
        if got and got in electron_values:
            problems.append(f"{host_id}: version={got!r} 是 Electron 运行时版本（冒充应用版本）")
    return problems


def check_versions(api: str) -> None:
    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []

    expected: dict[str, str] = {}
    electron: dict[str, str] = {}
    for item in items:
        host_id = item.get("id")
        asar_path, inner_path = _split_version_source(item.get("versionSource"))
        if asar_path and inner_path and os.path.isfile(asar_path):
            value = _asar_version_independent(asar_path, inner_path)
            if value:
                expected[host_id] = value
        source = item.get("electronSource")
        if source and os.path.isfile(source):
            value = _first_line(source)
            if value:
                electron[host_id] = value

    problems = _version_problems(items, expected, electron)
    # 「判据没空转」：至少要有宿主的应用版本能独立复算，否则这条绿是空的。
    if not expected:
        problems.append("没有任何宿主的 version 能用 asar 独立复算——这条判据这次没有判别力")
    record("⑦ version 来自 asar 应用清单（Electron 版本不许冒充）",
           status == 200 and not problems,
           f"HTTP {status} · 独立复算 {expected or '无'} · Electron 文件 {electron or '无'} · "
           f"问题：{problems or '无'}")

    # 变异 1：把 Electron 版本填进 `version` → 必须红（这正是 2026-11-24 修掉的那个真错）
    if electron:
        host_id, ev = next(iter(electron.items()))
        mutated = [dict(item) for item in items]
        for item in mutated:
            if item.get("id") == host_id:
                item["version"] = ev
        caught = bool(_version_problems(mutated, expected, electron))
        record("⑦-变异 把 Electron 版本填进 version 必须红", caught,
               f"{host_id}.version 改成 {ev!r} → "
               f"{'被抓到（判据有效）' if caught else '未被抓到（判据失效）'}")
    else:
        record("⑦-变异 把 Electron 版本填进 version 必须红", False,
               "本机没有可读的 Electron version 文件，变异无法构造")

    # 变异 2：把 `version` 改成对不上的值 → 必须红（防「只比类型不比值」的空判据）
    if expected:
        host_id, want = next(iter(expected.items()))
        mutated = [dict(item) for item in items]
        for item in mutated:
            if item.get("id") == host_id:
                item["version"] = f"{want}-tampered"
        caught = bool(_version_problems(mutated, expected, electron))
        record("⑦-变异 version 与 asar 清单不一致必须红", caught,
               f"{host_id}.version 改成 {want}-tampered → "
               f"{'被抓到（判据有效）' if caught else '未被抓到（判据失效）'}")
    else:
        record("⑦-变异 version 与 asar 清单不一致必须红", False,
               "没有任何可独立复算的版本，变异无法构造")


# ── ⑧ 推送通道：看已装采集器的**源码**，不看文件名（A2）───────────────────
#: 独立判据（**不复用** `fish_hosts.PUSH_ENTRY_MARKERS`）：源码里出现这些调用即算有推送。
_PUSH_CALL = re.compile(r"(?:maybe_push|select_push_candidates|append_push_log)\s*\(")


def _script_pushes_independently(path: str) -> bool | None:
    """独立读脚本源码 → 有没有推送入口；读不出返回 `None`（判不到，不许折成 False）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    return bool(_PUSH_CALL.search(text))


def _push_state_from_text(item: dict) -> str:
    """从端点的 `configs[].scripts` 独立推出 push 状态（`yes/no/unverified`）。"""
    scripts = [script for config in (item.get("configs") or [])
               if config.get("role") == "hooks" for script in (config.get("scripts") or [])]
    if not scripts:
        return "no"
    verdicts = [_script_pushes_independently(path) for path in scripts]
    if any(verdict is True for verdict in verdicts):
        return "yes"
    return "unverified" if any(verdict is None for verdict in verdicts) else "no"


def check_push_channel(api: str, db: str) -> None:
    """⑧ 推送能力必须由**已装采集器脚本的源码**推出来（WorkBuddy 不许被误判未接）。"""
    from dataclasses import replace  # noqa: PLC0415  只在变异处用

    status, payload = http_json(f"{api}/api/hosts")
    items = ((payload.get("data") or {}).get("items")) or []
    rows = []
    for item in items:
        expected = _push_state_from_text(item)
        cap = ((item.get("capabilities") or {}).get("push") or {})
        rows.append({"id": item.get("id"), "api": cap.get("state"), "grep": expected,
                     "match": cap.get("state") == expected,
                     "scripts": [os.path.basename(p) for config in (item.get("configs") or [])
                                 if config.get("role") == "hooks"
                                 for p in (config.get("scripts") or [])]})
    bad = [row for row in rows if not row["match"]]
    # 本进程现探与端点逐条对拍（读口可能跑着旧代码）
    local = {item["id"]: item for item in fish_hosts.detect_all(db_path=db)}
    drift = [row["id"] for row in rows
             if row["api"] != ((local.get(row["id"], {}).get("capabilities") or {})
                               .get("push") or {}).get("state")]
    workbuddy = next((item for item in items if item.get("id") == "workbuddy"), None)
    wb_cap = ((workbuddy or {}).get("capabilities") or {}).get("push") or {}
    wb_ok = (wb_cap.get("state") == "yes"
             and "fish_posttool_hook.py" in (wb_cap.get("evidence") or ""))
    ok = status == 200 and bool(items) and not bad and wb_ok
    detail = (f"HTTP {status} · 逐项 {json.dumps(rows, ensure_ascii=False)}"
              + (f" · 与现探不一致 {drift}（读口可能是旧代码，重启后重跑）" if drift else ""))
    record("⑧ push 能力由已装采集器源码推出（WorkBuddy=已接）", ok, detail)

    # ⑧-a 跨宿主一致：同一份脚本挂在两个 id 上，结论必须一模一样
    workbuddy_spec = fish_hosts.REGISTRY["workbuddy"]
    settings = os.path.expanduser("~/.workbuddy/settings.json")
    if not os.path.isfile(settings):
        record("⑧-a 同一脚本跨宿主结论一致", False, f"本机没有 {settings}，变异无法构造")
    else:
        probe = fish_hosts.ConfigProbe(
            role="hooks", label="settings.json", path_template=settings, format="json",
            locator=("hooks",), locator_text="hooks.<Event>")
        home = os.path.dirname(settings)
        base = dict(home_env=None, home_default=home, config_paths=(probe,),
                    version_probes=())
        spec_a = replace(workbuddy_spec, id="probe-a", display="A", **base)
        spec_b = replace(workbuddy_spec, id="probe-b", display="B", **base)
        cap_a = fish_hosts.detect(spec_a)["capabilities"]["push"]
        cap_b = fish_hosts.detect(spec_b)["capabilities"]["push"]
        same = cap_a == cap_b and cap_a["state"] == "yes"
        record("⑧-a 同一脚本跨宿主结论一致", same,
               f"A={cap_a['state']}/{cap_a['evidence']} · B={cap_b['state']} · 两份 spec 完全相同={cap_a == cap_b}")

        # ⑧-变异：把 command 指向**没有推送入口**的采集器 → 必须从 yes 翻成 no
        nopush = os.path.join(_P0, "fish_session_hook.py")
        tmp = os.path.join(tempfile.gettempdir(), "fish-hosts-verify-nopush.json")
        payload_mut = {"hooks": {"PostToolUse": [{"hooks": [{
            "type": "command", "command": f'python "{nopush}" --host workbuddy'}]}]}}
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload_mut, handle, ensure_ascii=False)
            mutant_probe = replace(probe, path_template=tmp)
            mutant = replace(spec_a, config_paths=(mutant_probe,))
            cap_mut = fish_hosts.detect(mutant)["capabilities"]["push"]
            caught = cap_a["state"] == "yes" and cap_mut["state"] == "no"
            record("⑧-变异 采集器没有推送入口 → push 必须判未接", caught,
                   f"对照组 {cap_a['state']} · 变异组 {cap_mut['state']}（{cap_mut['evidence']}）")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ── ⑨ 占位符家目录：必须是 path_unresolved，不许"编出路径"（A3）────────────
def _unresolved_violations(report: dict, token: str) -> list[str]:
    """A3 的判定函数：三条同时成立才算对。

      ① `configState == "path_unresolved"`（判不到，不是 missing）；
      ② `configPath is None`（**不许** `abspath`/`join` 拼出一个看似存在的路径）；
      ③ `configReason` 点名没解析的占位符/变量。

    这是一个纯函数：同一份判据喂给"编出来的路径"必须红（下面 ⑨-变异就这么做）。
    """
    problems: list[str] = []
    if report.get("configState") != "path_unresolved":
        problems.append(f"configState={report.get('configState')!r}（应为 'path_unresolved'）")
    if report.get("configPath") is not None:
        problems.append(f"configPath={report.get('configPath')!r}（应为 None——不许拼路径）")
    if token not in (report.get("configReason") or ""):
        problems.append(f"configReason 没点名占位符 {token!r}：{report.get('configReason')!r}")
    return problems


def check_path_unresolved(db: str) -> None:
    """⑨ 占位符路径未解析 → `path_unresolved` + `configPath=null` + 原因点名占位符。"""
    from dataclasses import replace  # noqa: PLC0415

    spec = fish_hosts.REGISTRY["dsh"]
    baseline = fish_hosts.detect(spec)
    baseline_ok = bool(baseline.get("configPath")) and os.path.isfile(baseline["configPath"])

    cases = (
        ("<用户名>", r"C:\Users\<用户名>\.dsh"),
        ("%__NO_SUCH_VAR__%", "%__NO_SUCH_VAR__%/.dsh"),
        ("$NOSUCHDIR", "$NOSUCHDIR/.dsh"),
    )
    rows = []
    for token, home in cases:
        mutant = replace(spec, home_env=None, home_default=home)
        report = fish_hosts.detect(mutant)
        rows.append({"token": token, "configState": report.get("configState"),
                     "configPath": report.get("configPath"),
                     "reason": report.get("configReason"),
                     "violations": _unresolved_violations(report, token)})
    problems = [f"{row['token']}: {row['violations']}" for row in rows if row["violations"]]
    record("⑨ 占位符家目录 → path_unresolved 且不编路径", baseline_ok and not problems,
           f"正对照 configPath={baseline.get('configPath')} · 逐例 "
           f"{json.dumps([{k: row[k] for k in ('token', 'configState', 'configPath')} for row in rows], ensure_ascii=False)}"
           + (f" · 问题：{problems}" if problems else ""))

    # ⑨-变异：同一个判定函数喂「编出来的路径」（旧行为：missing + I:\hooks.json）→ 必须红
    fake = {"configState": "missing", "configPath": r"I:\hooks.json",
            "configReason": "配置不存在：I:\hooks.json"}
    fake_problems = _unresolved_violations(fake, "<用户名>")
    record("⑨-变异 编出来的路径必须被判红", bool(fake_problems),
           f"变异输入 configPath={fake['configPath']!r} → {fake_problems or '未被抓到（判据失效）'}")


# ── ①-变异：spec 指向不存在的路径 → installed=false ─────────────────────────
def check_spec_mutation(db: str) -> None:
    from dataclasses import replace  # noqa: PLC0415  只在这里用

    spec = fish_hosts.REGISTRY["dsh"]
    baseline = fish_hosts.detect(spec, db_path=db)
    mutant = replace(
        spec,
        home_env=None,
        home_default=None,          # 家目录也不再给（否则 installed 会靠家目录为真）
        config_paths=tuple(replace(probe, path_template="__no_such__/{home}/x.json")
                           for probe in spec.config_paths),
        version_probes=(),
    )
    after = fish_hosts.detect(mutant, db_path=db)
    ok = baseline["installed"] is True and after["installed"] is False
    detail = (f"基线 installed={baseline['installed']} → 变异后 installed={after['installed']}"
              f"（configPath={after['configPath']}，configReason={after['configReason']}）")
    record("①-变异 config_paths 指空 → installed=false", ok, detail)


# ── main ───────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="宿主接入层一期机检（只读）")
    parser.add_argument("--api", default="http://127.0.0.1:8790", help="只读读口基址")
    parser.add_argument("--verbose", action="store_true", help="多打一些读数")
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")

    status, meta = http_json(f"{api}/api/meta")
    if status != 200:
        print(f"FAIL  读口不可用：{api}/api/meta → HTTP {status}")
        return 2
    db = ((meta.get("db") or {}).get("index")) or ""
    caps = meta.get("capabilities") or []
    print(f"# 判定时刻 {meta.get('asOf')} · 读口 {api}（pid {meta.get('serverPid')}）· 库 {db}")
    record("⓪ /api/meta.capabilities 含 hosts", "hosts" in caps, f"capabilities={caps}")

    check_list(api, db, args.verbose)
    check_config_paths(api)
    check_fish_presence(api, args.verbose)
    check_protocol(api)
    check_refresh(api, args.verbose)
    check_consistency(api, db)
    check_versions(api)
    check_push_channel(api, db)
    check_path_unresolved(db)
    check_spec_mutation(db)

    passed = sum(1 for _cid, ok, _detail in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n# 汇总 {passed}/{total} PASS")
    for cid, ok, _detail in RESULTS:
        if not ok:
            print(f"# 红：{cid}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
