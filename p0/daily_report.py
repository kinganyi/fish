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

语料采集（2026-10-27 复审 🟠-1）：**默认关闭**。全量采集是分钟级动作（实测 515 个
  DSH 会话 + 4GB WorkBuddy），挂在日报上会让每个日报用例、每次定时任务都去扫真实
  日志。三种开启方式（优先级从高到低）：
    ① `python daily_report.py --corpus`            —— 只这次跑；
    ② `FISH_CORPUS_DISABLE=0`（=1 则强制关断）     —— 临时，环境影响；
    ③ `fish config set corpus.daily_enabled 1`     —— 持久配置（默认 false）。
  关闭时日报仍会打印一行"语料采集：跳过（…）"，把开关状态明说出来。

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
        # 2026-10-26（T02）：动作流也进备份对账。缺表（旧库还没迁移）→ None，
        # 不是 0：0 是"表在但没数据"，两者不能混。
        "tool_activity": one("SELECT COUNT(*) FROM tool_activity"),
    }


def prune_activity(days=None, db_path=None) -> dict:
    """按保留期清理动作流 + 撞车审计（返回 `{pruned, asks_pruned, days}`）。

    为什么挂在每日任务：动作流是低语义审计、按天滚动的数据，天然"每天该清一次"；
    而清理 SQL 只在 `fish_activity.prune` 一处（本函数只负责开库/关库）。
    失败不抛：派生数据的失败不该让备份假红（主流程 = 报告 + 备份）。
    """
    db_path = Path(db_path or DB)
    data_dir = db_path.parent
    result = {"pruned": 0, "asks_pruned": 0, "days": None}
    if not db_path.exists():
        return result
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        import fish_activity
        keep = int(days if days is not None else fish_activity.get_cfg(
            conn, "activity.retention_days"))
        result["days"] = keep
        result["pruned"] = fish_activity.prune(conn, days=keep)
    finally:
        conn.close()
    # 撞车审计：同一条保留期。只删"整行是合法 JSON 且时间早于截止"的行，
    # 半行/坏行原样留着（审计文件的坏行本身就是证据）。
    if int(result["days"] or 0) > 0:
        asks = data_dir / "_audit" / "hooks" / "pretool-asks.jsonl"
        if asks.exists():
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=int(result["days"]))).strftime(
                          "%Y-%m-%dT%H:%M:%SZ")
            kept, removed = [], 0
            try:
                for line in asks.read_text(encoding="utf-8").splitlines():
                    try:
                        if (json.loads(line).get("ts") or "") >= cutoff:
                            kept.append(line)
                        else:
                            removed += 1
                    except ValueError:
                        kept.append(line)
                tmp = asks.with_name(asks.name + ".tmp")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                os.replace(tmp, asks)
                result["asks_pruned"] = removed
            except OSError:
                result["asks_pruned"] = 0
    return result


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


def archive_citizens_step(data_dir=None, days=None) -> str:
    """日报的一步（FB-12）：按 `session.archive_after_days` 归档闲置公民，返回**一行**。

    为什么抽成函数而不是写在 `main()` 里：判据要能只测这一步（配 0 说"跳过"、
    配 N 真归档、失败只落一行），而 `main()` 会顺带跑报告/备份/清理——
    拿它当夹具等于让每条判据都背上整套副作用与网络无关的重活。

    `days=None` → 读 config；`days<1` → 明写"跳过（未配置）"。
    默认值就是 0（用户 2026-10-25 裁定：会话归档**无限期**，不给默认天数），
    所以"什么都不做"必须是**显式**的一句话，不能是静默。

    **异常一律转成一行文本**：报告与备份比它重要，派生动作失败不许拖垮日报
    （与推送收口 / 看板归档 / 动作流清理同一口径）。
    """
    try:
        sys.path.insert(0, str(HERE))
        import fish as fishlib             # noqa: PLC0415 —— 延迟导入：日报不该依赖 fish 的加载期副作用
        conn = fishlib.connect(Path(data_dir or DATA))
        try:
            use_days = int(fishlib.get_config(conn, "session.archive_after_days")
                           if days is None else days)
            if use_days < 1:
                return "公民归档：跳过（未配置）"
            res = fishlib.archive_citizens(conn, days=use_days, dry_run=False)
        finally:
            conn.close()
        line = (f"公民归档：{res['archived_count']} 个（阈值 {use_days} 天，"
                f"截止 {res['cutoff']}）")
        if res["skipped"]:
            line += f"；跳过 {len(res['skipped'])} 个"
        return line
    except Exception as e:                 # noqa: BLE001 — 派生数据，不许拖垮主流程
        return f"公民归档失败：{type(e).__name__}: {e}"


