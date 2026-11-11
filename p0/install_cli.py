#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""fish CLI 安装卡（FB-13）：把 `fish` 变成一条**能直接敲**的命令。

## 为什么需要它

外部评估 22-R4 / 21-P2 的共同结论：装好 MCP 的 AI 手上有**工具**、没有**命令**。
它想跑 `fish session start` 时得到的是
`'fish' 不是内部或外部命令`，然后**放弃并绕路**——绕路的具体形态，就是长期停在
`self-reported`（明明 CLI 能签 `machine` 级，却一直没用上）。

## 两层结构（2026-11-11 重构，**这是本文件最重要的设计决定**）

    <bin>/fish.cmd        ← 纯 ASCII，只有一行：@python "%~dp0fish_launch.py" %*
    <bin>/fish_launch.py  ← UTF-8，含真实目标路径，用 subprocess 转发

**为什么不是单文件 .cmd**（旧设计，已证伪）：

旧实现把绝对路径直接写进 `fish.cmd`，并按 `mbcs`(cp936) 编码保存，注释里写着
「`.cmd` 由 Windows cmd 按 ANSI 读取」。**这个假设在本机不成立**，实测：

    cp936 写出的 .cmd，内容是 `@echo 大肥鱼生命系统`
      → cmd 执行输出 `���������ϵͳ`      （乱码）
    utf-8  写出的 .cmd，内容相同
      → cmd 执行输出 `大肥鱼生命系统`    （正确）

而本机的 ANSI 与 OEM 代码页**都是 936**——也就是说，cmd 到底按哪个代码页读 .cmd，
**不是看 chcp 就能确定的**，它取决于机器/Windows 版本/终端配置。于是：

- 按 936 写 → 在"按 UTF-8 读"的机器上乱码；
- 按 UTF-8 写 → 在"按 936 读"的机器上乱码；
- **两条路都会在某一类机器上炸，而且炸得很隐蔽**：`fish --help` 报的是
  `can't open file '...\p0\fish.py'`，看起来像"文件不存在"，实际是路径被转码。

所以正解不是"选一个正确的代码页"，而是**让 `.cmd` 里一个非 ASCII 字符都没有**：
`.cmd` 只做一件事——用 `%~dp0`（它自己所在目录，位于用户级 `.local\bin`，纯 ASCII）
拼出启动器路径，然后交给 python；真实路径住在 `fish_launch.py` 里，
而 **Python 源码恒按 UTF-8 读**（PEP 3120），与代码页无关。

附带收益：`.cmd` 里连 python 的绝对路径都不再出现（用 PATH 上的 `python`），
所以**整个 shim 与项目放在哪个目录、python 装在哪里都无关**。

## 这个脚本做什么（以及**刻意不做什么**）

做：在 `%USERPROFILE%\.local\bin` 下生成上面两个文件，然后把「要加进 PATH 的目录」
和「怎么加」打印出来。

**不做**：不改 PATH、不写注册表、不复制 python、不建计划任务。
理由：改用户级 PATH 是**全局且难回滚**的动作（溢出 2047 字符会截断别人的路径），
而两个 shim 文件是**删掉就干净**的。所以把选择权留在人手上——脚本只负责正确的那一半。

## 默认演练

不加 `--apply` 时**一个文件都不写**，只打印计划（路径 + 内容 + PATH 命令）。
这和 `fish archive` / `fish project retire` 是同一条纪律：会改变系统形状的操作，
先给人看计划。

## 用法

    python install_cli.py                    # 演练：看要生成什么、PATH 加哪里
    python install_cli.py --apply            # 真生成两个文件
    python install_cli.py --apply --bin-dir D:\tools\bin   # 换个目录（测试用）

生成之后**新开一个终端**再敲 `fish --help`（PATH 只在新进程里生效）。
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FISH_PY = HERE / "fish.py"
SHIM_NAME = "fish.cmd"
LAUNCHER_NAME = "fish_launch.py"


def default_bin_dir() -> Path:
    """默认 shim 目录：`%USERPROFILE%\\.local\\bin`（用户级、无需管理员、删掉即干净）。

    Windows 用 `USERPROFILE`（**不用** `HOME`：本机 `HOME` 可能是被其它工具改写过的值），
    其它平台退到 `~/.local/bin`（XDG 惯例）。
    """
    home = (os.environ.get("USERPROFILE") or "").strip()
    base = Path(home) if home else Path.home()
    return base / ".local" / "bin"


