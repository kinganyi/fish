#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**生产库只读副本**跑一次注入包，把 ③规矩 / ④该注意什么 两段的真实内容打出来。

只读纪律：
  · 生产库先整目录复制到 tempdir（含 `_audit/tool-calls.jsonl`，C 类要用）；
  · 副本用 `file:...?mode=ro` 打开——写任何东西都会当场报错，杜绝"手滑写生产"；
  · 结束删 tempdir。

用法：python _sample_inject.py [session_label]
"""
import datetime
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

P0 = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fish", str(P0 / "fish.py"))
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

PROD_DATA = P0 / "data"
PROJECT = "ANYI"


def ro_copy():
    tmp = Path(tempfile.mkdtemp(prefix="fish-sample-"))
    dst = tmp / "data"
    shutil.copytree(PROD_DATA, dst)
    uri = "file:" + str(dst / "index.sqlite").replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return tmp, dst, conn


def main():
    label = (sys.argv[1] if len(sys.argv) > 1 else "接入样例-新会话").strip()
    tmp, data, conn = ro_copy()
    try:
        owner = fish.resolve_owner(conn)
        owner_name = conn.execute("SELECT display_name FROM citizens WHERE citizen_id=?",
                                  (owner,)).fetchone()[0]
        actor = conn.execute(
            "SELECT citizen_id,display_name FROM citizens WHERE display_name='fish-maintainer'"
        ).fetchone()
        print(f"生产库副本：{data}")
        print(f"主人 resolve_owner = {owner}（{owner_name}）")
        print(f"行动者 = {actor[0]}（{actor[1]}）；本会话标签 = {label}；项目 = {PROJECT}")

        print("\n--- 诊断：C 类（别处读过但你没看）在几个候选标签下的命中 ---")
        now = datetime.datetime.now(datetime.timezone.utc)
        since = (now - datetime.timedelta(days=fish.KNOWLEDGE_GAP_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        audit = fish.read_tool_call_audit(data)
        for cand in (label, "anyi-studio-开发A-2", "self:fish外部评估", "dsh-main"):
            resolved = fish._resolve_profile_session(
                conn, audit, session_actor=cand, citizen_id=actor[0])
            scopes = fish._candidate_scopes(conn, audit, resolved["label"],
                                            resolved["session"]["citizen_id"])
            gaps = fish._knowledge_gaps(conn, scopes, resolved["label"], since, 2)
            print(f"  标签={cand!r} → label={resolved['label']!r} "
                  f"范围={[s['basis'] for s in scopes]} 命中={[g['code'] for g in gaps]}")

        pack, meta = fish.build_injection_pack(
            conn, data, actor[0], project=PROJECT, session_actor=label)
        print("\n================ 注入包（生产库副本 · 节选 ③④）================")
        lines = pack.splitlines()
        out, keep = [], False
        for line in lines:
            if line.startswith(("本项目规矩（", "全局规矩：", "该注意什么")):
                keep = True
            elif line.startswith(("项目：", "今日编码：", "常用标签：", "衰减(天)", "【操作指引】")):
                keep = False
            if keep:
                out.append(line)
        print("\n".join(out) if out else "（③④ 两段都没出现——见下面 meta 与数据盘点）")

        print("\n================ 完整注入包 ================")
        print(pack)

        print("================ meta（关键字段）================")
        for key in ("tokens", "budget", "over_budget", "layers", "trimmed",
                    "project", "project_source", "rules_codes", "attention_codes",
                    "recent_codes", "index_codes"):
            print(f"  {key} = {json.dumps(meta.get(key), ensure_ascii=False)}")

        print("\n================ 数据盘点（为何这两段长这样）================")
        rows = conn.execute(
            "SELECT owner,COUNT(*) FROM code_index WHERE mem_type='rule' "
            "AND status='active' AND dormant=0 GROUP BY owner").fetchall()
        print("  rule 的 owner（作者）分布：", [(r[0], r[1]) for r in rows],
              "← 只作诊断：规矩分栏**不看作者**")
        rows = conn.execute(
            "SELECT project,COUNT(*) FROM code_index WHERE mem_type='rule' "
            "AND status='active' AND dormant=0 GROUP BY project").fetchall()
        print("  rule 的 project（适用范围）分布：", [(r[0], r[1]) for r in rows])
        n = conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE mem_type='rule' AND status='active' "
            "AND dormant=0 AND project IS NULL").fetchone()[0]
        print(f"  全局规矩（project IS NULL）：{n} 条"
              + ("（0 → 「全局规矩」栏不出现，正常态）" if n == 0 else ""))
        since = (now - datetime.timedelta(days=fish.ATTENTION_LESSON_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT project,COUNT(*),MAX(recorded_at) FROM code_index "
            "WHERE mem_type='lesson' AND status='active' AND dormant=0 "
            "AND recorded_at >= ? GROUP BY project ORDER BY MAX(recorded_at) DESC",
            (since,)).fetchall()
        print(f"  近 {fish.ATTENTION_LESSON_WINDOW_DAYS} 天 lesson 按项目：",
              [(r[0], r[1], r[2]) for r in rows])
        n = conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE mem_type='lesson' AND status='active' "
            "AND dormant=0 AND recorded_at >= ?", (since,)).fetchone()[0]
        print(f"  lesson 合计 {n} 条；B 类口径 = 同项目优先取 top 2，不足用跨项目补齐")
        n = conn.execute("SELECT COUNT(*) FROM memory_access_log").fetchone()[0]
        print(f"  memory_access_log 行数：{n}")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
