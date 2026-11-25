#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证 · P2-2（2026-11-25 外部审查）。

三条修复各自都要证明"判据真的测到了修复点"，而不是第一次跑就是绿的：

  M-P2-2-A  `read_body_path` 的 body_sha256 比对改成 `if False`（不比对）→ B2 必红
  M-P2-2-B  补齐工具把 `memory.imported` 写成 `memory.recorded` → F2 必红
  M-P2-2-C  `fish_rebuild.py` 的 upsert 改回 `INSERT OR IGNORE` → D1 必红

每条都是：基线必须绿 → 变异后必须红 → 按 sha256 字节还原 → 复跑必须回到绿。

用法：`python mutation_p2_2.py`（无参数；日志写 `_audit/变异自证-P2-2-*.log`）
"""
from __future__ import annotations

import shutil
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # A档：拿不到可重配的流就按原样
from pathlib import Path

from mutation_review_r3 import AUDIT, run_tests, sha256

HERE = Path(__file__).resolve().parent

MUTATIONS = [
    (
        "M-P2-2-A",
        "变异自证-P2-2-A.log",
        "body_sha256 比对改成 if False（回到“有字段、无校验”）",
        [(
            "fish.py",
            "    if sha != fish_atomic.sha256_of_file(p):\n",
            "    if False:  # 变异：不比对正文哈希\n",
        )],
        ["test_body_sha_verify.BodyShaVerifyTest.test_B2_tampered_body_reports_corrupt_without_raising"],
    ),
    (
        "M-P2-2-B",
        "变异自证-P2-2-B.log",
        "补齐工具把事件类型写成 memory.recorded（而不是 memory.imported）",
        [(
            "tools/backfill_projection_events.py",
            '                    conn, "memory.imported",\n',
            '                    conn, "memory.recorded",\n',
        )],
        ["test_rebuild_duplicate_codes.BackfillProjectionEventsTest.test_F2_apply_appends_imported_event"],
    ),
    (
        "M-P2-2-C",
        "变异自证-P2-2-C.log",
        "rebuild 的 upsert 改回 INSERT OR IGNORE（只保留第一条事件）",
        [(
            "fish_rebuild.py",
            '                "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,"\n'
            '                "status,tags,file_path,occurred_at,recorded_at,session_actor,attachments) "\n'
            '                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "\n'
            '                "title=excluded.title,project=excluded.project,owner=excluded.owner,"\n'
            '                "mem_type=excluded.mem_type,visibility=excluded.visibility,"\n'
            '                "status=excluded.status,tags=excluded.tags,file_path=excluded.file_path,"\n'
            '                "occurred_at=excluded.occurred_at,recorded_at=excluded.recorded_at,"\n'
            '                "session_actor=excluded.session_actor,attachments=excluded.attachments",\n',
            '                "INSERT OR IGNORE INTO code_index (code,title,project,owner,mem_type,visibility,"\n'
            '                "status,tags,file_path,occurred_at,recorded_at,session_actor,attachments) "\n'
            '                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",\n',
        )],
        ["test_rebuild_duplicate_codes.DuplicateCodesTest.test_D1_last_event_wins_and_reported"],
    ),
]

EXPECT = {
    "M-P2-2-A": "B2：篡改正文必须报 corrupt；不比对就会以 exact/ok 通过",
    "M-P2-2-B": "F2：apply 必须写 memory.imported（cause=backfill:p2-2）",
    "M-P2-2-C": "D1：同一编码两条事件，rebuild 后 title 必须是第二条",
}


def main() -> int:
    AUDIT.mkdir(parents=True, exist_ok=True)
    failures = []
    for mid, log_name, desc, patches, tests in MUTATIONS:
        log_path = AUDIT / log_name
        files = sorted({name for name, _old, _new in patches})
        backups = {name: HERE / (name + ".p22mut-bak") for name in files}
        sums = {name: sha256(HERE / name) for name in files}
        verdict = "未执行"
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"# {mid} · {desc}\n")
            log.write(f"# 期望：{EXPECT[mid]} —— 这几条判据**变红**\n")
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
    print(f"{len(MUTATIONS)} 条变异自证成立（基线绿 → 变异红 → 还原字节一致 → 复跑绿）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
