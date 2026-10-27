#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 评审 Round 3（L1 / L2 / L5）：改真源码 → 目标判据必红 → 字节还原。

为什么每条修复都要有变异：新判据**第一次跑就是绿的**可能只是"它压根没测到东西"。
唯一可靠的证明是——把修复点改回旧行为，同一组断言必须变红；再把源码还原，
必须回到绿（`test_activity`/`test_surface_naming`/`test_prompt_push` 里另有
in-test 的 monkeypatch 版自证，这里做的是**源码级**的，两者互补）。

  M-L1  快照 `dir_abs` 改回「相对显示串 + 看板项目根反推」→ L1 两条判据红
  M-L2  摘掉 `path_hit` 的「相对目录面按分量链包含」→ L2 正例红
  M-L5  `activity_overlap` 的门槛降成 1（等于绕过 `min_segments`）→ 「单段不报」红

用法：`python mutation_review_r3.py`（不需要参数；逐条打印，日志写进 `_audit/`）
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE / "_audit"

# (编号, 日志名, 目标消息, [(文件, 旧片段, 新片段)], [要跑的判据])
MUTATIONS = [
    (
        "M-L1",
        "变异自证-L1.log",
        "快照 auto.dir_abs 改回「相对显示串 + 看板项目根反推」（旧实现）",
        [(
            "fish_activity.py",
            '            dir_abs = item.get("dir_abs") or ""\n'
            '            if not fish_surface.is_absolute(dir_abs):\n',
            '            dir_abs = (fish_surface.make_abs(item.get("dir") or "", base)\n'
            '                       if base else (item.get("dir") or ""))\n'
            '            if not dir_abs:\n',
        )],
        [
            "test_activity.SnapshotTest.test_auto_dir_abs_from_fact_when_no_project",
            "test_activity.SnapshotTest.test_auto_dir_abs_not_rebased_to_board_project",
        ],
    ),
    (
        "M-L2",
        "变异自证-L2.log",
        "摘掉 path_hit 的「相对目录面按分量链包含」这一步（评审 L2 的修复点）",
        [(
            "fish_surface.py",
            "    if not is_absolute(a) and not looks_like_file(a) and len(sa) >= floor:\n"
            "        if _contains_run(sb, sa):\n"
            "            return a\n",
            "    # （变异：这一整步被摘掉——相对目录面只走后缀匹配）\n",
        )],
        [
            "test_surface_naming.PathHitTest.test_relative_directory_hits_deeper_target",
        ],
    ),
    (
        "M-L5",
        "变异自证-L5.log",
        "activity_overlap 的 path_hit 门槛降成 1（等价于绕过 min_segments）",
        [(
            "fish.py",
            "fish_surface.path_hit(one, target_abs, min_segments=min_segments)",
            "fish_surface.path_hit(one, target_abs, min_segments=1)",
        )],
        [
            "test_prompt_push.ActivityOverlapTest"
            ".test_single_segment_dir_not_reported_across_projects",
        ],
    ),
]

