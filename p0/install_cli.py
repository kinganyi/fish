#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""fish CLI 安装卡（FB-13）：把 `fish` 变成一条**能直接敲**的命令。

## 为什么需要它

外部评估 22-R4 / 21-P2 的共同结论：装好 MCP 的 AI 手上有**工具**、没有**命令**。
它想跑 `fish session start` 时得到的是
`'fish' 不是内部或外部命令`，然后**放弃并绕路**——绕路的具体形态，就是长期停在
`self-reported`（明明 CLI 能签 `machine` 级，却一直没用上）。

## 这个脚本做什么（以及**刻意不做什么**）

做：在 `%USERPROFILE%\.local\bin` 下生成 `fish.cmd`，内容是一条转发：
    @"<python.exe>" "<...>\p0\fish.py" %*
    然后把「要加进 PATH 的目录」和「怎么加」打印出来。

**不做**：不改 PATH、不写注册表、不复制 python、不建计划任务。
理由：改用户级 PATH 是**全局且难回滚**的动作（溢出 2047 字符会截断别人的路径），
而一个 shim 文件是**删掉就干净**的。所以把选择权留在人手上——脚本只负责正确的那一半。

## 默认演练

不加 `--apply` 时**一个文件都不写**，只打印计划（路径 + 内容 + PATH 命令）。
这和 `fish archive` / `fish project retire` 是同一条纪律：会改变系统形状的操作，
先给人看计划。

## 用法

    python install_cli.py                    # 演练：看要生成什么、PATH 加哪里
    python install_cli.py --apply            # 真生成 fish.cmd
    python install_cli.py --apply --bin-dir D:\tools\bin   # 换个目录（测试用）

生成之后**新开一个终端**再敲 `fish --help`（PATH 只在新进程里生效）。
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FISH_PY = HERE / "fish.py"
SHIM_NAME = "fish.cmd"


def default_bin_dir() -> Path:
    """默认 shim 目录：`%USERPROFILE%\\.local\\bin`（用户级、无需管理员、删掉即干净）。

    Windows 用 `USERPROFILE`（**不用** `HOME`：本机 `HOME` 可能是被其它工具改写过的值），
    其它平台退到 `~/.local/bin`（XDG 惯例）。
    """
    home = (os.environ.get("USERPROFILE") or "").strip()
    base = Path(home) if home else Path.home()
    return base / ".local" / "bin"


def shim_content(python_exe=None, fish_py=None) -> str:
    """`fish.cmd` 的内容：把参数原样转发给 `fish.py`。

    两个路径都加引号：本机的 python 与仓库路径都可能带空格（`Program Files` 就会），
    不加引号的 bat 会在空格处把路径切断——那是"装好了但一敲就报找不到文件"的经典成因。
    `%*` 原样转发全部参数（`fish board checkin --doing "..."` 这种带空格的参数也活）。
    """
    py = Path(python_exe or sys.executable).resolve()
    script = Path(fish_py or FISH_PY).resolve()
    return f'@"{py}" "{script}" %*\r\n'


def plan(bin_dir=None, python_exe=None, fish_py=None) -> dict:
    """算出"要生成什么、加哪个目录进 PATH"，**不落任何文件**。"""
    target_dir = Path(bin_dir) if bin_dir else default_bin_dir()
    script = Path(fish_py or FISH_PY).resolve()
    py = Path(python_exe or sys.executable).resolve()
    shim = target_dir / SHIM_NAME
    return {
        "bin_dir": target_dir,
        "shim_path": shim,
        "content": shim_content(py, script),
        "python": py,
        "fish_py": script,
        "path_dir": target_dir,
        "path_session_cmd": f'$env:PATH = "{target_dir};$env:PATH"',
        "path_persist_cmd": f'setx PATH "%PATH%;{target_dir}"',
    }


