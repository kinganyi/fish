#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W4.5（推送路由 + 三开关）判据的变异自证（113 §6.4 · 2026-11-25）。

## 为什么这一波更需要变异

W4.5 的全部承诺是"**默认值钉在今天的行为**，只多两列"。这类承诺最容易变成恒绿：
键登记了但没人读、闸门写了但永远不触发、`parent_session` 写了但其实恒 NULL——
三种都"看起来做了"。所以每条判据都先让它在坏输入下红一次（再确认还原后复绿）。

## 六个变异体

  M1  `push.no_identity` 默认值 `allow` → `skip`
      → J-P1 回放红（默认档挡掉了行）。**这就是"W4.7 之前不许切闸门"的可判形式。**
  M2  摘掉 `cmd_prompt_push` 里那句 early-return（闸门只写不接）
      → J-P3② 红（`skip` 档照样推）。
  M3  删掉 docstring 里"无身份"那句（文档再次与实现脱钩）
      → J-P3③ 红。
  M4  `push_target_of` 恒返回 `(None, None)`（列写了但从不填）
      → J-P2① 红（子会话推送不带父）。
  M5  hook 侧把 `strict_anonymous=True` 改成 `False`（allow 档偷偷放宽推送面）
      → J-P3（同源）红。
  M6  `_PUSH_LOG_COLS` 摘掉两列（表结构没真加）
      → §6.1 红（新库缺列 / 旧库补不出来）。

用法：`python -X utf8 mutation_push_routing.py [--only M4]`

⚠️ 本脚本会**临时改写**源码（逐字节还原），跑的时候别在同一个工作树上做别的编辑。
"""
import argparse
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
COMMON_PY = HERE / "fish_common.py"
FISH_PY = HERE / "fish.py"
HOOK_PY = HERE / "fish_push_hook.py"
SCHEMA_PY = HERE / "fish_schema.py"
TEST = "test_push_routing.py"

# (名字, 文件, -k 选择器, 靶点原文, 变异后, 打的是哪条判据)
MUTANTS = [
    ("M1", COMMON_PY, "test_JP1_default_allow_blocks_nothing",
     '    "push.no_identity": ("allow", "enum", ["allow", "skip"]),',
     '    "push.no_identity": ("skip", "enum", ["allow", "skip"]),',
     "J-P1：默认档回放 0 挡（默认值被改成 skip）"),
    ("M2", FISH_PY, "test_JP3_skip_blocks_with_zero_output",
     "        if push_gate_no_identity(conn, citizen_id):\n            return\n",
     "        if False:            # 变异：闸门只写不接\n            return\n",
     "J-P3②：skip 档零输出（闸门没接上）"),
    ("M3", FISH_PY, "test_JP3_docstring_and_implementation_agree",
     "`push.enabled`/`push.prompt_enabled` 关；**无身份且 `push.no_identity=skip`**",
     "`push.enabled`/`push.prompt_enabled` 关；**prompt 洗成空**",
     "J-P3③：docstring 与实现同源"),
    ("M4", FISH_PY, "test_JP2_child_push_lands_on_child_key_with_parent",
     '        if (target_mode or get_config(conn, "push.target_mode")) != "session" or host != "dsh":',
     '        if True:            # 变异：列写了但从不填\n            return None, None\n'
     '        if (target_mode or get_config(conn, "push.target_mode")) != "session" or host != "dsh":',
     "J-P2①：子会话推送带父"),
    ("M5", HOOK_PY, "test_JP3_hook_channel_keeps_its_own_strict_rule",
     "if fish.push_gate_no_identity(conn, citizen_id, strict_anonymous=True):",
     "if fish.push_gate_no_identity(conn, citizen_id, strict_anonymous=False):",
     "J-P3：hook 侧同源（allow 档不许放宽推送面）"),
    ("M6", SCHEMA_PY, "test_legacy_db_gets_columns_and_old_rows_stay_null",
     '    ("agent_id", "TEXT"),\n    ("parent_session", "TEXT"),\n',
     "",
     "§6.1：旧库自动补两列"),
]


def _run(k):
    """跑一个测试子集：返回 (是否绿, 末行摘要)。"""
    r = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", TEST, "-q",
                        "--no-header", "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:140]


def source_mutants(picked):
    fails = []
    for name, path, k, old, new, why in picked:
        print(f"  {name}  [{path.name} / -k {k}]")
        print(f"       打的是：{why}")
        green, tail = _run(k)
        if not green:
            print(f"       x 对照组就红了（判据本身有问题）：{tail}")
            fails.append(name)
            continue
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"       x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mut_green, mut_tail = _run(k)
            if mut_green:
                print("       x 变异后仍绿——这条判据没有判别力")
                fails.append(name)
            else:
                print(f"       ok 变红：{mut_tail}")
        finally:
            src.restore()
        if not (src.sha256() == before and src.restored()):
            print("       x 还原失败：工作树被变异体污染，立刻停机检查")
            fails.append(name)
            continue
        back_green, back_tail = _run(k)
        if back_green:
            print("       还原复绿")
        else:
            print(f"       x 还原后仍红：{back_tail}")
            fails.append(name)
    return fails


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑某个变异体（M1..M6）")
    a = ap.parse_args(argv)
    print("\n=== W4.5 变异自证（113 §6.4）===")
    picked = [m for m in MUTANTS if not a.only or m[0] == a.only]
    if not picked:
        print(f"  --only {a.only} 没有匹配的变异体（可选：{[m[0] for m in MUTANTS]}）")
        return 2
    fails = source_mutants(picked)
    print()
    if fails:
        print(f"  失败：{fails}")
        return 1
    print("  全部变异体通过（红→还原→绿 三步都成立）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
