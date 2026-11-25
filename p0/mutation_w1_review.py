#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W1 审查修复轮的变异自证（2026-11-25 · 外部审查 F-P1-3 / F-P1-4 / F-P1-5）。

## 为什么要有这个脚本

三条修复都是"**删掉那段就回到缺陷**"的形态，所以判据的价值只能靠变异证明：

    M1  删掉 situational 查询的可见性谓词      → 推送候选泄露 private 标题
    M2  删掉 KB 敏感面的 citizen kind 判定      → visitor 自授权读/写凭证文件
    M3  删掉校验失败后的回滚                     → 源目录搬空、文件留在目标侧
    M4  删掉 dst ⊂ src 的拒绝                    → 库搬进自己的子目录（清单边搬边长）

每个变异体的流程与 `mutation_wave4.py` 一致：
  ① 先跑**未变异**的对照组，绿才继续（否则"变异后红"可能只是判据本来就红）；
  ② 改源码 → 必须红；
  ③ 逐字节还原 → 必须复绿。
任一步不成立就非零退出。

用法：
    python mutation_w1_review.py            # 全部
    python mutation_w1_review.py --only M3  # 单个
"""
import argparse
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from pathlib import Path

from mutation_source import SourceText

HERE = Path(__file__).resolve().parent
PUSH = HERE / "fish_push.py"
MCP = HERE / "mcp_server.py"
KB = HERE / "fish_kb.py"

VIS = "test_visibility_guard.py"
SENS = "test_kb_sensitive_kind.py"
RELOC = "test_relocate_atomicity.py"


def _run(test_file, k):
    r = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q", "--no-header",
                        "-p", "no:cacheprovider", "-k", k],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    tail = [x for x in out.strip().split("\n") if x.strip()][-1:] or ["(无输出)"]
    return r.returncode == 0, tail[0].strip()[:120]


MUTANTS = [
    # ---------------- F-P1-3：推送候选的可见性
    ("M1", PUSH, VIS, "push_candidates_visibility",
     '+ _vs +', '+ "" +'),
    # ---------------- F-P1-4：KB 敏感面自授权
    ("M2", MCP, SENS, "visitor_cannot",
     '        if str(kind or "").strip().lower() in fish_trust.AUTHORIZED_CITIZEN_KINDS:\n',
     '        if True:      # 变异：去掉 kind 判定\n'),
    # ---------------- F-P1-5：校验失败回滚
    # 旧缺陷的准确形状 = "校验失败不进回滚"：把 except 收窄成 OSError，
    # 校验抛的 KBError 就绕过了回滚 → 源空、目标有、note 卡 moving。
    ("M3", KB, RELOC, "verify_failure",
     "        except BaseException as exc:\n",
     "        except OSError as exc:      # 变异：校验失败不再走回滚\n"),
    # 再退一步：回滚循环本身被删空（"删回滚 → 源空目标有 → 红"）
    ("M5", KB, RELOC, "verify_failure",
     "            for rel in reversed(moved_rels):\n",
     "            for rel in []:      # 变异：回滚循环被删空\n"),
    # ---------------- F-P1-5：拒绝 dst ⊂ src
    ("M4", KB, RELOC, "dst_inside_src",
     "    if _inside:\n",
     "    if False and _inside:      # 变异：删掉拒绝\n"),
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    a = ap.parse_args(argv)
    picked = [m for m in MUTANTS if not a.only or m[0] == a.only]
    fails = []
    for name, path, test_file, k, old, new in picked:
        ok, tail = _run(test_file, k)
        if not ok:
            print(f"  {name}  x 对照组红（先修判据再谈变异）：{tail}")
            fails.append(name)
            continue
        print(f"  {name}  对照组绿：{tail}")
        src = SourceText(path)
        before = src.sha256()
        try:
            try:
                src.write(src.replaced(old, new))
            except AssertionError as exc:
                print(f"  {name}  x 靶点未命中：{exc}")
                fails.append(name)
                continue
            mok, mtail = _run(test_file, k)
            if mok:
                print(f"  {name}  x 变异后仍绿（判据没有判别力）")
                fails.append(name)
            else:
                print(f"  {name}  ok 变异后变红：{mtail}")
        finally:
            src.restore()
        if src.sha256() != before or not src.restored():
            print(f"  {name}  x 还原失败（工作树被改脏了，立刻手工查）")
            fails.append(name)
            continue
        pok, ptail = _run(test_file, k)
        if not pok:
            print(f"  {name}  x 还原后仍红：{ptail}")
            fails.append(name)
        else:
            print(f"  {name}    还原后复绿：{ptail}")
    print()
    print("  全部 %d 个变异体通过" % len(picked) if not fails else "  失败：%s" % fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
