#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 评审 Round 5（N1）：改真源码 → 目标判据必红 → 字节还原。

N1 的修复点是**删掉**"锚定正则"这一档：它只查**字符集**、不查**可编译性**。
`^(Write|write|Edit|edit$` 与 `^(Write|write|Edit|edit))$` 的字符集完全合法，
但宿主 parser 会 `SyntaxError` → 整份配置被拒（0 事件注册），而旧 `--check` 照样绿。

本变异把它原样加回去（`^[A-Za-z0-9_|()]+$` 形态重新判合法），用来证明新判据真的
测到了修复点，而不是"第一次跑就是绿的、其实什么都没测"：

  基线必须绿 → 加回锚定档后 `test_matcher_dialect_contract` 与
  `test_malformed_anchored_matcher_makes_check_red` 必须红 →
  按 sha256 字节还原 → 复跑必须回到绿。

用法：`python mutation_review_r5.py`（无参数；日志写 `_audit/变异自证-N1.log`）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from mutation_review_r3 import AUDIT, run_tests, sha256, verdict_text

HERE = Path(__file__).resolve().parent

# (编号, 日志名, 说明, [(文件, 旧片段, 新片段)], [要跑的判据])
MUTATIONS = [
    (
        "M-N1",
        "变异自证-N1.log",
        "把 Round 4 的「锚定正则档」原样加回（只查字符集 → 括号不平衡也判合法）",
        [(
            "install_hooks.py",
            '    if LITERAL_ALTERNATIVES.fullmatch(raw):\n'
            '        return "literal", ""\n',
            '    if LITERAL_ALTERNATIVES.fullmatch(raw):\n'
            '        return "literal", ""\n'
            '    if re.fullmatch(r"\\^(?:[A-Za-z0-9_|()])+\\$", raw):\n'
            '        return "anchored-regex", ""      # 变异：恢复 Round 4 的锚定档\n',
        )],
        [
            "test_surface_naming.InstallerTest.test_matcher_dialect_contract",
            "test_surface_naming.InstallerTest"
            ".test_malformed_anchored_matcher_makes_check_red",
        ],
    ),
]

EXPECT = {
    "M-N1": "N1：锚定档已取消——一切正则形态（含括号不平衡的锚定串）必须判非法；"
            "`--check` 必须 exit 2",
}


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    failures = []
    for mid, log_name, desc, patches, tests in MUTATIONS:
        log_path = AUDIT / log_name
        files = sorted({name for name, _old, _new in patches})
        backups = {name: HERE / (name + ".r5mut-bak") for name in files}
        sums = {name: sha256(HERE / name) for name in files}
        verdict = "未执行"
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"# {mid} · {desc}\n")
            log.write(f"# 期望：{EXPECT[mid]} 这几条判据**变红**\n")
            log.write("# 基线 sha256: " + ", ".join(
                f"{name}={sums[name][:16]}…" for name in files) + "\n\n")
            for name in files:
                shutil.copy2(HERE / name, backups[name])
            try:
                log.write("== ① 基线（未变异）应全绿 ==\n")
                rc_base = run_tests(tests, log)
                log.write(f"\n基线退出码 = {rc_base}（0 = 全绿）\n\n")
                if rc_base != 0:
                    failures.append((mid, "基线不是全绿，判据本身有问题"))
                    verdict = "基线不绿 → 不作数"
                    log.write("结论：基线不绿 → 本次变异**不作数**\n")
                    continue
                applied = True
                for name, old, new in patches:
                    src = (HERE / name).read_text(encoding="utf-8")
                    if old not in src:
                        failures.append((mid, f"{name} 里找不到待替换片段"))
                        log.write("结论：找不到待替换片段 → 变异**未施加**\n")
                        applied = False
                        break
                    (HERE / name).write_text(src.replace(old, new, 1), encoding="utf-8")
                if not applied:
                    verdict = "变异未施加"
                    continue
                log.write("== ② 变异后（必须红）==\n")
                rc_mut = run_tests(tests, log)
                log.write(f"\n变异退出码 = {rc_mut}（非 0 = 判据红，符合预期）\n\n")
                if rc_mut == 0:
                    failures.append((mid, "变异后判据仍然绿——判据没测到修复点"))
                for name in files:
                    shutil.copy2(backups[name], HERE / name)
                restored = all(sha256(HERE / name) == sums[name] for name in files)
                log.write("== ③ 还原 ==\n")
                log.write("sha256 与基线" + ("一致 ✓" if restored else "**不一致** ✗") + "\n\n")
                if not restored:
                    failures.append((mid, "还原失败（源码可能残留变异）"))
                    verdict = "还原失败"
                    continue
                log.write("== ④ 还原后再跑（应回到全绿）==\n")
                rc_back = run_tests(tests, log)
                log.write(f"\n复跑退出码 = {rc_back}（0 = 回到绿）\n")
                if rc_back != 0:
                    failures.append((mid, "还原后判据仍红——源码未干净恢复"))
                verdict = ("红（符合预期）" if rc_mut != 0 else "绿（变异没生效！）")
                log.write(f"\n结论：{verdict}\n")
            finally:
                for name in files:
                    if sha256(HERE / name) != sums[name]:
                        shutil.copy2(backups[name], HERE / name)
                    backups[name].unlink(missing_ok=True)
        print(f"{mid}: {verdict}  → _audit/{log_path.name}")
    if failures:
        print("\n有未通过项：")
        for mid, why in failures:
            print(f"  {mid}: {why}")
        return 1
    print("1 条变异自证成立（基线绿 → 变异红 → 还原字节一致 → 复跑绿）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
