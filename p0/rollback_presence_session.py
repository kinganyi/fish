#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""presence 会话维度回滚 · 显式工具（T01 · 设计 45 §6.2 ②）。

    python rollback_presence_session.py --root data --dry-run   # 先看会丢什么
    python rollback_presence_session.py --root data --apply     # 合并会话行 → 一行/公民

## 它做什么（规则写死在 `fish.presence_session_rollback`，不在这里）

  · 每个 `(board_id, citizen_id)` 选 `max(last_seen_at, auto_seen_at)` 最大的行做主行；
  · `session_key` 丢弃；`agent_id` 取主行的值；
  · 主行 `first_seen_at` 换成该公民在本板**最早**的 `first_seen_at`；
  · 被丢弃的行（含 `doing` / `surface` / 会话键）逐条打印到 stderr 并落 `_audit/`
    ——**丢信息必须留证**。

## 什么时候用

只有"数据回滚"这一档需要它（设计 §6.2）：读侧止血用
`fish config set board.presence_session_rows false` 就够了，库里的会话行不动。
执行顺序：先 `--dry-run` 看会丢什么，再 `--apply`。

## 退出码

    0  成功（含"本来就是两列主键"的幂等路径）
    2  参数/库路径问题（没动库）
    4  回滚失败（原表未动；原因见 stderr）
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _dump_dropped(root: Path, payload: dict) -> Path:
    """把"被合并掉的行"落盘（丢信息必须留证）。"""
    out_dir = Path(root) / "_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"presence-session-rollback-{_stamp()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _print_dropped(dropped) -> None:
    for item in dropped:
        sk = item["session_key"] or "（无会话归属）"
        print(f"  丢弃 {item['citizen_id']} 的会话 {sk}："
              f"doing={item['doing'] or '—'} surface={item['surface'] or '—'}",
              file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="presence 回滚：三列主键 → 两列主键（合并会话行，丢弃 session_key）")
    parser.add_argument("--root", default="data", help="数据目录（含 index.sqlite）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="只算会丢什么，不写库")
    group.add_argument("--apply", dest="apply", action="store_true",
                       help="真执行（先自动备份）")
    args = parser.parse_args(argv)

    root = Path(args.root)
    db = root / "index.sqlite"
    if not db.exists():
        print(f"错误：{db} 不存在——--root 要指向含 index.sqlite 的数据目录。", file=sys.stderr)
        return 2
    if not (args.dry_run or args.apply):
        print("错误：必须显式给 --dry-run 或 --apply（回滚是破坏性动作，缺省不动）。",
              file=sys.stderr)
        return 2

    conn = fish.connect(root)
    try:
        report = fish.presence_session_report(conn)
        print(f"── 回滚前 ── 行数 {report['rows']}，主键 {report['pk']}，"
              f"pending={report['pending'] or '无'}")
        try:
            result = fish.presence_session_rollback(conn, data_root=root,
                                                    dry_run=bool(args.dry_run))
        except fish.PresenceMigrationError as exc:
            print(f"\n回滚失败（**原表未动**）：{exc}", file=sys.stderr)
            return 4
        dropped = result.get("dropped") or []
        print(f"  合并后行数      : {result['rows_after']}（原 {result['rows_before']}）")
        print(f"  被合并掉的会话行: {len(dropped)}")
        if dropped:
            _print_dropped(dropped)
        if args.dry_run:
            print("\n[dry-run] 未写库。确认无误后加 --apply。")
            return 0
        saved = _dump_dropped(root, {
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "root": str(root), "result": {k: v for k, v in result.items() if k != "dropped"},
            "dropped": dropped})
        print(f"  备份            : {result.get('backup')}")
        print(f"  丢行留证        : {saved}")
        after = fish.presence_session_report(conn)
        print(f"── 回滚后 ── 行数 {after['rows']}，主键 {after['pk']}，"
              f"pending={after['pending'] or '无'}")
        print("\n注意：读到 v3 的代码会自动重建三列主键（`migrate()` 的结构段）。"
              "要长期停在两列形态，必须同时把 `board.presence_session_rows` 置 false "
              "并停用本轮代码——回滚的真正意义是**数据可还原**，不是「永久降级」。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
