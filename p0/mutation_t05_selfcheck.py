#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证执行器（T05 · MCP 工具面 · 2026-10-26）。

与 `mutation_b2b3_selfcheck.py` 同构，只是被作用的文件换成 `mcp_server.py`
（T05 的全部逻辑都在那里）与它的端到端判据 `test_mcp_kb.py`。

为什么这一批必须单独有一个执行器：`mcp_server.py` 的判据只在**进程边界**上成立
（工具表、JSON-RPC 分发、错误→isError）。在同一个进程里改个常量、断言一下，
证明不了"宿主调这个工具时会红"——所以每条变异都起真子进程跑真测试。

四条纪律：
  ① **只改临时副本**：`p0/mcp_server.py` 一个字节不动（`_copy_workdir` 复制到 mkdtemp）；
  ② 变异前提必须成立：`old` 串找不到 → 直接报"前提不成立"，不当成绿；
  ③ 对照组：未变异的副本跑完整 `test_mcp_kb` 必须绿；
  ④ 期望的是**具体用例名 + 具体失败文案**，不是"退出码非零"——否则任何环境噪声都能冒充"红了"。

用法：`python mutation_t05_selfcheck.py [-v]`，Exit 0 = 全部符合预期。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST = "test_mcp_kb.MCPKBToolsTest"

# (编号, 说明, 被改文件, old, new, 单测目标, 期望在输出里出现的失败证据)
MUTATIONS = [
    ("M-T05-1", "给 `kb` 的 schema 手抄一份 token 定义 → import 期就该炸",
     "mcp_server.py",
     '                "action": {"type": "string",\n'
     '                           "enum": ["vaults", "tree", "open", "search"]},\n',
     '                "action": {"type": "string",\n'
     '                           "enum": ["vaults", "tree", "open", "search"]},\n'
     '                "token": {"type": "string"},\n',
     f"{TEST}.test_03_constants_match",
     "token 只允许由 _attach_token 统一补"),

    ("M-T05-2", "`call_tool` 的分发表漏写 kb_write → 工具面有名字但打不通",
     "mcp_server.py",
     '            "kb": self.tool_kb,\n'
     '            "kb_write": self.tool_kb_write,\n',
     '            "kb": self.tool_kb,\n',
     f"{TEST}.test_11_kb_write_with_token_creates_backup_and_is_attributed",
     "未知工具"),

    ("M-T05-3", "回执头的来源声明被换掉 → `source=` 那行不再是固定常量",
     "mcp_server.py",
     'KB_SOURCE_LINE = "source=知识库（未建索引）"',
     'KB_SOURCE_LINE = "source=（未标注）"',
     f"{TEST}.test_04_kb_tree_three_line_header",
     "第 1 行必须是来源声明"),

    ("M-T05-4", "`tool_kb` 丢掉 ident → 带凭证读也被记成 unattributed",
     "mcp_server.py",
     "        citizen = ident.citizen_id          # None → kb_log_access 里映射成 'unattributed'",
     "        citizen = None                      # 变异：丢弃归属",
     f"{TEST}.test_13_write_then_read_with_token_is_attributed",
     "unattributed"),

    ("M-T05-5", "`kb_write` 回执把正文回显出来 → §5.6 的'不打印正文'破了",
     "mcp_server.py",
     '        lines.append(f"（write_id={res.get(\'write_id\')}｜via=mcp:kb_write"',
     '        lines.append(str(content))                     # 变异：回显正文\n'
     '        lines.append(f"（write_id={res.get(\'write_id\')}｜via=mcp:kb_write"',
     f"{TEST}.test_11_kb_write_with_token_creates_backup_and_is_attributed",
     "回执里不该回显正文"),

    # ---------------------------------------------------------------- 第二轮（R11）
    ("M-R11", "删掉 kb_write 的凭证门槛 → 无 token 也能写知识库（安全 F3 回归）",
     "mcp_server.py",
     '        if ident.status != IDENTITY_VALID or not ident.citizen_id:\n'
     '            raise ValueError(\n'
     '                "写知识库需要会话凭证（审计要记\'是谁、哪个会话改的\'）。"\n'
     '                "先调 `session_start` 拿 token，这次调用带上 token=... 再 write。")\n',
     '        pass\n',
     f"{TEST}.test_11b_kb_write_without_token_is_rejected",
     "无 token 的 kb_write 被放行了"),
]

COPY_FILES = ("fish.py", "mcp_server.py", "test_mcp.py", "test_mcp_kb.py")


def _copy_workdir(tmp: Path) -> Path:
    """把 p0 下全部 `*.py` 复制到临时目录（**不碰真实 p0/**）。

    2026-10-26 合并后修复：原来只复制 `COPY_FILES` 白名单；合并后 `fish.py`
    依赖了更多模块（`fish_activity`/`fish_surface`…），副本不自足 → 对照组
    `ImportError`（被误判成"环境有问题"）。改为复制全部 `.py`，让副本自足。
    """
    work = tmp / "work"
    work.mkdir(parents=True, exist_ok=True)
    for f in HERE.glob("*.py"):
        shutil.copy2(f, work / f.name)
    return work


def _run(work: Path, target: str) -> subprocess.CompletedProcess:
    # 子进程统一 UTF-8（R9 · QA F-QA-6）：不设时子进程按 cp936 写中文，父进程按 utf-8 解
    # → `expect in output` 永远 False（"变异假红"）。父脚本自己的 stdout 也已在 main()
    # 里 reconfigure，所以两层都不再依赖调用方环境变量。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "unittest", target, "-v"],
        cwd=str(work), capture_output=True, text=True, env=env,
        encoding="utf-8", errors="replace")


def main(argv) -> int:
    # R9（QA F-QA-6）：cp936 控制台下 ✓/✗ 会触发 UnicodeEncodeError（看起来像"变异假红"）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    verbose = "-v" in argv
    tmp = Path(tempfile.mkdtemp(prefix="fish-mutation-t05-"))
    bad = 0
    try:
        # 对照组：未变异的副本，完整 test_mcp_kb 必须绿
        control = _run(_copy_workdir(tmp / "control"),
                       "test_mcp_kb")
        passed = control.returncode == 0
        print(f"[对照] test_mcp_kb: {'OK（绿）' if passed else '红——环境有问题，变异结果不可信'}")
        if not passed:
            print(control.stdout[-2000:], control.stderr[-2000:])
            return 1

        for mid, desc, filename, old, new, target, expect in MUTATIONS:
            work = _copy_workdir(tmp / mid)
            src = (work / filename).read_text(encoding="utf-8")
            if old not in src:
                print(f"✗ {mid} 前提不成立：源码里找不到要被替换的片段")
                bad += 1
                continue
            (work / filename).write_text(src.replace(old, new, 1), encoding="utf-8")
            proc = _run(work, target)
            output = proc.stdout + proc.stderr
            red = proc.returncode != 0 and expect in output
            mark = "✓" if red else "✗"
            print(f"{mark} {mid} {target.rsplit('.', 1)[-1]} —— {desc}")
            if not red:
                bad += 1
                if verbose:
                    print(output[-3000:], proc.stderr[-2000:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"共 {len(MUTATIONS)} 条变异：{'全部按预期变红' if not bad else f'{bad} 条不符合预期'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
