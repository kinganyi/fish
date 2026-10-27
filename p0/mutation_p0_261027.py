#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · P0 三项（2026-10-27）。

## 为什么要有它

P0-1（知识库进推送）/ P0-2（反应回填）/ P0-3（board_seen 会话化）各有一条关键
判据。本脚本把实现改坏，证明对应判据**真的会红**（否则"全绿"没有判别力）。

驱动：cd p0 && python mutation_p0_261027.py   → exit 0 = 三个变异体全部被抓住
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

NEW_FILES = {
    "board_seen": "test_board_seen_session",
    "kb": "test_push_kb",
    "outcome": "test_push_outcome",
}

MUTANTS = [
    {
        "id": "M13",
        "file": "fish.py",
        "desc": "board_seen 键退回两列（同公民多会话互相吞推送）",
        "old": '''    if "session_key" in _board_seen_columns(conn):
        return True, "board_id, citizen_id, session_key"
    return False, "board_id, citizen_id"''',
        "new": '''    return False, "board_id, citizen_id"''',
        "must_fail": ["test_board_seen_session.WriteTest.test_SBS2_two_sessions_two_rows"],
        "others": ["kb", "outcome"],
    },
    {
        "id": "M14",
        "file": "fish.py",
        "desc": "知识库候选永久为空（kb 不接推送）",
        "old": '''    try:
        vaults = kb_vault_list(conn)
    except Exception:       # noqa: BLE001 —— 知识库是增强项，坏了不拖垮候选
        return []''',
        "new": '''    return []
    try:
        vaults = kb_vault_list(conn)
    except Exception:       # noqa: BLE001 —— 知识库是增强项，坏了不拖垮候选
        return []''',
        "must_fail": ["test_push_kb.KbCandidateTest.test_SKB1_vault_hit_produces_candidate"],
        "others": ["board_seen", "outcome"],
    },
    {
        "id": "M15",
        "file": "fish.py",
        "desc": "前缀回填去掉会话条件（把别人看的算成自己看的）",
        "old": '''    cur = conn.execute(
        "UPDATE push_log SET reaction='accepted', reacted_at=?, "
        "latency_sec=CAST((julianday(?) - julianday(pushed_at)) * 86400 AS INTEGER) "
        "WHERE code LIKE ? AND session_key IS ? AND reaction IS NULL AND pushed_at >= ?",
        (ts, ts, prefix, session_key, cutoff))''',
        "new": '''    cur = conn.execute(
        "UPDATE push_log SET reaction='accepted', reacted_at=?, "
        "latency_sec=CAST((julianday(?) - julianday(pushed_at)) * 86400 AS INTEGER) "
        "WHERE code LIKE ? AND reaction IS NULL AND pushed_at >= ?",
        (ts, ts, prefix, cutoff))''',
        "must_fail": ["test_push_outcome.PrefixAcceptedTest."
                      "test_SPO2_prefix_accepts_same_session_in_window"],
        "others": ["board_seen", "kb"],
    },
]

LOG_DIR = HERE / "_audit" / "mutation_p0_261027"


def _run(*specs):
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
    return proc.returncode, (proc.stderr or proc.stdout).strip().splitlines()[-6:]


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    print("=" * 74)
    print("P0 三项 · 变异自证（3 个变异体）")
    print("=" * 74)
    print("\n[0/2] 基线：三个判据文件必须全绿")
    for name, module in NEW_FILES.items():
        rc, tail = _run(module)
        print(f"  {module:28} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")
        if rc != 0:
            print("基线不是全绿——判据本身有问题，终止。", file=sys.stderr)
            return 1
    failures = []
    print("\n[1/2] 逐个变异 → 指定判据必红 → 其余文件不许新红 → 逐字节还原")
    for mutant in MUTANTS:
        path = HERE / mutant["file"]
        src = SourceText(path)
        base_sha = _sha(path)
        try:
            try:
                src.write(src.replaced(mutant["old"], mutant["new"]))
            except AssertionError as exc:
                print(f"\n{mutant['id']} 靶点失效：{exc}", file=sys.stderr)
                failures.append((mutant["id"], "靶点失效"))
                continue
            caught, clean = True, True
            for spec in mutant["must_fail"]:
                rc, tail = _run(spec)
                if rc == 0:
                    caught = False
                    print(f"\n    ↳ {spec} 变异后仍然绿：{tail}")
            for key in mutant["others"]:
                rc, tail = _run(NEW_FILES[key])
                if rc != 0:
                    clean = False
                    print(f"\n    ↳ {NEW_FILES[key]} 出现预期外新红：{tail}")
            print(f"\n{mutant['id']} {'CAUGHT' if caught and clean else 'MISSED'} "
                  f"{mutant['desc']}")
            if not caught:
                failures.append((mutant["id"], "指定判据没红"))
            if not clean:
                failures.append((mutant["id"], "其余判据出现新红"))
        finally:
            src.restore()
            restored = _sha(path) == base_sha
            print(f"    还原：sha256 {'一致 ✓' if restored else '不一致 ✗'}（{path.name}）")
            if not restored:
                return 2
    print("\n[2/2] 还原后复跑")
    for name, module in NEW_FILES.items():
        rc, tail = _run(module)
        print(f"  {module:28} {'绿 ✓' if rc == 0 else '红 ✗'}")
        if rc != 0:
            failures.append((module, "还原后仍红"))
    if failures:
        for mid, why in failures:
            print(f"  {mid}: {why}", file=sys.stderr)
        print("结果：变异自证 **未通过**", file=sys.stderr)
        return 2
    print(f"结果：{len(MUTANTS)}/{len(MUTANTS)} 变异体全部被抓住，其余判据文件无新红")
    print("exit=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
