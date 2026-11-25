#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fish 写进 WorkBuddy 的 mcp.json —— **缺省只读（`--check`），写必须 `--apply`**。

## 三档入口（P1-10，外部审查 2026-11-25；与 install_hooks 统一）

    python install_mcp.py                # 缺省 = **只读**：定位 + 比对 + 报差异（零写盘）
    python install_mcp.py --check        # 同上（显式写法）
    python install_mcp.py --dry-run      # 同上（别名）
    python install_mcp.py --apply        # 真的写：备份 → 合并 → 回读校验

## 为什么缺省从"写"改成"只读"

旧版缺省动作是**写宿主配置**，而 `--check` 的帮助文案还写着"缺省是写"——把危险默认
写在帮助里。与 `install_hooks.py`（写类动作必须 `--apply`）不一致：一次误执行
`python install_mcp.py` 就会改 `~/.workbuddy/mcp.json`。审查方点名这是 P1-10。
既有脚本若依赖"无参数即写"，改成显式加 `--apply`（迁移只有这一步）。

## 写档的家目录护栏（P1-10 后半）

写之前先过 `_refuse_unsafe_target`：目标必须在 `fish_hosts.resolve_home(spec)` 之下，
占位符未解析 / 网络路径（UNC）一律拒绝。为什么只拦写不拦读：`--check` 读错路径最多是
"判不到"，而 `--apply` 会**真的写进去**一份没人预期的配置。
`--file` 是演练/测试用的显式路径，不受"必须在宿主家目录下"这条约束（人称即授权）；
环境变量 `FISH_WORKBUDDY_CFG` 会**被子进程继承**，所以它仍受护栏约束。

## 参数化到哪一层（不新增写宿主的能力）

- **目标路径**：从 `fish_hosts` 的 WorkBuddy spec 取（`config_paths` 里 `role="mcp"`），
  家目录按 `WORKBUDDY_HOME` → `~/.workbuddy` 解析；`FISH_WORKBUDDY_CFG` 仍可整体覆盖
  （测试/演练用）。**没有**新增 `DSH_HOME`/`CODEX_HOME` 等写入能力。
- **MCP 条目**：从 `HostSpec.mcp_entry` 模板渲染（`{python}`/`{server}`/`{root}` 现场填）。
- **其它宿主**：`--host dsh` 之类会**响亮拒绝**并点名 `fish_hosts.BLOCKED_ACTIONS`——
  它们的 MCP 序列化/合并语义各不相同（DSH 是 YAML+合并规则、Codex 是 TOML 段、
  ZCode/CodeBuddy 是另外两种键名），每接一个都要一套「保序+保注释+保其它键」的
  写入器。一期边界是「宿主层只能看不能写」，本工具不越界。