def shim_content(launcher_name=LAUNCHER_NAME) -> str:
    """`fish.cmd` 的内容：**必须是纯 ASCII**，且不含任何绝对路径。

    `%~dp0` 是批处理内建变量，展开成"本 .cmd 所在目录（带尾随反斜杠）"。
    .cmd 与启动器同目录，所以拼出来永远是正确路径——不依赖项目位置，
    也不依赖 python 装在哪（用 PATH 上的 `python`）。

    结尾 `%*` 原样转发全部参数（`fish board checkin --doing "..."` 这种带空格的也活）。
    """
    return f'@python "%~dp0{launcher_name}" %*\r\n'


def launcher_content(target, python_exe=None) -> str:
    """`fish_launch.py` 的内容：真实路径住在这里，Python 按 UTF-8 读，与代码页无关。

    **没有转义问题**：用 `r"..."` 原始字符串，Windows 路径里的反斜杠原样保留。
    路径若含双引号会破坏字面量——那种路径在 Windows 上本来就不合法，故不处理。

    `INSTALLED_WITH` 只做**诊断**（"当初是用哪个解释器装的"），不参与执行：
    执行用 `sys.executable`，即实际运行本启动器的那个 python。这样 python 升级后
    shim 不需要重装。
    """
    py = str(Path(python_exe or sys.executable).resolve())
    tgt = str(Path(target).resolve())
    return f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish CLI 启动器（由 install_cli.py 生成，**不要手改**）。

为什么需要这一层：`.cmd` 里的非 ASCII 字符会因机器不同被 cmd 按不同代码页解码，
中文路径会变乱码、python 报 `can't open file`（看着像文件不存在，其实是转码）。
把 `.cmd` 限制成纯 ASCII、真实路径放进本文件，整类代码页问题就消失了。

