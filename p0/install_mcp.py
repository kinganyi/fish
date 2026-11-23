#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fish 写进 WorkBuddy 的 mcp.json —— **默认仍是「先备份，再写，再回读校验」**，
另提供只读档（`--check` / `--dry-run`）先看清楚再决定要不要写。

## 三档入口（设计 106 §5：只读化 + 参数化）

    python install_mcp.py                # 缺省 = 写（与历史行为逐字一致）
    python install_mcp.py --check        # 只读：定位 + 比对 + 报差异，**零写盘**
    python install_mcp.py --dry-run      # 同上（别名，动作语义更直白）
    python install_mcp.py --apply        # 显式写（与缺省同一条路径）

## 为什么默认不改（与设计 §5 的一处偏离，已在 docs/106 执行记录里说明）

设计原稿建议「`--check` 为缺省档」。实施时按任务书口径保留**缺省=写**：这条判据是
「默认行为与输出文本与现在一致」，而现行为就是写。把缺省改成只读会让既有调用
（旧脚本/习惯）静默不写——那是另一种「悄悄没生效」，正是本工具最该防的失败。
真正新增的是**只读档本身**：先 `--check` 看清差异，再决定 `--apply`。

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


def cmd_check(host: str, cfg: Path) -> int:
    """只读档：定位 + 比对 + 报差异。**一个字节都不写**（含备份也不做）。"""
    print(f"[{host}] {cfg}")
    want = fish_entry(host)
    if not cfg.exists():
        print("  ✗ 配置文件不存在（`--apply` 会新建，但本档不写盘）")
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
        print("  ✗ fish 条目与目标不一致（`--apply` 会写入这份差异）")
        if have is None:
            print("    · 现状：没有 fish 条目")
        else:
            print(f"    · 现状：{json.dumps(have, ensure_ascii=False, sort_keys=True)}")
        print(f"    · 目标：{json.dumps(want, ensure_ascii=False, sort_keys=True)}")
    if others:
        print(f"  ✓ 其它 server 保持不变：{', '.join(others)}")
    return 0 if have == want else 2


def cmd_apply(host: str, cfg: Path) -> int:
    """写档（= 历史行为，逐字保留）：先备份，再合并写入，再回读校验。"""
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
    ap = argparse.ArgumentParser(
        description="把 fish 写进宿主的 MCP 配置（一期只支持 WorkBuddy）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="只读：定位 + 比对 + 报差异，零写盘（缺省是写）")
    mode.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="--check 的别名（零写盘）")
    mode.add_argument("--apply", action="store_true", help="显式写入（与缺省同一条路径）")
    ap.add_argument("--host", default=DEFAULT_HOST, choices=list(MCP_HOSTS),
                    help=f"目标宿主（缺省 {DEFAULT_HOST}；只有它有 MCP 条目模板）")
    ap.add_argument("--file", default=None, help="显式指定配置文件（测试/演练用；零副作用）")
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
    if args.check or args.dry_run:
        return cmd_check(args.host, cfg)
    return cmd_apply(args.host, cfg)


if __name__ == "__main__":
    sys.exit(main())
