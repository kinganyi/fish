#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补迁「改名之后又被写进 ANYI 的记忆」（2026-10-25 晚）。

## 发生了什么

2026-10-25 04:20 我执行了数据治理：`code_registry.prefix` 从 `ANYI` 改成 `anyi-studio`。
但 **08:07 有另一个会话执行了 `add_project ANYI`**（它不知道改名的事），
于是 `ANYI` 重新出现在 registry 里（`last_seq` 从 0 开始），
接着写了 4 条 `ANYI-261025-01~04`。

**这 4 条复用了"腾出来的编码"**：旧的 `ANYI-261025-01` 已迁成 `anyi-studio-261025-01`，
主键腾空，新写入正好占了它——于是同一个编码在时间上被用了两次（内容不同）。

## 怎么处理

1. 按内容判定归属（都是「审查A-2」的工作 → anyi-studio）
2. 分配**不冲突**的新序号（从该前缀当日最大序号顺延）
3. 搬迁正文 + 更新 file_path / links / access_log / push_log
4. 把 `ANYI` 前缀标 **closed**——它不该再接收新写入，但保留可检索（闭合 ≠ 删除）
5. 追加 `code.renamed.batch` 补充事件，让 rebuild 也能应用这批映射
"""
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(P0, "data")
DB = os.path.join(DATA, "index.sqlite")
PROJECTS = os.path.join(DATA, "projects")

TARGET = "anyi-studio"


def next_free(conn, prefix, date_part, used=()):
    """该前缀该日期的下一个空闲序号（现有最大 +1，且避开 `used` 里本轮已分配的）。"""
    rows = conn.execute(
        "SELECT code FROM code_index WHERE code GLOB ?", (f"{prefix}-{date_part}-*",)).fetchall()
    mx = 0
    for (c,) in rows:
        parts = c.rsplit("-", 2)
        if len(parts) == 3 and parts[1] == date_part and parts[2].isdigit():
            mx = max(mx, int(parts[2]))
    seq = mx + 1
    while (f"{prefix}-{date_part}-{seq:02d}" in used
           or conn.execute("SELECT 1 FROM code_index WHERE code=?",
                           (f"{prefix}-{date_part}-{seq:02d}",)).fetchone()):
        seq += 1
    return f"{prefix}-{date_part}-{seq:02d}"


def main():
    global DATA, DB, PROJECTS
    # --data-root：指到副本上先演练（同 gov_apply 的纪律）
    if "--data-root" in sys.argv:
        i = sys.argv.index("--data-root")
        DATA = os.path.abspath(sys.argv[i + 1])
        DB = os.path.join(DATA, "index.sqlite")
        PROJECTS = os.path.join(DATA, "projects")
        print("⚠️  使用外部数据目录：%s" % DATA)
    con = sqlite3.connect(DB)
    out = []
    try:
        rows = con.execute(
            "SELECT code,title,status,file_path FROM code_index WHERE code GLOB 'ANYI-*' "
            "ORDER BY code").fetchall()
        if not rows:
            print("没有需要补迁的记录")
            return 0
        print("发现 %d 条改名后写入的记录：" % len(rows))
        mapping = {}
        # ⚠️ 必须用 `used` 记录**本轮已分配**的序号：`next_free` 只看库里已有的，
        # 而库要到 UPDATE 时才变——不记 used 的话四条会全拿到同一个序号，
        # 然后在 UPDATE 时撞 UNIQUE 约束（2026-10-25 第一次演练就是这么崩的）。
        used = set()
        for code, title, status, fp in rows:
            parts = code.rsplit("-", 2)
            date_part = parts[1]
            new_code = next_free(con, TARGET, date_part, used)
            used.add(new_code)
            mapping[code] = new_code
            print("  %-18s → %-28s %s" % (code, new_code, (title or "")[:36]))

        # 1) 数据迁移
        for old, new in mapping.items():
            con.execute("UPDATE code_index SET code=?, project=?, file_path=? WHERE code=?",
                        (new, TARGET, f"projects/{TARGET}/{new}.md", old))
            con.execute("UPDATE links SET src=? WHERE src=?", (new, old))
            con.execute("UPDATE links SET dst=? WHERE dst=?", (new, old))
            con.execute("UPDATE memory_access_log SET code=? WHERE code=?", (new, old))
            con.execute("UPDATE push_log SET code=? WHERE code=?", (new, old))

        # 2) 正文搬迁（复制到新目录 + 内容替换 + 旧文件归档）
        src_dir = os.path.join(PROJECTS, "ANYI")
        dst_dir = os.path.join(PROJECTS, TARGET)
        os.makedirs(dst_dir, exist_ok=True)
        for old, new in mapping.items():
            p = os.path.join(src_dir, old + ".md")
            if os.path.exists(p):
                txt = io.open(p, encoding="utf-8").read().replace(old, new)
                io.open(os.path.join(dst_dir, new + ".md"), "w", encoding="utf-8").write(txt)
        # 旧目录整体归档（如果空了就删，非空则改名保留）
        if os.path.isdir(src_dir):
            arch = os.path.join(PROJECTS, "_retired-ANYI-late-20261025")
            if not os.path.isdir(arch):
                os.rename(src_dir, arch)
                print("  projects/ANYI 归档为 _retired-ANYI-late-20261025")

        # 3) ANYI 前缀标 closed（不再接收新写入；闭合 ≠ 删除，可 reopen）
        n = con.execute(
            "UPDATE code_registry SET status='closed' WHERE prefix='ANYI' AND status!='closed'"
        ).rowcount
        print("  ANYI 前缀状态 → closed（影响 %d 行）" % n)

        # 4) 追加补充映射事件（让 rebuild 也能应用这批）
        con.commit()
        import hashlib
        h = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False)
                           .encode("utf-8")).hexdigest()
        eid = "EVT-CODE-RENAMED-LATE-" + h[:16]
        if not con.execute("SELECT 1 FROM events WHERE event_id=?", (eid,)).fetchone():
            seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, seq, ts, "code.renamed.batch",
                 '{"citizen_id": "fish-主AI-2", "evidence_level": "signed"}',
                 '{"kind": "project", "id": "ANYI"}',
                 json.dumps({"mapping": mapping, "count": len(mapping),
                             "note": "改名后又被写进 ANYI 的补迁"}, ensure_ascii=False),
                 "补迁：ANYI 改名后重新建前缀导致的 4 条", "evt@1"))
            print("  已追加补充映射事件 %s（%d 条）" % (eid, len(mapping)))
        con.commit()

        # 5) FTS 用生产函数重建
        sys.path.insert(0, P0)
        import fish as fishlib
        n_fts = fishlib.reindex_fts(con, DATA)
        print("  FTS 重建：%d 条" % n_fts)

        # 6) 校验
        print("\n校验：")
        left = con.execute("SELECT count(*) FROM code_index WHERE code GLOB 'ANYI-*'").fetchone()[0]
        print("  ANYI-* 残留: %d %s" % (left, "PASS" if left == 0 else "FAIL"))
        ap = con.execute("SELECT count(*) FROM code_index WHERE project='ANYI'").fetchone()[0]
        print("  project='ANYI' 残留: %d %s" % (ap, "PASS" if ap == 0 else "FAIL"))
        total = con.execute("SELECT count(*) FROM code_index").fetchone()[0]
        print("  总行数: %d（补迁前 210）" % total)
        broken = [c for c, fp in con.execute("SELECT code,file_path FROM code_index") if fp
                  and not os.path.exists(os.path.join(DATA, fp))]
        print("  file_path 断链: %d %s" % (len(broken), "PASS" if not broken else "FAIL"))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
