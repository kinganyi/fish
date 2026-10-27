#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 评审 Round 4（F1 / F2 / F4 / F5）：改真源码 → 目标判据必红 → 字节还原。

与 `mutation_review_r3.py` 同一套纪律（基线必须先绿 → 施加变异 → 必须红 →
按 sha256 字节还原 → 复跑必须回到绿），只换变异清单。两条 in-test 自证
（`test_board_readable` 的展示列变异、`test_prompt_push` 的窗口变异）是补充，
这里做的是**源码级**的，两者互补。

  M-F1a `PRETOOL_MATCHER` 改回 `(?i)^(…) $`（评审 F1 的肇事串）→ 生成侧判据红
  M-F1b 把「纯字面备选」判据放宽成 `^.*$`（= 什么都合法，旧版口径）
        → 契约判据 + `--check` 红判据都必须红
  M-F2a 看板「观察到的重叠」实际面改回展示列 `dir` → 跨项目同名目录必误报
  M-F2b `_surfaces_touch` 退回"无门槛 surface_hit"（拿展示串判定）→ 假
        「声明≠实际」判据必红
  M-F4  `activity_overlap` 的目标退回「只看对方目录」→ "同一文件被两人改"必漏报
  M-F5  `_declared_row_fresh` 恒 True（声明面不看时间窗）→ 窗口判据必红

用法：`python mutation_review_r4.py`（无参数；逐条打印，日志写进 `_audit/`）
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
        "M-F1a",
        "变异自证-F1a.log",
        "生成侧 matcher 改回 `(?i)^(write|edit|…)$`（宿主 JS RegExp 会 Invalid group）",
        [(
            "install_hooks.py",
            'PRETOOL_MATCHER = "|".join(\n'
            '    name for pair in WRITE_TOOL_CASED for name in (pair[1], pair[0]))',
            'PRETOOL_MATCHER = r"(?i)^(write|edit|multiedit|notebookedit|apply_patch)$"',
        )],
        [
            "test_surface_naming.InstallerTest.test_generated_matcher_is_literal_form",
            "test_surface_naming.InstallerTest.test_two_hosts_four_events_and_one_collector",
        ],
    ),
    (
        "M-F1b",
        "变异自证-F1b.log",
        "把「纯字面备选」判据放宽成 `^.*$`（= 任何串都算合法，旧版的口径）",
        [(
            "install_hooks.py",
            'LITERAL_ALTERNATIVES = re.compile(r"^[A-Za-z0-9_]+(?:\\|[A-Za-z0-9_]+)*$")',
            'LITERAL_ALTERNATIVES = re.compile(r"^.*$")      # 变异：什么都合法',
        )],
        [
            "test_surface_naming.InstallerTest.test_matcher_dialect_contract",
            "test_surface_naming.InstallerTest.test_illegal_matcher_makes_check_red",
        ],
    ),
    (
        "M-F2a",
        "变异自证-F2a.log",
        "看板「观察到的重叠」实际面改回展示列 `dir`（评审 F2 的原始病）",
        [(
            "fish.py",
            '        actual = [d["dir_abs"] for d in (p.get("auto_dirs") or []) if d.get("dir_abs")]',
            '        actual = [d["dir"] for d in (p.get("auto_dirs") or []) if d.get("dir")]',
        )],
        [
            "test_board_readable.ObservedOverlapJudgeTest"
            ".test_cross_project_same_named_dir_no_overlap",
        ],
    ),
    (
        "M-F2b",
        "变异自证-F2b.log",
        "`_surfaces_touch` 退回无门槛 `surface_hit`（拿展示串判「声明≠实际」）",
        [(
            "fish.py",
            '    acts = [fish_surface.normalize_path(a) for a in (actual or ())]\n'
            '    acts = [a for a in acts if a and fish_surface.is_absolute(a)]\n'
            '    if not decl or not acts:\n'
            '        return True              # 有一侧没有可判定路径 → 不下结论\n'
            '    for a in decl:\n'
            '        for b in acts:\n'
            '            if fish_surface.path_hit(a, b, min_segments=min_segments):\n'
            '                return True\n'
            '    return False',
            '    acts = [fish_surface.normalize_path(a) for a in (actual or ())]\n'
            '    acts = [a for a in acts if a]\n'
            '    for a in declared or ():\n'
            '        for b in acts:\n'
            '            if surface_hit(fish_surface.normalize_path(a), b):\n'
            '                return True\n'
            '    return False',
        )],
        [
            "test_board_readable.SurfacesTouchJudgeTest.test_display_string_is_not_judged",
        ],
    ),
    (
        "M-F4",
        "变异自证-F4.log",
        "`activity_overlap` 的目标退回「只看对方目录」（= 评审 F4 的单向漏报）",
        [(
            "fish.py",
            '        for item in info.get("files") or []:\n'
            '            file_abs = item.get("path") or ""\n'
            '            if not file_abs:\n'
            '                continue\n'
            '            if fish_activity.is_noise_dir(fish_surface.dir_of(file_abs) or ""):\n'
            '                continue                     # 临时/缓存目录里的文件同样不判\n'
            '            targets.append((file_abs, file_abs, int(item.get("count") or 0)))\n',
            '        # （变异：文件面被摘掉——只剩对方的目录键）\n',
        )],
        [
            "test_prompt_push.ActivityOverlapTest"
            ".test_declared_file_face_reported_when_other_writes_same_file",
        ],
    ),
    (
        "M-F5",
        "变异自证-F5.log",
        "`_declared_row_fresh` 恒 True（声明面不看时间窗，评审 F5 的原始病）",
        [(
            "fish.py",
            '    window = int(window_min or 0)\n'
            '    if window <= 0:\n'
            '        return True                      # 0 = 不限窗口（沿用 0=永不的约定）',
            '    return True                              # 变异：声明面永远算有效\n'
            '    window = int(window_min or 0)\n'
            '    if window <= 0:\n'
            '        return True                      # 0 = 不限窗口（沿用 0=永不的约定）',
        )],
        [
            "test_prompt_push.ActivityOverlapTest.test_declared_row_outside_window_not_used",
        ],
    ),
]

EXPECT = {
    "M-F1a": "生成侧只用**字面备选**——宿主不编译正则（F1 的修复本身）",
    "M-F1b": "非法形态（内联 flag）必须判非法；`--check` 必须 exit 2 并打印原因",
    "M-F2a": "跨项目同名展示目录不得报「推断重叠」（判定列 `dir_abs`）",
    "M-F2b": "绝对声明 vs 相对展示串不得产生假「声明≠实际」",
    "M-F4": "声明文件面 + 对方写同一文件必须报（目标是目录 ∪ 文件两种形态）",
    "M-F5": "声明行超出 `board.overlap_window_min` 后不参与判定（与 find_collisions 同口径）",
}


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    failures = []
    for mid, log_name, desc, patches, tests in MUTATIONS:
        log_path = AUDIT / log_name
        files = sorted({name for name, _old, _new in patches})
        backups = {name: HERE / (name + ".r4mut-bak") for name in files}
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
    print(f"\n{len(MUTATIONS)} 条变异自证全部成立"
          "（基线绿 → 变异红 → 还原字节一致 → 复跑绿）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
