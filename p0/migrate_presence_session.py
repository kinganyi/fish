#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""presence 会话维度迁移 · 显式工具（T01 · 设计 45 §2.3）。

三种模式（互斥，缺省 `--report`）：

    python migrate_presence_session.py --root data --dry-run    # 只报告，不写
    python migrate_presence_session.py --root data --apply      # 备份 + 重建 + 三重校验
    python migrate_presence_session.py --root data --report     # 读现状

## 为什么只有 CLI、没有第二份迁移逻辑

迁移逻辑只有一份，在 `fish._ensure_presence_session`（本脚本直接调它）。本项目为
"同一件事两份实现"付过多次学费，所以这里只负责三件事：CLI、备份路径、报告落盘。

## 为什么通常不需要手动跑

`migrate()` 的**结构段**里已经挂了自动迁移（设计 D5：带备份 + 三重校验 + 失败即降级），
合并后的首次 CLI 运行就会自动执行。本脚本的用途是：① 演练（`--dry-run`）；
② 半迁移态的显式收口；③ 把读数落成 JSON 供交付报告引用。

## 退出码

    0  成功（含"本来就已经是 v3"的幂等路径）
    2  参数/库路径问题（没动库）
    3  迁移完成但留下了 `pending`（半迁移态，需要人工处置）
    4  迁移失败（原表未动；原因见 stderr）
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _print_report(title, data):
    """人读的报告（机器读的那份同时落 `_audit/`，见 `_save`）。"""
    print(f"── {title} ──")
    print(f"  行数            : {data['rows']}")
    print(f"  主键            : {data['pk'] or '（表不存在）'}")
    print(f"  session_key 列  : {'有' if data['has_session_key'] else '无'}")
    print(f"  有会话归属      : {data['backfilled']}"
          f"（迁移前预期回填 {data['expected_backfilled']}）")
    print(f"  无会话归属 ''   : {data['blank']}")
    print(f"  pending         : {data['pending'] or '无'}")
    conflicts = data.get("prefix_conflicts") or []
    print(f"  短/长前缀疑似同会话: {len(conflicts)} 条（**只报告不合并**）")
    for item in conflicts[:10]:
        print(f"    · {item['citizen_id']}: {item['short']} ⊂ {item['long']}")
    if len(conflicts) > 10:
        print(f"    · …共 {len(conflicts)} 条")


def _save(root: Path, payload: dict) -> Path:
    out_dir = root / "_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"presence-session-migration-{_stamp()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="presence 会话维度（三列主键）迁移工具：报告 / 演练 / 执行")
    parser.add_argument("--root", default="data", help="数据目录（含 index.sqlite）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="只报告现状与 apply 会做什么，**不写任何东西**")
    group.add_argument("--apply", dest="apply", action="store_true",
                       help="备份 + 重建 + 三重校验（逻辑在 fish._ensure_presence_session）")
    group.add_argument("--report", dest="report", action="store_true",
                       help="读现状（默认动作）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    db = root / "index.sqlite"
    if not db.exists():
        print(f"错误：{db} 不存在——--root 要指向**含 index.sqlite 的数据目录**。",
              file=sys.stderr)
        return 2

    conn = fish.connect(root)
    try:
        before = fish.presence_session_report(conn)
        _print_report(f"迁移前现状（{root}）", before)
        if not args.apply:
            if args.dry_run:
                if before["pending"] == "rebuild":
                    would = ("会备份到 " + str(root / "_backups")
                             + "/index-before-presence-session-<ts>.sqlite，然后重建三列主键")
                elif before["pending"] is None:
                    would = "无需重建（已经是三列主键，apply 会是空操作）"
                else:
                    would = (f"pending={before['pending']}：本脚本会尝试按设计 §2.2 "
                             "重跑一次（不通过就回滚，原表不动）")
                print(f"\n[dry-run] {would}")
                print("[dry-run] 未写任何东西。")
            return 0
        started = time.time()
        try:
            result = fish._ensure_presence_session(conn, data_root=root)
        except fish.PresenceMigrationError as exc:
            print(f"\n迁移失败（**原表未动**）：{exc}", file=sys.stderr)
            return 4
        elapsed = round(time.time() - started, 3)
        after = fish.presence_session_report(conn)
        payload = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "elapsed_sec": elapsed, "root": str(root),
                   "before": before, "result": result, "after": after,
                   "backup": result.get("backup")}
        saved = _save(root, payload)
        print(f"\n── 迁移结果 ──")
        print(f"  rebuilt         : {result['rebuilt']}"
              f"{'（已重建三列主键）' if result['rebuilt'] else '（幂等：无动作）'}")
        print(f"  行数守恒        : {result['rows_before']} → {result['rows_after']}")
        print(f"  回填 / 空白     : {result['backfilled']} / {result['blank']}")
        print(f"  备份            : {result['backup'] or '（本次未产生）'}")
        print(f"  耗时            : {elapsed}s")
        print(f"  报告落盘        : {saved}")
        _print_report("迁移后现状", after)
        if result.get("pending"):
            print(f"\n⚠ 仍留 pending={result['pending']}——按设计 §2.2 步 3/4 不自动重建，"
                  "请人工确认后处置（同公民多会话目前会互相覆盖）。", file=sys.stderr)
            return 3
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