# 语料采集的开关（2026-10-27 复审 🟠-1）：**日报默认不采集**。
# 全量采集是分钟级动作（实测 515 个 DSH 会话 + 4GB WorkBuddy），挂在日报上等于让每个
# 日报测试/每次定时任务都去扫真实日志——测试里表现为"跑不完"，生产里表现为每天一次
# 长时间写锁。开启方式写在 `--help` 与 `docs/依赖说明-corpus.md`：见 `CORPUS_ENABLE_HINT`。
CORPUS_CFG_KEY = "corpus.daily_enabled"
ENV_CORPUS_DISABLE = "FISH_CORPUS_DISABLE"
CORPUS_ENABLE_HINT = ("开启：fish config set %s 1（持久）；临时关断：%s=1；"
                      "单次开启：daily_report.py --corpus"
                      % (CORPUS_CFG_KEY, ENV_CORPUS_DISABLE))


def _parse_bool_env(name):
    """环境变量的三态解析：`None` = 没设置；`True`/`False` = 明确；其它 → `ValueError`。

    为什么不"差不多就当真"：`FISH_CORPUS_DISABLE=banana` 猜成开或关都是替用户做了一个
    决定（扫全盘 / 让每天采集静默停掉）——这种值必须响亮，而不是被默默当默认值。
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if raw == "":
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError("环境变量 %s 的值无法解释：%r（用 1/0）" % (name, raw))


def corpus_daily_enabled(data_dir=None, *, explicit=None) -> dict:
    """决定"这次日报要不要跑语料采集"，返回 `{"enabled", "source", "reason"}`。

    优先级：`explicit`（函数参数 / CLI 开关）> 环境变量 > 配置（默认 **关闭**）。
    "为什么关"要能被人读懂（一句开启办法），不是静默跳过；配置读不到时也**关**，
    因为"读不到"不代表用户开启了它。
    """
    if explicit is not None:
        return {"enabled": bool(explicit), "source": "param",
                "reason": "本次调用显式%s" % ("开启" if explicit else "关闭")}
    try:
        env = _parse_bool_env(ENV_CORPUS_DISABLE)
    except ValueError as exc:
        return {"enabled": False, "source": "env", "reason": str(exc)}
    if env is not None:
        return {"enabled": not env, "source": "env",
                "reason": "%s=%d（环境变量%s）"
                          % (ENV_CORPUS_DISABLE, 1 if env else 0,
                             "强制关断" if env else "临时开启")}
    data = Path(data_dir) if data_dir is not None else DATA
    if not (data / "index.sqlite").exists():
        return {"enabled": False, "source": "default",
                "reason": "主库不存在，配置读不到（默认关闭）"}
    try:
        conn = fish.connect(data)
        try:
            value = bool(fish.get_config(conn, CORPUS_CFG_KEY))
        finally:
            conn.close()
    except Exception as exc:               # noqa: BLE001 —— 读不到配置 = 不采（保守）
        return {"enabled": False, "source": "config",
                "reason": "配置读取失败（%s: %s），按关闭处理"
                          % (type(exc).__name__, exc)}
    return {"enabled": value, "source": "config",
            "reason": ("配置 %s=true" % CORPUS_CFG_KEY if value
                       else "默认关闭（配置 %s 未开启）" % CORPUS_CFG_KEY)}


def corpus_ingest_step(root=None, *, enabled=None, reason=None):
    """语料采集一步（设计 §4.3）：失败只落一行、`busy` 时视为正常跳过（不改退出码）。

    **默认不执行**（2026-10-27 复审 🟠-1）：全量采集是**分钟级**动作（实测 515 个 DSH
    会话 + 4GB WorkBuddy），挂在日报上等于让每个日报测试、每次定时任务都去扫真实日志
    ——实测表现是"日报用例跑不完"（测试隔离问题），生产里则是每天一次长时间写锁。

    三档开关（优先级从高到低，见 `corpus_daily_enabled`）：
      ① 参数 `enabled=True/False`（测试与脚本显式要，`main` 也走这一档）；
      ② 环境变量 `FISH_CORPUS_DISABLE=1`（临时关断）/ `=0`（临时开启）；
      ③ 配置 `corpus.daily_enabled`（持久开关，**默认 false**）。
    关闭时返回一行**说清怎么开**的文本，不是静默跳过。

    采集窗口与 CLI 缺省同一口径：`days=fish_corpus.DEFAULT_WINDOW_DAYS`（3 天，
    2026-10-27 用户决策）。**必须显式传**——`ingest()` 的函数默认是 `None`（不限），
    漏传就等于把几个月的历史重新全量投影回来。

    为什么解析器在这里注入：项目/公民归属要读主库（`workspace_map` / `session_tokens`），
    而语料模块按判据 J2 **不许 import fish**——所以由本模块开一个主库连接喂进去。
    为什么不做常驻守护：多一个常驻面就多一处要留痕/监控的东西；日报 + 手动 CLI
    已经满足"每天采一次 + 可查滞后"（设计 D13）。
    """
    data = Path(root) if root is not None else DATA
    try:
        if enabled is None:
            decision = corpus_daily_enabled(data)
            enabled, reason = decision["enabled"], decision["reason"]
        elif not reason:
            reason = "调用方显式%s" % ("开启" if enabled else "关闭")
        if not enabled:
            return "语料采集：跳过（%s。%s）" % (reason, CORPUS_ENABLE_HINT)
        sys.path.insert(0, str(HERE))
        import fish_corpus                  # noqa: PLC0415
        conn = fish.connect(data)
        try:
            res = fish_corpus.ingest(
                hosts=("dsh", "workbuddy"), root=data,
                # 时间窗口（2026-10-27 用户决策：**只投影最近 3 天**，更早留待回填）：
                # 日报是**生产入口**，口径必须与 CLI 缺省一致，所以引用常量而不是再写一个
                # `3`——两处各写一个数字，改一处就会静默漂成两种口径。注意 `ingest()`
                # 的函数默认是 `None`（不限）：不显式传，这里就会全量采集。
                days=fish_corpus.DEFAULT_WINDOW_DAYS,
                project_of=lambda cwd: fish.resolve_project_by_cwd(conn, cwd),
                citizen_of=lambda key: fish.fish_activity.resolve_citizen(conn, key),
                candidates_of=lambda cwd: fish.list_workspace_candidates(conn, cwd))
        finally:
            conn.close()
        if res.get("busy"):
            return "语料采集：跳过（并发锁被占用，下一轮再采）"
        line = (f"语料：新增 {res['segments_new']} 段 / 会话 {res['sessions_seen']}"
                f" / 降级 {len(res['degraded'])} / 孤儿 {len(res['orphan'])}")
        if res.get("purged_by_segment_tombstone"):
            line += f" / 台账回收 {res['purged_by_segment_tombstone']} 段"
        if res.get("warnings"):
            line += f" / 告警 {len(res['warnings'])}"
        pruned = fish_corpus.prune_access_log(root=data)      # 审计保留 30 天（设计 §8）
        if pruned.get("deleted"):
            line += f"；审计清理 {pruned['deleted']} 行"
        lag = res.get("lag_sec")
        if lag is not None and lag > 26 * 3600:
            line += f" ⚠ 滞后 {round(lag / 3600.0, 1)} h（源日志可能不再增长=假增量）"
        # C-P0-9 红②（2026-10-27）：连续 N 轮 0 新增但源在变 → 日报必须喊出来。
        # 只放在 stats 里没人看得到：日报是每天唯一会被人读的那一行。
        stall = res.get("stall") or {}
        if stall.get("alarm"):
            line += (f" ⚠ 语料停滞：连续 {stall.get('zero_streak')} 轮 0 新增但源在变"
                     "（C-P0-9 红②，查 kinds/事件模型是否漂移）")
        return line
    except Exception as e:                 # noqa: BLE001 — 派生数据，不许拖垮主流程
        return f"语料采集失败：{type(e).__name__}: {e}"


# ------------------------------------------------------------------ 离线维护管道
# 2026-10-27 · T04b（34 号 §2.2/§2.3）：把"平时没人记得跑"的维护动作挂到已有的
# 每日调度上。**挂载不改既有步骤**：报告 / 收口 / 归档 / 语料 / 备份的顺序与语义
# 全都保持原样，维护只是在语料之后、备份之前多跑一段。

# 日报负责哪几个维护任务：**语料采集除外**——它在日报里已有唯一 owner
# （`corpus_ingest_step`，带 CLI/env/配置三档闸门）。若这里再跑一次：
#   ① 配置开启时会在同一天**采两遍**（分钟级动作，第二遍还会抢写锁）；
#   ② `--no-corpus` 会被维护任务绕过（那是用户明确说"这次别采"）。
# 其余六个任务归维护管道：间隔由台账判，不会与既有步骤重复。
MAINTENANCE_DAILY_ONLY = ("kb_reindex", "links_rebuild", "push_log_prune",
                          "citizens_archive", "fts_verify", "corpus_retag")


def maintenance_step(data_dir=None):
    """日报的一步（T04b）：跑离线维护管道，返回**一行**文本。

    依赖注入是这一步的核心工作：`fish_maintenance` 按依赖纪律**不 import `fish`**，
    六个门面能力由这里显式接上（`get_config` / `archive_citizens` / `reindex_fts` /
    `build_links_on_write` / `corpus_ingest` / `corpus_retag`）。

    两个语料 callable 必须带 `project_of/citizen_of/candidates_of` 解析器：没有它们，
    `ingest` 会把项目静默标成 unmapped、`retag` 会直接拒绝执行——而按设计，
    "查不到"要如实记 unmapped，不是猜一个。

    `data_dir` 可覆盖根目录（判据把整套流程落在临时目录里，不碰生产 `p0/data`）。

    **异常一律转成一行文本**：维护是派生数据，失败不许拖垮报告/备份，也不改退出码
    （与推送收口、看板归档、公民归档同一口径）。
    """
    data = Path(data_dir) if data_dir is not None else DATA
    try:
        sys.path.insert(0, str(HERE))
        import fish as fishlib                    # noqa: PLC0415 —— 延迟导入：日报不该依赖 fish 的加载期副作用
        import fish_common                        # noqa: PLC0415 —— T02 起配置/FTS 原语已下沉，维护只按 shared 层取
        import fish_corpus                        # noqa: PLC0415 —— 语料门面只在维护/采集时用
        from fish_maintenance import run_maintenance   # noqa: PLC0415

        conn = fishlib.connect(data)
        try:
            resolvers = {
                "project_of": lambda cwd: fishlib.resolve_project_by_cwd(conn, cwd),
                "citizen_of": lambda key: fishlib.fish_activity.resolve_citizen(conn, key),
                "candidates_of": lambda cwd: fishlib.list_workspace_candidates(conn, cwd),
            }
            deps = {
                # T02 已把 get_config / reindex_fts 下沉到 fish_common：维护侧只按
                # shared 层取（与 J2 的单向依赖一致），fish 侧同名只是再导出。
                "get_config": lambda key: fish_common.get_config(conn, key),
                "archive_citizens": fishlib.archive_citizens,
                "reindex_fts": fish_common.reindex_fts,
                "build_links_on_write": fishlib.build_links_on_write,
                # root 与 days 由 run_maintenance 透传（days 必须是
                # fish_corpus.DEFAULT_WINDOW_DAYS，缺省不传会变成"不限"）。
                "corpus_ingest": lambda *, root=None, days=None: fish_corpus.ingest(
                    hosts=("dsh", "workbuddy"),
                    root=Path(root) if root else data, days=days, **resolvers),
                "corpus_retag": lambda *, root=None: fish_corpus.retag(
                    root=Path(root) if root else data, **resolvers),
            }
            res = run_maintenance(conn, data, deps=deps,
                                  only=list(MAINTENANCE_DAILY_ONLY))
        finally:
            conn.close()
    except Exception as e:                 # noqa: BLE001 —— 派生数据，不许拖垮主流程
        return f"维护失败：{type(e).__name__}: {e}"

    parts = []
    for task in res["tasks"]:
        label = task["status"]
        if label == "skipped":
            label = f"skip:{task.get('reason') or '?'}"
        parts.append(f"{task['name']}={label}({task['affected']})")
    line = (f"维护：执行 {res['ran']} / 跳过 {res['skipped']} / 失败 {res['errors']}；"
            + "，".join(parts))
    if res["errors"]:
        first = next(t for t in res["tasks"] if t["status"] == "error")
        line += f"；首个错误：{first['name']} {first['error']}"
    return line


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="fish 每日任务：生成报告 + 数据库备份",
        epilog=("语料采集**默认关闭**（全量扫描是分钟级动作）。开启方式：\n"
                "  fish config set corpus.daily_enabled 1      # 持久开启\n"
                "  python daily_report.py --corpus             # 只这次跑\n"
                "  FISH_CORPUS_DISABLE=1                        # 临时关断（优先于配置）"))
    ap.add_argument("--skip-report", action="store_true", help="只做备份，不生成报告")
    ap.add_argument("--days", type=int, default=1, help="报告统计最近 N 天（默认 1）")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS, help="备份保留天数（默认 14）")
    corpus_group = ap.add_mutually_exclusive_group()
    corpus_group.add_argument(
        "--corpus", dest="corpus_on", action="store_true", default=False,
        help="本次也跑语料采集（默认不跑；持久开启用 fish config set %s 1）"
             % CORPUS_CFG_KEY)
    corpus_group.add_argument(
        "--no-corpus", dest="corpus_off", action="store_true", default=False,
        help="本次不跑语料采集（即使配置/环境变量已开启）")
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

    # 动作流清理（2026-10-26 · T02）：保留期默认 14 天，与撞车审计同一口径。
    # 按发生顺序：先清旧的 → 再备份（备份里是"已经是正确形状"的库）。
    activity_line = ""
    try:
        pr = prune_activity()
        if pr["days"] is None:
            activity_line = "动作流清理：跳过（库不存在）"
        elif int(pr["days"]) <= 0:
            activity_line = "动作流清理：跳过（activity.retention_days < 1 = 不清理）"
        else:
            activity_line = (f"动作流清理：{pr['pruned']} 行（保留 {pr['days']} 天）；"
                             f"撞车审计清 {pr['asks_pruned']} 行")
    except Exception as e:                 # noqa: BLE001 — 派生数据，不许拖垮主流程
        activity_line = f"动作流清理失败：{type(e).__name__}: {e}"

    # 公民归档（2026-10-26 · FB-12）：30 天裁决的另一半——`archive_citizens` 此前
    # **没有调用点**，于是"闲置 N 天 → 归档"只有手动 CLI 一条路，冷表永远是空的。
    # 默认 0 = 不自动归档（无限期），配了 N 才动；异常只落一行，不中断其它步骤。
    citizen_line = archive_citizens_step()

    # 语料采集（2026-10-27 · T03/T04/T05；同日复审 🟠-1 加闸门）：**默认不采集**。
    # 是否采集由 `corpus_daily_enabled()` 明确决定（CLI 开关 > 环境变量 > 配置），
    # 而不是"反正每天都跑一下"——那是最贵的一句默认。
    corpus_explicit = True if args.corpus_on else (False if args.corpus_off else None)
    corpus_decision = corpus_daily_enabled(explicit=corpus_explicit)
    if corpus_explicit is not None:
        corpus_decision["reason"] = ("命令行 %s" % ("--corpus" if corpus_explicit
                                                    else "--no-corpus"))
    corpus_line = corpus_ingest_step(enabled=corpus_decision["enabled"],
                                     reason=corpus_decision["reason"])

    # 离线维护管道（T04b · 34 号 §2.2/§2.3）：语料步之后、备份之前——
    # 备份要拍下"维护之后"的形状（索引/FTS/边/清理都已完成），顺序不能反。
    # 失败只落一行、不改退出码：维护是派生数据，不许让日报假红。
    maintenance_line = maintenance_step()

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
        if activity_line:
            f.write(activity_line + "\n")
        # 顺序 = 发生顺序：清理 → 归档（搬行）→ 语料（采集）→ 备份（把采完的状态拍下来）
        if citizen_line:
            f.write(citizen_line + "\n")
        if corpus_line:
            f.write(corpus_line + "\n")
        if maintenance_line:
            f.write(maintenance_line + "\n")
        f.write(backup_line + "\n")

    print(f"[{stamp}] 报告 exit={report_code}；{backup_line}")
    if push_line:
        print(f"[{stamp}] {push_line}")
    if board_line:
        print(f"[{stamp}] {board_line}")
    if activity_line:
        print(f"[{stamp}] {activity_line}")
    if citizen_line:
        print(f"[{stamp}] {citizen_line}")
    if corpus_line:
        print(f"[{stamp}] {corpus_line}")
    if maintenance_line:
        print(f"[{stamp}] {maintenance_line}")
    return 0 if (report_code == 0 and backup_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