def render_plan(p: dict, applied: bool) -> str:
    """把计划渲染成给人看的几行（**先看得懂，再敢执行**）。"""
    tag = "已生成" if applied else "演练（没有写任何文件）"
    lines = [
        f"fish CLI 安装卡 · {tag}",
        f"  python   {p['python']}",
        f"  fish.py  {p['fish_py']}",
        f"  shim     {p['shim_path']}",
        f"  内容     {p['content'].strip()}",
        "",
        "需要把这个目录加进用户 PATH（**本脚本不改 PATH、不写注册表**）：",
        f"  {p['path_dir']}",
        f"  仅当前窗口： {p['path_session_cmd']}",
        f"  永久（用户级）：{p['path_persist_cmd']}",
        "  注意：setx 会把当前值整体覆盖——先 `echo %PATH%` 确认原有内容，",
        "        再决定是追加还是干脆只用\"仅当前窗口\"那条（重启后失效但零风险）。",
    ]
    if applied:
        lines += ["", f"完成：新开一个终端敲 `fish --help` 验证（PATH 只在新进程里生效）。"]
    else:
        lines += ["", "演练完毕：没有写任何文件。确认后加 --apply 生成 shim。"]
    return "\n".join(lines) + "\n"


def install(apply=False, bin_dir=None, python_exe=None, fish_py=None) -> dict:
    """生成 shim（`apply=True` 才写盘）。返回 `{...plan, applied, written, verified}`。

    写后**读回来逐字比对**：写成功但内容不对（编码/BOM/换行）时，"verify 一下"
    是唯一能当场发现的办法——而 shim 坏了的症状是"命令找得到、一跑就报错"，
    比没有 shim 更难查。
    """
    p = plan(bin_dir=bin_dir, python_exe=python_exe, fish_py=fish_py)
    p.update({"applied": bool(apply), "written": False, "verified": False})
    if not apply:
        return p
    p["bin_dir"].mkdir(parents=True, exist_ok=True)
    # newline="" + 显式 \r\n：cmd 的批处理对裸 \n 容错不好（尤其带引号的命令行）。
    # 2026-10-26 合并后修复：原来写死 ascii——主树路径含中文（`大肥鱼生命系统`）时
    # shim 里带中文 → UnicodeEncodeError（排练库路径纯 ASCII，直到落回主树才暴露）。
    # `.cmd` 由 Windows cmd 按 ANSI 读取，故用 mbcs（系统 ANSI/cp936）最兼容。
    shim_enc = "mbcs" if os.name == "nt" else "utf-8"
    with open(p["shim_path"], "w", encoding=shim_enc, newline="") as f:
        f.write(p["content"])
    p["written"] = True
    # 读**字节**再解码：`read_text` 会做通用换行转换（`\r\n` → `\n`），
    # 拿它跟带 `\r\n` 的原文比必然不等——校验器自己先错，就白写了。
    p["verified"] = p["shim_path"].read_bytes().decode(shim_enc, "replace") == p["content"]
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="install_cli.py",
        description="生成 fish 的 CLI shim（默认演练；不改 PATH、不写注册表）")
    ap.add_argument("--apply", action="store_true",
                    help="真的生成 fish.cmd；不加只打印计划（默认行为）")
    ap.add_argument("--bin-dir", default=None,
                    help=f"shim 放哪个目录（缺省 %USERPROFILE%\\.local\\bin）")
    ap.add_argument("--python", dest="python_exe", default=None,
                    help="用哪个 python 跑 fish.py（缺省=当前解释器）")
    args = ap.parse_args(argv)

    res = install(apply=args.apply, bin_dir=args.bin_dir, python_exe=args.python_exe)
    sys.stdout.write(render_plan(res, applied=res["applied"]))
    if not res["applied"]:
        return 0
    if not res["verified"]:
        # 写失败/写坏了必须非零退出：脚本的成功判据是"文件内容逐字正确"，不是"没抛异常"。
        sys.stderr.write(f"错误：shim 写入后校验不一致：{res['shim_path']}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