"""
import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish_hosts  # noqa: E402  宿主注册表：目标路径与 MCP 条目模板的唯一真相

# 2026-10-29（发布前脱敏）：路径不再硬编码本机绝对路径。
# 2026-11-24（docs/106 W5）：目标从 spec 取，`FISH_WORKBUDDY_CFG` 仍可整体覆盖。
ENV_CFG = "FISH_WORKBUDDY_CFG"
ENV_PYTHON = "FISH_PYTHON"
DEFAULT_HOST = "workbuddy"
PY = os.environ.get(ENV_PYTHON, sys.executable)
SERVER = str(HERE / "mcp_server.py")
DATA = str(HERE / "data")

#: 有 MCP 配置落点的宿主（`--host` 的合法取值；能不能写由 `mcp_entry` 决定）。
MCP_HOSTS = tuple(spec.id for spec in fish_hosts.spec_all() if spec.mcp_configs)


def _mcp_cfg_path(host: str, file=None) -> Path:
    """目标 mcp 配置路径：`--file` > `FISH_WORKBUDDY_CFG` > spec 的 mcp 落点。"""
    if file:
        return Path(file)
    override = (os.environ.get(ENV_CFG) or "").strip()
    if override:
        return Path(override)
    spec = fish_hosts.spec_of(host)
    probe = next(iter(spec.mcp_configs), None)
    if probe is None:
        raise ValueError(f"{host}: spec 没有声明 MCP 配置落点（role='mcp'）")
    home, reason = fish_hosts.resolve_home(spec)
    path, why = fish_hosts.resolve_path(probe.path_template, home)
    if path is None:
        raise ValueError(f"{host}: MCP 配置路径解析不出来：{why}（家目录：{reason}）")
    return Path(path)


def _fill(value, mapping: dict):
    """把模板里的 `{python}`/`{server}`/`{root}` 填成实值（递归，保序）。"""
    if isinstance(value, str):
        text = value
        for key, replacement in mapping.items():
            text = text.replace("{" + key + "}", replacement)
        return text
    if isinstance(value, list):
        return [_fill(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, mapping) for key, item in value.items()}
    return value


def fish_entry(host: str) -> dict:
    """从 `HostSpec.mcp_entry` 模板渲染出 fish 的 MCP 条目（键序与历史逐字一致）。"""
    spec = fish_hosts.spec_of(host)
    if spec.mcp_entry is None:
        raise ValueError(f"{host}: spec 没有声明 MCP 条目模板（mcp_entry）")
    return _fill(dict(spec.mcp_entry), {"python": PY, "server": SERVER, "root": DATA})


def _refusal(host: str) -> str:
    """非 WorkBuddy 宿主的**响亮拒绝**文案（点名 BLOCKED_ACTIONS）。"""
    lines = [f"[{host}] 拒绝：本工具一期只支持 WorkBuddy 的 MCP 写入——不新增写宿主的能力。"]
    for action in fish_hosts.BLOCKED_ACTIONS:
        lines.append(f"  · {action}：{fish_hosts.BLOCKED_REASONS.get(action, '')}")
    lines.append("  原因：各宿主的 MCP 序列化/合并语义各不相同（DSH 是 YAML + 合并规则、"
                 "Codex 是 TOML 段、ZCode/CodeBuddy 是另外两种键名），"
                 "每接一个都要一套「保序 + 保注释 + 保其它键」的写入器；"
                 "一期边界是「宿主层只能看不能写」（fish_hosts.BLOCKED_ACTIONS）。")
    return "\n".join(lines)


def _refuse_unsafe_target(host: str, cfg: Path, *, explicit: bool) -> str | None:
    """写类动作的**家目录护栏**（P1-10）→ 拒绝时返回文案，放行返回 `None`。

    三条，全部只作用于**写**（`--check` 读什么都不改现场）：

    1. **占位符没解析**：写进一个含 `{home}` / `%ENV%` 的路径，等于在磁盘上造一个
       叫 `{home}` 的目录/文件——读的时候永远"不存在"，写的时候不报错。
    2. **网络路径（UNC）**：`\\\\server\\share` 上的配置本机宿主读不到，而且写远程共享
       是另一个信任面（这里不做）。
    3. **不在宿主家目录之下**：正常落点是 `resolve_home(spec)` 里的 `mcp.json`；
       跑到别处说明解析出了问题（或环境变量指错），那不是"用户想要的那份配置"。

    `explicit`（`--file`）放行第三条：命令行上点名的路径是人的显式授权，也是演练/
    测试唯一的入口。`FISH_WORKBUDDY_CFG` **不放行**——环境变量会被子进程继承，
    一次设置会在所有后续调用里生效，风险不对称。
    """
    text = str(cfg)
    token = fish_hosts._unresolved_token(text)
    if token:
        return (f"[{host}] 拒绝写入：目标路径含未解析占位符 {token}（{text}）。\n"
                "  写进这种路径不会报错，但宿主永远读不到——先把环境变量设好。")
    if text.startswith("\\\\") or text.startswith("//"):
        return (f"[{host}] 拒绝写入：目标是网络路径（{text}）。\n"
                "  本工具只写本机宿主配置；远程共享是另一个信任面，不做。")
    if explicit:
        return None
    home, reason = fish_hosts.resolve_home(fish_hosts.spec_of(host))
    if not home:
        return (f"[{host}] 拒绝写入：宿主家目录解析不出来（{reason}）。\n"
                "  确认目标请用 `--file <路径>` 显式指定。")
    target = os.path.normcase(os.path.abspath(text))
    root = os.path.normcase(os.path.abspath(home))
    if target != root and not target.startswith(root + os.sep):
        return (f"[{host}] 拒绝写入：目标 {text} 不在宿主家目录 {home} 之下。\n"
                "  正常落点由 spec 决定；确认要写这里请用 `--file <路径>` 显式指定。")
    return None


def cmd_check(host: str, cfg: Path) -> int:
    """只读档：定位 + 比对 + 报差异。**一个字节都不写**（含备份也不做）。"""
    print(f"[{host}] {cfg}")
    want = fish_entry(host)
    if not cfg.exists():
        print("  ✗ 配置文件不存在（`--apply` 需要它已存在；本档不写盘）")
        return 2
    try:
        obj = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ✗ 读不出来：{type(exc).__name__}: {exc}")
        return 2
    servers = obj.get("mcpServers") or {}
    have = servers.get("fish")
    others = sorted(name for name in servers if name != "fish")
    if have == want:
        print("  ✓ fish 条目与目标一致（本档零写盘）")
    else:
        print("  ✗ fish 条目与目标不一致（本档**零写盘**；`--apply` 才会写入这份差异）")
        if have is None:
            print("    · 现状：没有 fish 条目")
        else:
            print(f"    · 现状：{json.dumps(have, ensure_ascii=False, sort_keys=True)}")
        print(f"    · 目标：{json.dumps(want, ensure_ascii=False, sort_keys=True)}")
    if others:
        print(f"  ✓ 其它 server 保持不变：{', '.join(others)}")
    return 0 if have == want else 2


def cmd_apply(host: str, cfg: Path) -> int:
    """写档（**只能由 `--apply` 走到**，P1-10）：先备份，再合并写入，再回读校验。

    为什么写落在"备份 → 合并 → 回读"三步而不是直接覆写：宿主配置里还有别人的
    `mcpServers`，覆写会静默删掉它们；回读则是"写完再判一次"——写坏的配置比没写更糟。
    """
    if not cfg.exists():
        print(f"[{host}] ✗ 配置不存在：{cfg}")
        return 1
    want = fish_entry(host)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = cfg.with_name(f"{cfg.name}.bak-{ts}-before-fish")

    # 1. 备份（保留元数据）
    shutil.copy2(cfg, bak)
    assert bak.stat().st_size == cfg.stat().st_size, "备份大小不一致，中止"
    print(f"[1/4] 已备份 → {bak.name} ({bak.stat().st_size} B)")

    # 2. 读原文件
    text = cfg.read_text(encoding="utf-8")
    obj = json.loads(text)
    before = list(obj.get("mcpServers", {}))
    print(f"[2/4] 原有 server：{before}")

    # 3. 写入 fish（已存在则覆盖）
    obj.setdefault("mcpServers", {})["fish"] = want
    cfg.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[3/4] 已写入 fish（共 {len(obj['mcpServers'])} 个 server）")

    # 4. 回读校验
    check = json.loads(cfg.read_text(encoding="utf-8"))
    assert "fish" in check["mcpServers"], "回读失败：fish 不存在"
    assert all(key in check["mcpServers"] for key in before), "回读失败：原有 server 丢失"
    assert check["mcpServers"]["fish"] == want, "回读失败：fish 条目与目标不一致"
    print("[4/4] 回读校验通过")
    print("\n当前配置：")
    for name, conf in check["mcpServers"].items():
        print(f"  - {name:16} type={conf.get('type', '(推断)'):14} "
              f"disabled={conf.get('disabled', False)}")
    return 0


def main(argv=None) -> int:
    # 控制台/计划任务下 stdout 是 GBK：`✓` 与中文会抛 UnicodeEncodeError（"功能正常但
    # 退出码 1"的假失败），也会让 `subprocess(text=True, encoding="utf-8")` 的调用方解码失败。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:      # noqa: BLE001 —— 不可重配的流（测试捕获对象）保持原样
            pass
    ap = argparse.ArgumentParser(
        description="把 fish 写进宿主的 MCP 配置（一期只支持 WorkBuddy）"
                    "——**缺省只读，写必须 --apply**")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="只读：定位 + 比对 + 报差异，零写盘（**缺省就是这个**）")
    mode.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="--check 的别名（零写盘）")
    mode.add_argument("--apply", action="store_true",
                      help="真的写入：先备份，再合并，再回读校验")
    ap.add_argument("--host", default=DEFAULT_HOST, choices=list(MCP_HOSTS),
                    help=f"目标宿主（缺省 {DEFAULT_HOST}；只有它有 MCP 条目模板）")
    ap.add_argument("--file", default=None,
                    help="显式指定配置文件（测试/演练用；会绕开「必须在宿主家目录下」这条护栏）")
    args = ap.parse_args(argv)

    try:
        cfg = _mcp_cfg_path(args.host, args.file)
    except (KeyError, ValueError) as exc:
        print(f"✗ {exc}")
        return 2
    spec = fish_hosts.spec_of(args.host)
    if spec.mcp_entry is None:
        print(_refusal(args.host))
        return 2
    if not args.apply:
        # P1-10：**缺省只读**。写是唯一会改宿主配置的动作，必须被显式点名。
        return cmd_check(args.host, cfg)
    refusal = _refuse_unsafe_target(args.host, cfg, explicit=bool(args.file))
    if refusal:
        print(refusal)
        return 2
    return cmd_apply(args.host, cfg)


if __name__ == "__main__":
    sys.exit(main())