# 打印用：判据 ↔ 修复 的对照（读日志的人不必翻代码）
EXPECT = {
    "M-L1": "L1 ①②：dir_abs 必须绝对、必须以事实列 path_abs 的目录为准",
    "M-L2": "L2 正例：p0/data 命中 …/p0/data/x.db",
    "M-L5": "L5：跨项目同名单段目录（p0）不报撞车",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests(test_ids, log) -> int:
    """跑指定判据，返回子进程退出码（0=全绿，非 0=有红）。"""
    args = [sys.executable, "-m", "unittest", "-v"] + list(test_ids)
    # 编码：子进程的输出被重定向进管道时默认用**本地代码页**（GBK），
    # 而日志文件是 UTF-8 —— 不钉死就会把证据写成乱码（2026-10-26 实测踩到）。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    # cwd 必须是 p0：模块级的 DATA/SNAPSHOT_PATH 都按本目录算
    proc = subprocess.run(args, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    log.write(proc.stdout or "")
    log.write(proc.stderr or "")
    log.flush()
    return proc.returncode


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    failures = []
    for mid, log_name, desc, patches, tests in MUTATIONS:
        log_path = AUDIT / log_name
        files = sorted({name for name, _old, _new in patches})
        backups = {name: HERE / (name + ".r3mut-bak") for name in files}
        sums = {name: sha256(HERE / name) for name in files}
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"# {mid} · {desc}\n")
            log.write(f"# 期望：{EXPECT[mid]} 这几条判据**变红**\n")
            log.write(f"# 基线 sha256: " + ", ".join(
                f"{name}={sums[name][:16]}…" for name in files) + "\n\n")
            for name in files:
                shutil.copy2(HERE / name, backups[name])
            try:
                # ① 先证明基线是绿的（否则"变异后红"毫无意义）
                log.write("== ① 基线（未变异）应全绿 ==\n")
                rc_base = run_tests(tests, log)
                log.write(f"\n基线退出码 = {rc_base}（0 = 全绿）\n\n")
                if rc_base != 0:
                    failures.append((mid, "基线不是全绿，判据本身有问题"))
                    log.write("结论：基线不绿 → 本次变异**不作数**\n")
                    continue
                # ② 施加变异
                for name, old, new in patches:
                    src = (HERE / name).read_text(encoding="utf-8")
                    if old not in src:
                        failures.append((mid, f"{name} 里找不到待替换片段"))
                        log.write("结论：找不到待替换片段 → 变异**未施加**\n")
                        continue
                    (HERE / name).write_text(src.replace(old, new, 1), encoding="utf-8")
                log.write("== ② 变异后（必须红）==\n")
                rc_mut = run_tests(tests, log)
                log.write(f"\n变异退出码 = {rc_mut}（非 0 = 判据红，符合预期）\n\n")
                if rc_mut == 0:
                    failures.append((mid, "变异后判据仍然绿——判据没测到修复点"))
                # ③ 还原
                for name in files:
                    shutil.copy2(backups[name], HERE / name)
                restored = all(sha256(HERE / name) == sums[name] for name in files)
                log.write("== ③ 还原 ==\n")
                log.write("sha256 与基线" + ("一致 ✓" if restored else "**不一致** ✗") + "\n\n")
                if not restored:
                    failures.append((mid, "还原失败（源码可能残留变异）"))
                    log.write("结论：还原失败 → 请手工检查源码\n")
                    continue
                # ④ 还原后复跑必须回到绿
                log.write("== ④ 还原后再跑（应回到全绿）==\n")
                rc_back = run_tests(tests, log)
                log.write(f"\n复跑退出码 = {rc_back}（0 = 回到绿）\n")
                if rc_back != 0:
                    failures.append((mid, "还原后判据仍红——源码未干净恢复"))
                verdict = "红（符合预期）" if rc_mut != 0 else "绿（变异没生效！）"
                log.write(f"\n结论：{verdict}\n")
            finally:
                # 兜底：任何路径退出都要保证源码回到基线（备份按 sha 校验过）
                for name in files:
                    if sha256(HERE / name) != sums[name]:
                        shutil.copy2(backups[name], HERE / name)
                    backups[name].unlink(missing_ok=True)
        print(f"{mid}: {verdict_text(log_path)}  → _audit/{log_path.name}")
    if failures:
        print("\n有未通过项：")
        for mid, why in failures:
            print(f"  {mid}: {why}")
        return 1
    print("\n三条变异自证全部成立（基线绿 → 变异红 → 还原字节一致 → 复跑绿）。")
    return 0


def verdict_text(log_path: Path) -> str:
    """从日志里取那一行结论（给控制台看，不重复实现逻辑）。"""
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("结论："):
                return line[len("结论："):]
    except OSError:
        pass
    return "（日志缺失）"


if __name__ == "__main__":
    sys.exit(main())