重装（项目搬家/改名后）：`python <p0>/install_cli.py --apply`
"""
import os
import subprocess
import sys

# 真实目标（项目搬家后这里要跟着变——重跑 install_cli.py 即可）
TARGET = r"{tgt}"

# 仅诊断用：安装时用的是哪个解释器。执行时用 sys.executable。
INSTALLED_WITH = r"{py}"

_MIN_PY = (3, 8)      # fish.py 只用 stdlib，3.8+ 足够；太低给明确提示而不是莫名其妙的语法错


def main(argv):
    if sys.version_info < _MIN_PY:
        sys.stderr.write(
            "fish 需要 Python >= %d.%d（当前 %s）\\n"
            % (_MIN_PY[0], _MIN_PY[1], sys.version.split()[0]))
        return 2
    if not os.path.exists(TARGET):
        sys.stderr.write(
            "fish 启动器找不到目标：%s\\n"
            "（项目被移动/改名了？重跑 install_cli.py --apply 生成新启动器）\\n" % TARGET)
        return 2
    # stdio / 环境变量原样继承：退出码、中文输出、管道行为都与直接调 fish.py 一致。
    return subprocess.call([sys.executable, TARGET] + list(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def plan(bin_dir=None, python_exe=None, fish_py=None) -> dict:
    """算出"要生成什么、加哪个目录进 PATH"，**不落任何文件**。"""
    target_dir = Path(bin_dir) if bin_dir else default_bin_dir()
    script = Path(fish_py or FISH_PY).resolve()
    py = Path(python_exe or sys.executable).resolve()
    shim = target_dir / SHIM_NAME
    launcher = target_dir / LAUNCHER_NAME
    cmd_text = shim_content()
    py_text = launcher_content(script, py)
    # PATH 上的 `python` 用来执行 .cmd；与安装用的解释器不同只提示，不阻断
    # （fish.py 纯 stdlib，任何 3.8+ 都能跑）。
    on_path = shutil.which("python") or shutil.which("python3")
    return {
        "bin_dir": target_dir,
        "shim_path": shim,
        "launcher_path": launcher,
        "content": cmd_text,
        "launcher_content": py_text,
        "python": py,
        "python_on_path": Path(on_path) if on_path else None,
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
        f"  fish.py    {p['fish_py']}",
        f"  shim       {p['shim_path']}",
        f"    内容     {p['content'].strip()}",
        "             （纯 ASCII，不含任何绝对路径——所以不受代码页/项目位置影响）",
        f"  启动器     {p['launcher_path']}",
        f"    目标     {p['fish_py']}",
        f"  python     {p['python']}"
        + ("" if p.get("python_on_path") else "  ⚠ 但 PATH 上找不到 `python`（.cmd 需要它）"),
        "",
        "需要把这个目录加进用户 PATH（**本脚本不改 PATH、不写注册表**）：",
        f"  {p['path_dir']}",
        f"  仅当前窗口： {p['path_session_cmd']}",
        f"  永久（用户级）：{p['path_persist_cmd']}",
        "  注意：setx 会把当前值整体覆盖——先 `echo %PATH%` 确认原有内容，",
        "        再决定是追加还是干脆只用\"仅当前窗口\"那条（重启后失效但零风险）。",
    ]
    if applied:
        lines += ["", "完成：新开一个终端敲 `fish --help` 验证（PATH 只在新进程里生效）。"]
    else:
        lines += ["", "演练完毕：没有写任何文件。确认后加 --apply 生成 shim。"]
    return "\n".join(lines) + "\n"


def install(apply=False, bin_dir=None, python_exe=None, fish_py=None) -> dict:
    """生成 shim + 启动器（`apply=True` 才写盘）。返回 `{...plan, applied, written, verified}`。

    写后**读回来逐字比对**：写成功但内容不对（编码/BOM/换行）时，"verify 一下"
    是唯一能当场发现的办法——而 shim 坏了的症状是"命令找得到、一跑就报错"，
    比没有 shim 更难查。

    两个文件都按 **UTF-8（无 BOM）** 写、`.cmd` 用显式 `\\r\\n`：
    - `.cmd` 现在是纯 ASCII，所以 UTF-8/936/65001 三种读法结果**完全一致**——
      这正是这次重构要的那个性质，不必再猜 cmd 用哪个代码页。
    - `.py` 由 Python 按 UTF-8 读（PEP 3120），BOM 会让 `# -*- coding:` 行前多字符，
      所以写无 BOM。
    """
    p = plan(bin_dir=bin_dir, python_exe=python_exe, fish_py=fish_py)
    p.update({"applied": bool(apply), "written": False, "verified": False})
    if not apply:
        return p
    p["bin_dir"].mkdir(parents=True, exist_ok=True)
    for path, text in ((p["shim_path"], p["content"]),
                       (p["launcher_path"], p["launcher_content"])):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    p["written"] = True
    # 读**字节**再解码：`read_text` 会做通用换行转换（`\r\n` → `\n`），
    # 拿它跟带 `\r\n` 的原文比必然不等——校验器自己先错，就白写了。
    p["verified"] = (
        p["shim_path"].read_bytes().decode("utf-8", "replace") == p["content"]
        and p["launcher_path"].read_bytes().decode("utf-8", "replace") == p["launcher_content"]
    )
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="install_cli.py",
        description="生成 fish 的 CLI shim（默认演练；不改 PATH、不写注册表）")
    ap.add_argument("--apply", action="store_true",
                    help="真的生成 fish.cmd + fish_launch.py；不加只打印计划（默认行为）")
    ap.add_argument("--bin-dir", default=None,
                    help=f"shim 放哪个目录（缺省 %USERPROFILE%\\.local\\bin）")
    ap.add_argument("--python", dest="python_exe", default=None,
                    help="记录用哪个解释器安装（执行用 PATH 上的 python）")
    args = ap.parse_args(argv)

    res = install(apply=args.apply, bin_dir=args.bin_dir, python_exe=args.python_exe)
    sys.stdout.write(render_plan(res, applied=res["applied"]))
    if not res["applied"]:
        return 0
    if not res["verified"]:
        # 写失败/写坏了必须非零退出：脚本的成功判据是"文件内容逐字正确"，不是"没抛异常"。
        sys.stderr.write(f"错误：shim/启动器写入后校验不一致：{res['bin_dir']}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
