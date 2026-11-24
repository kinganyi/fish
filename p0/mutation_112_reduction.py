#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · 112 架构收敛（W-A / W-B / W-C / W-D）。

## 为什么每一波都要它

112 §七的结论是：**没红过的判据不算判据**。`test_surface_naming.py:198` 就是
"两边都读同一个答案"的假绿——它今天在分叉已成立的情况下依然全绿。所以本轮的
每条判据都配一个**真源码变异**：

    改源码（打在语义开关上，不是注释/变量名）
      → 指定判据必须红
      → 同轮其它新判据文件不许新红（否则红的理由不可信）
      → 逐字节还原 → sha256 与基线一致

驱动方式：

    cd p0
    python mutation_112_reduction.py            # 全部
    python mutation_112_reduction.py --only WA-M2

退出码：0 = 全部按预期；1 = 基线不是全绿；2 = 某个变异没被抓住 / 新红 / 还原失败。
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from mutation_source import SourceText  # noqa: E402

#: 各波的判据文件（按波次加；未落地的波次不进基线）
NEW_FILES = {
    "wa": "test_reduction_wa",
    "wb": "test_reduction_wb",
    "wc": "test_reduction_wc",
    "wd": "test_reduction_wd",
}

MUTANTS = [
    # ── W-A ──────────────────────────────────────────────────────────────
    {
        "id": "WA-M1",
        "file": "fish_pretool_hook.py",
        "desc": "把 `_host_from_argv` 复制回 hook（四份副本的老问题复发）",
        "old": 'DEFAULT_HOST = "dsh"\n',
        "new": ('def _host_from_argv(argv):\n'
                '    return "dsh"\n\n\n'
                'DEFAULT_HOST = "dsh"\n'),
        "must_fail": [
            "test_reduction_wa.HostAndDbgConsolidatedTest"
            ".test_no_local_host_from_argv_anywhere",
        ],
        "others": [],
    },
    {
        "id": "WA-M2",
        "file": "fish_posttool_hook.py",
        "desc": "posttool 的缺省 host 改成 `dsh`（112 §三 B3 的行为分叉）",
        "old": 'DEFAULT_HOST = "workbuddy"',
        "new": 'DEFAULT_HOST = "dsh"',
        "must_fail": [
            "test_reduction_wa.HostFromArgvBehaviourTest"
            ".test_each_hook_passes_its_historical_default_explicitly",
        ],
        "others": [],
    },
    {
        "id": "WA-M3",
        "file": "fish_activity.py",
        "desc": "在 activity 里再写一份 `_presence_has_session`（委托被撤掉）",
        "old": "def _surfaces_query(project, cutoff, *, by_session: bool):",
        "new": ('def _presence_has_session(conn) -> bool:\n'
                '    return True\n\n\n'
                'def _surfaces_query(project, cutoff, *, by_session: bool):'),
        "must_fail": [
            "test_reduction_wa.PresenceProbeConsolidatedTest.test_only_one_definition",
            "test_reduction_wa.PresenceProbeConsolidatedTest"
            ".test_activity_delegates_to_the_single_copy",
        ],
        "others": [],
    },
    {
        "id": "WA-M4",
        "file": "fish_activity.py",
        "desc": "状态文件改回**裸写**（原子性丢失——写到一半被杀就是半文件）",
        "old": ('        fish_atomic.write_text_atomic(\n'
                '            path, json.dumps(payload, ensure_ascii=False))\n'
                '        return True'),
        "new": ('        path.write_text(json.dumps(payload, ensure_ascii=False),\n'
                '                          encoding="utf-8")\n'
                '        return True'),
        "must_fail": [
            "test_reduction_wa.AtomicStateWriteTest.test_activity_state_write_uses_fish_atomic",
        ],
        "others": [],
    },
    # ── W-B ──────────────────────────────────────────────────────────────
    {
        "id": "WB-M1",
        "file": "fish_push_vocab.py",
        "desc": "往通道单源里加历史词 `hook`（声明集合 ≠ 库实际集合）",
        "old": 'PUSH_CHANNELS = ("inject", "prompt", "tool_after")',
        "new": 'PUSH_CHANNELS = ("inject", "prompt", "tool_after", "hook")',
        "must_fail": [
            "test_reduction_wb.ChannelVocabularyTest.test_single_source_is_the_actual_three",
            "test_reduction_wb.ChannelVocabularyTest"
            ".test_declared_equals_db_actual_with_a_negative_control",
        ],
        "others": [],
    },
    {
        "id": "WB-M2",
        "file": "fish_push_vocab.py",
        "desc": "采纳率分母改成 accepted+ignored+1（口径漂移的典型形态）",
        "old": "    decided = accepted + ignored",
        "new": "    decided = accepted + ignored + 1",
        "must_fail": [
            "test_reduction_wb.AcceptRateSingleSourceTest.test_formula_and_cross_endpoint_agreement",
            "test_reduction_wb.AcceptRateSingleSourceTest.test_workbench_stats_uses_the_same_rate",
        ],
        "others": [],
    },
    {
        "id": "WB-M3",
        "file": "fish_ui.py",
        "desc": "兜底短标签改回取 id 的**末** 4 位（同一会话两个标签）",
        "old": "    return fish_naming.session_short(session_key)",
        "new": ('    if not session_key:\n'
                '        return ""\n'
                '    host, _, rest = session_key.partition(":")\n'
                '    tail = rest[-4:] if len(rest) >= 4 else rest\n'
                '    return "%s#%s" % (host or "?", tail)'),
        "must_fail": [
            "test_reduction_wb.SessionShortLabelTest.test_ui_tag_equals_naming_short",
            "test_reduction_wb.SessionShortLabelTest.test_tail_of_id_is_never_used",
        ],
        "others": [],
    },
    {
        "id": "WB-M4",
        "file": "fish_workbench_api.py",
        "desc": "推送 Top 的率改回内联公式（不再是单源，但数值可能一样）",
        "old": '                "rate": fish_push_vocab.accept_rate(accepted, ignored),',
        "new": ('                "rate": ((accepted / (accepted + ignored))\n'
                '                         if (accepted + ignored) else None),'),
        "must_fail": [
            "test_reduction_wb.AcceptRateSingleSourceTest"
            ".test_all_three_read_paths_call_the_one_function",
        ],
        "others": [],
    },
    # ── W-C ──────────────────────────────────────────────────────────────
    {
        "id": "WC-M1",
        "file": "fish_surface.py",
        "desc": "摘掉 canonical 兜底（junction 上两个解析器又分叉，112 §三 A2）",
        "old": ('        if target == norm or target.startswith(norm + "/"):\n'
                '            canon = norm\n'
                '        else:\n'
                '            if target_canon is None:\n'
                '                target_canon = canonical_path(path)\n'
                '            canon = canonical_path(key)\n'
                '            if not (target_canon == canon '
                'or target_canon.startswith(canon + "/")):\n'
                '                continue\n'),
        "new": ('        if target == norm or target.startswith(norm + "/"):\n'
                '            canon = norm\n'
                '        else:\n'
                '            continue  # 变异：只做文本归一\n'),
        "must_fail": [
            "test_reduction_wc.JunctionParityTest.test_mapping_real_query_link",
            "test_reduction_wc.JunctionParityTest.test_mapping_link_query_real",
            "test_reduction_wc.JunctionParityTest"
            ".test_longest_prefix_and_component_boundary_survive_realpath",
        ],
        "others": [],
    },
    # ── W-D ──────────────────────────────────────────────────────────────
    {
        "id": "WD-M1",
        "file": "fish_push_audit.py",
        "desc": "把审计的时间原语改回 `now_utc`（同名不同义复发）",
        "old": "def now_utc_dt() -> datetime:",
        "new": "def now_utc() -> datetime:",
        "must_fail": [
            "test_reduction_wd.SameNameDifferentMeaningTest.test_now_utc_is_str_only",
        ],
        "others": [],
    },
    {
        "id": "WD-M2",
        "file": "fish_activity.py",
        "desc": "把 `inf` 策略并成 `None`（两份策略被合成一种）",
        "old": '    return float("inf") if age is None else age',
        "new": "    return age  # 变异：策略被合并（与 git 侧同一种）",
        "must_fail": [
            "test_reduction_wd.AgePrimitiveStrategyTest.test_policies_stay_at_call_sites",
        ],
        "others": [],
    },
    {
        "id": "WD-M3",
        "file": "fish_activity.py",
        "desc": "状态文件函数改回 `_write_state`（与 fastpath 同名不同策略）",
        "old": "def _write_state_file(state_path, payload):",
        "new": "def _write_state(state_path, payload):",
        "must_fail": [
            "test_reduction_wd.SameNameDifferentMeaningTest"
            ".test_state_file_accessors_are_renamed",
        ],
        "others": [],
    },
]

