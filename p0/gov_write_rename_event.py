#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补写 `code.renamed.batch` 事件（含**完整映射**）——让 rebuild 能应用它。

为什么需要这一条：
  2026-10-25 的数据治理把 149+4 条编码改了（ANYI-* → anyi-studio-* / 大肥鱼生命系统-*），
  但 `events` 是 append-only（ADR-001），历史事件里存的仍是旧编码。
  于是 `rebuild_projection` 一重放就会把编码**退回旧值**——这是本轮留下的定时炸弹。

  已追加的 `project.renamed` 事件只带了 mapping 的 sha256（防篡改指纹），
  但**没有映射本体**，rebuild 无法据此还原。所以这里补一条带完整映射的事件：
  `code.renamed.batch`，payload.mapping = {旧: 新} 全表。

幂等：同 `event_id` 已存在则跳过。
"""
import datetime
import io
import json
import os
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(P0, "data")
DB = os.path.join(DATA, "index.sqlite")
PLAN = os.path.join(os.path.dirname(P0), "35-迁移映射-mapping.json")

# 企业智能那 4 条不在 mapping.json 里（它们由 gov_apply 阶段 3 现场决定，含撞号顺延）。
# 这里按 2026-10-25 执行报告 `36` §1 的实际结果硬编码——**它们是既成事实，不是猜测**。
QIYE_MOVED = {
    "企业智能-261022-03": "大肥鱼生命系统-261022-10",   # 撞号顺延
    "企业智能-261022-04": "大肥鱼生命系统-261022-12",   # 撞号顺延
    "企业智能-261022-05": "anyi-studio-261022-05",
    "企业智能-261022-06": "anyi-studio-261022-06",
}


def main():
    plan = json.load(io.open(PLAN, encoding="utf-8"))
    mapping = {m["old"]: m["new"] for m in plan["mapping"]}
    mapping.update(QIYE_MOVED)

    con = sqlite3.connect(DB)
    try:
        # 幂等：同一批映射只写一次
        import hashlib
        h = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False)
                           .encode("utf-8")).hexdigest()
        eid = "EVT-CODE-RENAMED-" + h[:16]
        exists = con.execute("SELECT 1 FROM events WHERE event_id=?", (eid,)).fetchone()
        if exists:
            print("已存在，跳过：%s（映射 %d 条）" % (eid, len(mapping)))
            return 0
        seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "project_from": "ANYI",
            "project_to": "anyi-studio",
            "other_targets": ["大肥鱼生命系统", "工作"],
            "count": len(mapping),
            "mapping": mapping,
            "note": "2026-10-25 数据治理的完整编码映射。"
                    "rebuild_projection 读这条事件把旧编码还原成新编码；"
                    "缺了它 = 重建后退回 ANYI-*（定时炸弹）。",
        }
        con.execute(
            "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, seq, ts, "code.renamed.batch",
             '{"citizen_id": "fish-主AI-2", "evidence_level": "signed"}',
             '{"kind": "project", "id": "ANYI"}',
             json.dumps(payload, ensure_ascii=False),
             "补写完整映射，让 rebuild 能应用（拆定时炸弹）", "evt@1"))
        con.commit()
        print("已写入 %s（seq=%d，映射 %d 条）" % (eid, seq, len(mapping)))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
