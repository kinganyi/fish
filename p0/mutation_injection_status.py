#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异自证：把注入包里的 status 过滤摘掉，看判据是否变红。

驱动方式（真源码变异 → 跑 → 必红 → 字节还原）：
  python mutation_injection_status.py

本驱动只改 `fish.py` 的四处 SQL，每次改完立即还原（sha256 校验）。

2026-10-26（F1）：读写统一走 `mutation_source.SourceText`——工作树是 CRLF，
旧实现 `read_text/write_text` 虽能读，但靶点里的 `'\\n'` 在别的脚本上会 0 命中；
这里一并换成"LF 归一匹配 + 原字节还原"。
2026-10-26（F2）：C 的判据名已随 L2 改版迁移（原「今日编码」段已删除），
用例名同步成 `test_C_index_lists_only_projects_with_active_memories`。
"""
import hashlib
import shutil
import subprocess
import sys

# 第四轮评审：本文件打印 ✓ / ✅ / 🟠 这类符号，Windows 控制台默认 GBK 会崩
# （UnicodeEncodeError → 退出码 1，看起来像"变异失败"）。fish.py 与 tools/*.py
# 早修过，但根目录的 mutation_*.py 一直没修——同族文件要一起看。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # A档：拿不到可重配的流就按原样
from pathlib import Path

from mutation_source import SourceText

FISH = Path("fish.py")
BAK = FISH.with_suffix(".py.mutation-bak")

MUTATIONS = [
    (
        "M-A", "去掉 _project_activity 的 status 过滤（活跃数虚高）",
        'f"WHERE project IN ({ph}) AND dormant=0 AND status=\'active\'", pnames).fetchone()',
        'f"WHERE project IN ({ph}) AND dormant=0", pnames).fetchone()',
        "test_A_active_count_excludes_non_active",
    ),
    (
        # 2026-11-26（P2-3）：旧靶点写的是 `"AND status='active' ORDER BY ...` 的连续
        # 字符串，后来 recent 查询接入了 `_visibility_sql`（见 fish.py 的 F-P1-X 修复），
        # `+ _vis_sql +` 把这段字面量切成两半 → 旧靶点 0 命中、M-B 长期 SKIP。
        # 现在按**当前源码**钉：整段包含 `"AND status='active'" + _vis_sql +`；
        # 去掉 status 过滤后 SQL 变成 `dormant=0  ORDER BY ...`，作废条目会冒进 → B 必红。
        "M-B", "去掉 recent 查询的 status 过滤（可能推已作废）",
        'WHERE project=? AND dormant=0 "\n            "AND status=\'active\'" + _vis_sql + " ORDER BY code DESC LIMIT 3',
        'WHERE project=? AND dormant=0 "\n            "" + _vis_sql + " ORDER BY code DESC LIMIT 3',
        "test_B_recent_excludes_non_active",
    ),
    (
        "M-C", "「可查项目」的 JOIN 不筛 status（非 active 也被算进计数）",
        '        "JOIN code_index c ON c.project = r.prefix AND c.status = \'active\' "',
        '        "JOIN code_index c ON c.project = r.prefix "',
        "test_C_index_lists_only_projects_with_active_memories",
    ),
    (
        "M-D", "去掉 dormant_n 的 status 过滤（休眠数虚高）",
        'WHERE dormant=1 AND status=\'active\'',
        'WHERE dormant=1',
        "test_D_dormant_count_excludes_non_active",
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(name: str) -> int:
    r = subprocess.run(
        [sys.executable, "test_injection_status.py", f"InjectionStatusFilterTest.{name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    # unittest 用退出码表达结果：0=通过，非0=失败
    return r.returncode


def main() -> int:
    base = sha(FISH)
    print(f"基线 fish.py sha256 = {base[:16]}…")
    print(f"备份到 {BAK.name}")
    shutil.copy2(FISH, BAK)

    # 先确认四条判据在未变异时全绿
    print("\n=== 基线（未变异）应该全绿 ===")
    ok_all = True
    for mid, desc, _old, _new, test in MUTATIONS:
        rc = run_test(test)
        mark = "绿 ✓" if rc == 0 else f"红 ✗ (rc={rc})"
        print(f"  {test:46} {mark}")
        if rc != 0:
            ok_all = False
    if not ok_all:
        print("\n基线就不是全绿——判据本身有问题，终止。")
        shutil.move(BAK, FISH)
        return 1

    results = []
    for mid, desc, old, new, test in MUTATIONS:
        src = SourceText(FISH)
        hits = src.count(old)
        if hits != 1:
            print(f"\n{mid}: 靶点命中 {hits} 次（要求恰好 1 次），跳过（片段可能已变）")
            print(f"  想找: {old[:80]}")
            results.append((mid, desc, test, "SKIP"))
            continue
        src.write(src.replaced(old, new))
        try:
            rc = run_test(test)
            status = "红（符合预期）" if rc != 0 else "绿（变异没生效！）"
            print(f"\n{mid} [{status}] {desc}")
            print(f"  目标判据: {test}")
            results.append((mid, desc, test, status))
        finally:
            src.restore()            # 原始字节（BAK 只是崩溃兜底）
            restored = sha(FISH) == base
            print(f"  还原: sha256 {'一致 ✓' if restored else '不一致 ✗'}")
            if not restored:
                print("  ✗✗ 还原失败，立即中止！")
                return 2

    # 收尾再跑一次全绿
    print("\n=== 还原后复跑，应全绿 ===")
    for mid, desc, _o, _n, test in MUTATIONS:
        rc = run_test(test)
        print(f"  {test:46} {'绿 ✓' if rc == 0 else f'红 ✗ (rc={rc})'}")

    print("\n" + "=" * 70)
    for mid, desc, test, status in results:
        print(f"{mid:6} {test:46} {status}")
    print("=" * 70)
    print(f"最终 fish.py sha256 = {sha(FISH)[:16]}…（与基线{'一致' if sha(FISH) == base else '不一致'}）")
    BAK.unlink(missing_ok=True)
    # 外部审查 🟠-B2（2026-10-29）：SKIP / MISS 必须让进程非 0（同 workspace_normalize）。
    bad = [mid for mid, _desc, _o, _n, status in
           ((r[0], r[1], None, None, r[3]) for r in results)
           if str(status).startswith(("SKIP", "MISS", "变异没生效"))]
    if bad:
        print(f"✗ {len(bad)} 个变异未真正执行/未生效：{bad}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
