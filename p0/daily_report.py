#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""每日任务：生成使用报告 + 生成一份可独立打开的数据库备份。

为什么要备份（2026-10-22 观察期必做项）：
  一周的真实使用数据全在 `data\` 里，此前只有文档里一句「备份纪律」、没有任何副本。
  磁盘故障 / 误删 / WAL 损坏 = 七天数据直接归零。这比任何功能 BUG 都严重。

备份三步，以及为什么这么做：
  1. **PRAGMA wal_checkpoint(TRUNCATE)**：把 WAL 里的内容合回主库并截断。
     这一步可能因为其它连接（6 个 AI 的 MCP 进程）正在读写而只完成一部分 ——
     所以 busy / log / checkpointed 三个返回值**如实记进 `_meta.json`**，不谎报。
  2. **VACUUM INTO**：产出一个**一致快照**的独立数据库文件。
     不是「拷到写了一半的文件」，也不是裸拷 `index.sqlite` —— 实测裸拷会丢掉
     整个 WAL 里的数据（见 `07` D-9：主库 61 KB / WAL 1.39 MB）。
  3. **写 `_meta.json`**：条数 / 文件大小 / sha256 / 时间 / checkpoint 结果。

幂等与安全：
  - 同一天重跑会覆盖当天备份，但顺序是「先写 `.tmp` → 校验通过 → 原子替换」；
  - 校验（独立打开、integrity_check、条数逐项比对）不通过就删掉半成品、保留旧备份；
  - 绝不用一次失败的新备份换掉一份好的旧备份。

保留策略：只留最近 14 天（含今天）。只删**名字能解析成日期**的目录，其它一律不动，
          并且每次删除前确认目标在 `_backups` 之下（防越界）。

计划任务注册（用户级，不需要管理员）：
  schtasks /create /tn "fish_daily_report" /sc daily /st 23:00 ^
    /tr "\"C:\Users\15596\AppData\Local\Programs\Python\Python311\python.exe\" \"I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0\daily_report.py\""
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
DATA = HERE / "data"
DB = DATA / "index.sqlite"
BACKUPS = DATA / "_backups"
LOG = DATA / "_reports" / "_run.log"
KEEP_DAYS = 14

# 推送结果收口要复用 fish 里的唯一判定 SQL（"什么算超时"只许有一份定义）。
# 显式插 sys.path：计划任务/其它 cwd 下运行时，脚本目录不保证在 sys.path 里。
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import fish  # noqa: E402


# ------------------------------------------------------------------ 工具

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(conn) -> dict:
    """条数快照。缺表时返回 None 而不是抛错——备份不该被一个新版才有的表搞挂。"""
    def one(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.Error:
            return None

    return {
        "memories": one("SELECT COUNT(*) FROM code_index"),
        "active": one("SELECT COUNT(*) FROM code_index WHERE status='active'"),
        "superseded": one("SELECT COUNT(*) FROM code_index WHERE status='superseded'"),
        "events": one("SELECT COUNT(*) FROM events"),
        "citizens": one("SELECT COUNT(*) FROM citizens"),
    }


def _atomic_replace(src: Path, dst: Path, retries: int = 5) -> None:
    """Windows 上 `os.replace` 可能因为别的进程短暂持有目标文件而失败
    （杀软扫描、搜索索引器、或某个还开着的只读连接）。

    2026-10-22 实测踩到过：一次没关的只读连接就让当天第二次备份直接 PermissionError。
    这里重试几次再放弃——备份失败应该是真失败，不该被一次瞬时占用打掉。
    """
    last = None
    for i in range(max(retries, 1)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.2 * (i + 1))
    raise last


def checkpoint(conn) -> dict:
    """把 WAL 合回主库并截断，返回 {busy, log, checkpointed}。

    busy=1 表示有其它连接正在读写，本次只做了部分合并。数据仍然安全：
    备份走的是 VACUUM INTO 一致快照，不依赖 checkpoint 是否完全成功。
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if not row:
        return {}
    return {"busy": row[0], "log": row[1], "checkpointed": row[2]}


# ------------------------------------------------------ 推送结果收口（16 §5.6）

def mark_stale_pushes(db_path=None, window_hours=None) -> dict:
    """把超过窗口仍无反应的推送判为 ignored，返回 {ignored, window_hours}。

    为什么只能在这里做：窗口内没读 ≠ 忽略（可能只是还没用到），所以"这次推送白费了"
    这件事**只有等时间过去**才成立。判定 SQL 在 `fish.close_stale_pushes`（唯一出口），
    本函数只负责开库/关库——P0 不许出现第二份"什么算超时"的定义。

    window_hours 缺省取 config 的 push.outcome_window_hours；<=0 = 关闭判定（返回 0）。
    参数可覆盖是为了让测试能在临时目录里跑，不碰生产库。
    """
    db_path = Path(db_path or DB)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        window = (int(window_hours) if window_hours is not None
                  else fish.push_outcome_window_hours(conn))
        ignored = fish.close_stale_pushes(conn, window_hours=window)
    finally:
        conn.close()
    return {"ignored": int(ignored or 0), "window_hours": window}


# ------------------------------------------------------------------ 备份

def backup_database(db_path=None, backups_dir=None, day=None, keep_days=KEEP_DAYS) -> dict:
    """生成当天备份。返回 meta dict（含 ok / path / sha256 / counts 等）。

    参数可覆盖是为了让测试能在临时目录里跑，不碰生产库。
    """
    db_path = Path(db_path or DB)
    backups_dir = Path(backups_dir or BACKUPS)
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    day = day or datetime.date.today().isoformat()
    dest_dir = backups_dir / day
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "index.sqlite"
    tmp = dest_dir / "index.sqlite.tmp"
    if tmp.exists():
        tmp.unlink()                      # VACUUM INTO 要求目标不存在

    # ---- 1) checkpoint + 2) VACUUM INTO（同一个连接里做，保证看到同一份状态）----
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        ck = checkpoint(conn)
        counts = _counts(conn)
        src_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()

    # ---- 3) 校验：能独立打开、integrity ok、条数逐项一致 ----
    problems = []
    if src_integrity != "ok":
        problems.append(f"源库 integrity_check={src_integrity}")
    bconn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        b_integrity = bconn.execute("PRAGMA integrity_check").fetchone()[0]
        bcounts = _counts(bconn)
    finally:
        bconn.close()
    if b_integrity != "ok":
        problems.append(f"备份 integrity_check={b_integrity}")
    for k, v in counts.items():
        if v is not None and bcounts.get(k) != v:
            problems.append(f"{k} 条数不一致：源 {v} / 备份 {bcounts.get(k)}")
    if problems:
        tmp.unlink(missing_ok=True)       # 半成品删掉，旧备份原样保留
        raise RuntimeError("备份校验失败：" + "；".join(problems))

    # ---- 4) 原子替换 + 原子写 meta ----
    _atomic_replace(tmp, dest)
    meta_path = dest_dir / "_meta.json"
    meta = {
        "day": day,
        "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(db_path),
        "backup": "index.sqlite",
        "size_bytes": dest.stat().st_size,
        "sha256": _sha256(dest),
        "counts": counts,
        "integrity": b_integrity,
        "wal_checkpoint": ck,
        "keep_days": keep_days,
        "ok": True,
        "path": str(dest),
        "meta_path": str(meta_path),
    }
    mtmp = dest_dir / "_meta.json.tmp"
    mtmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _atomic_replace(mtmp, meta_path)     # 目标是 _meta.json，绝不能是 dest
    return meta


def prune_backups(backups_dir=None, keep_days=KEEP_DAYS, day=None) -> list:
    """只保留最近 keep_days 天（含当天）的备份目录，返回被删的目录名。"""
    backups_dir = Path(backups_dir or BACKUPS)
    if not backups_dir.exists():
        return []
    today = datetime.date.fromisoformat(day) if day else datetime.date.today()
    cutoff = today - datetime.timedelta(days=max(keep_days, 1) - 1)
    base = backups_dir.resolve()
    removed = []
    for d in sorted(backups_dir.iterdir()):
        if not d.is_dir():
            continue
        try:
            dd = datetime.date.fromisoformat(d.name)
        except ValueError:
            continue                       # 不是日期目录 → 不动
        if dd >= cutoff:
            continue
        p = d.resolve()
        if base not in p.parents:          # 防越界：绝不动 _backups 之外的东西
            continue
        shutil.rmtree(p)
        removed.append(d.name)
    return removed


# ------------------------------------------------------------------ 报告

def _run_report(days=1):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([PY, str(HERE / "fish.py"), "--root", str(DATA),
                           "report", "--days", str(days), "--save"],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=120)


def main(argv=None):
    ap = argparse.ArgumentParser(description="fish 每日任务：生成报告 + 数据库备份")
    ap.add_argument("--skip-report", action="store_true", help="只做备份，不生成报告")
    ap.add_argument("--days", type=int, default=1, help="报告统计最近 N 天（默认 1）")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS, help="备份保留天数（默认 14）")
    args = ap.parse_args(argv)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    report_code, out, err = 0, "", ""
    if not args.skip_report:
        r = _run_report(args.days)
        report_code, out, err = r.returncode, r.stdout or "", r.stderr or ""

    # 推送结果收口（16 §5.6）：必须在备份**之前**——当天的备份要带"已经判完"的状态，
    # 而不是一堆悬着的 NULL。失败只记一行、不改退出码：派生数据的失败不该让日报假红
    # （主流程 = 报告 + 备份，推送日志写不进去时它们必须照跑）。
    push_line = ""
    try:
        st = mark_stale_pushes()
        push_line = f"推送收口：ignored {st['ignored']} 条（窗口 {st['window_hours']} h）"
    except Exception as e:                 # noqa: BLE001 — 派生数据，不许拖垮主流程
        push_line = f"推送收口失败：{type(e).__name__}: {e}"

    # 看板归档（2026-10-25 · 用户要求 C）：把 N 天前的 open 板置 closed。
    # 为什么挂在每日任务：它天然就是"每天该干一次"的事，而这里已经有调度；
    # 再不归档的话 `boards` 里的 open 板会日复一日累积（看板按日切、没有别的东西会关它）。
    board_line = ""
    try:
        sys.path.insert(0, str(HERE))
        import fish as fishlib             # noqa: PLC0415 —— 延迟导入：日报不该依赖 fish 的加载期副作用
        conn = fishlib.connect(DATA)
        try:
            sw = fishlib.board_sweep(conn, DATA)
        finally:
            conn.close()
        board_line = (f"看板归档：{sw['swept']} 块（截止 {sw['cutoff']}）"
                      if sw.get("cutoff") else "看板归档：跳过（board.sweep_days < 1）")
    except Exception as e:                 # noqa: BLE001 — 派生数据，不许拖垮主流程
        board_line = f"看板归档失败：{type(e).__name__}: {e}"

    # 备份即使报告失败也要做：数据安全 > 报告成功
    backup_ok, backup_line = True, ""
    try:
        meta = backup_database(keep_days=args.keep_days)
        pruned = prune_backups(keep_days=args.keep_days)
        ck = meta["wal_checkpoint"]
        backup_line = (f"备份 OK：{meta['path']}（{meta['size_bytes']} B，"
                       f"sha256 {meta['sha256'][:12]}…，记忆 {meta['counts']['memories']} / "
                       f"事件 {meta['counts']['events']}，checkpoint busy={ck.get('busy')}）")
        if pruned:
            backup_line += f"；已清理超期备份：{', '.join(pruned)}"
    except Exception as e:                 # noqa: BLE001 — 备份失败必须如实报出，不能吞
        backup_ok = False
        backup_line = f"备份失败：{type(e).__name__}: {e}"

    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n=== {stamp} report_exit={report_code} backup_ok={backup_ok} ===\n")
        if out:
            f.write(out)
        if err:
            f.write("\n[stderr]\n" + err)
        # 按发生顺序落日志：收口（判定）→ 备份（把判定后的状态拍下来）
        if push_line:
            f.write(push_line + "\n")
        if board_line:
            f.write(board_line + "\n")
        f.write(backup_line + "\n")

    print(f"[{stamp}] 报告 exit={report_code}；{backup_line}")
    if push_line:
        print(f"[{stamp}] {push_line}")
    if board_line:
        print(f"[{stamp}] {board_line}")
    return 0 if (report_code == 0 and backup_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