LOG_DIR = HERE / "_audit" / "mutation_112_reduction"


def _run(*specs) -> int:
    return _run_detail(*specs)[0]


def _run_detail(*specs):
    cmd = [sys.executable, "-m", "unittest", *specs]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=900)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    name = "-".join(spec.split(".")[-1] for spec in specs)[:80]
    (LOG_DIR / f"{name}.log").write_text(
        f"$ python -m unittest {' '.join(specs)}\nexit={proc.returncode}\n\n"
        f"---- stdout ----\n{proc.stdout}\n---- stderr ----\n{proc.stderr}",
        encoding="utf-8")
    return proc.returncode, proc.stderr.strip().splitlines()[-6:]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    only = None
    if "--only" in argv:
        index = argv.index("--only")
        only = argv[index + 1].upper() if index + 1 < len(argv) else ""
    print("=" * 74)
    print(f"112 架构收敛 · 变异自证（{len(MUTANTS)} 个变异体）"
          + (f" ｜ 只跑 {only}" if only else ""))
    print("=" * 74)
    print("\n[0/3] 基线：新判据文件必须全绿")
    for name, module in NEW_FILES.items():
        rc = _run(module)
        print(f"  {module:26} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            print("\n基线不是全绿——判据本身有问题，终止。", file=sys.stderr)
            return 1

    failures = []
    print("\n[1/3] 逐个变异 → 指定判据必红 → 其余新判据不许新红 → 逐字节还原")
    for mutant in MUTANTS:
        if only and mutant["id"] != only:
            continue
        path = HERE / mutant["file"]
        src = SourceText(path)
        base_sha = _sha(path)
        try:
            try:
                src.write(src.replaced(mutant["old"], mutant["new"]))
            except AssertionError as exc:
                print(f"\n{mutant['id']} 靶点失效（源码形态变了）：{exc}", file=sys.stderr)
                failures.append((mutant["id"], "靶点失效"))
                continue
            caught = True
            details = []
            for spec in mutant["must_fail"]:
                rc, tail = _run_detail(spec)
                details.append(f"{spec.split('.')[-1]}={'红 ✓' if rc != 0 else '绿 ✗'}")
                if rc == 0:
                    caught = False
                    print(f"\n    ↳ {spec} 变异后仍然绿（判据没有判别力）：{tail}")
            clean = True
            for key in mutant["others"]:
                rc, tail = _run_detail(NEW_FILES[key])
                if rc != 0:
                    clean = False
                    details.append(f"{NEW_FILES[key]}=新红 ✗")
                    print(f"\n    ↳ {NEW_FILES[key]} 出现**预期外**的新红：{tail}")
            print(f"\n{mutant['id']} {'CAUGHT' if caught and clean else 'MISSED'} "
                  f"{mutant['desc']}")
            print(f"    {mutant['file']} ｜ " + "，".join(details))
            if not caught:
                failures.append((mutant["id"], "指定判据没红"))
            if not clean:
                failures.append((mutant["id"], "其余新判据出现新红"))
        finally:
            src.restore()
            restored = _sha(path) == base_sha
            print(f"    还原：sha256 {'一致 ✓' if restored else '不一致 ✗'}（{path.name}）")
            if not restored:
                print("    ✗✗ 还原失败，立即中止！", file=sys.stderr)
                return 2

    print("\n[2/3] 还原后复跑（应全绿）")
    for name, module in NEW_FILES.items():
        rc = _run(module)
        print(f"  {module:26} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            failures.append((module, "还原后仍红"))

    print("\n[3/3] 结论")
    print("=" * 74)
    ran = [m["id"] for m in MUTANTS if not only or m["id"] == only]
    if failures:
        for mid, why in failures:
            print(f"  {mid}: {why}", file=sys.stderr)
        print(f"结果：{len(failures)} 项不符 —— 变异自证 **未通过**", file=sys.stderr)
        return 2
    print(f"结果：{len(ran)}/{len(ran)} 变异体全部被抓住，其余新判据无新红")
    print("exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
