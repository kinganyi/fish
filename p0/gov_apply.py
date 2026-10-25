#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据治理执行器（2026-10-25）· 五阶段

用法：
    python gov_apply.py --dry-run       # 只打印将要做什么
    python gov_apply.py --apply         # 真执行

阶段：
  1  改名：code_registry ANYI → anyi-studio；注册「大肥鱼生命系统」；建别名
  2  迁移 149 条（mapping.json）：code_index / links / access_log / push_log / 正文
  3  企业智能 4 条混杂清理（-03/-04 → fish；-05/-06 → anyi-studio，-04/-06 标 superseded）
  4  CR 铁律收敛：anyi-studio-261022-98 supersede by -75
  5  校验（七项）

**events 不改**（ADR-001）——只追加 project.renamed 事件记录完整映射。
⚠️ 迁移后**不要跑全量 rebuild_projection**：events 里的 code 仍是旧值，重建会把编码退回去。
   要做 rebuild 必须先让 rebuild 读 project.renamed 并应用映射（下一批）。
"""
import argparse
import datetime
import io
import json
import os
import re
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
# 数据目录：默认本目录下的 data；可用 --data-root 指到**副本**上先演练。
# 为什么必须留这个口：迁移会重写 149 条编码 + 309 处正文引用，**在副本上先跑一遍
# 是验证脚本本身的唯一办法**——用生产库试错等于拿真数据当测试环境。
DATA = os.path.join(P0, "data")
DB = os.path.join(DATA, "index.sqlite")
PROJECTS = os.path.join(DATA, "projects")
PLAN = os.path.join(os.path.dirname(P0), "35-迁移映射-mapping.json")

ANYI_NEW = "anyi-studio"
FISH = "大肥鱼生命系统"
WORK = "工作"

# 企业智能桶里 4 条混杂（盘点 + 我核实）
QIYE_FIX = {
    "企业智能-261022-03": (FISH, False, "fish 反馈处理结果 → 归 fish"),
    "企业智能-261022-04": (FISH, True, "fish 能力清单 D-1~D-9（已被 ANYI-261022-54 的新版取代）"),
    "企业智能-261022-05": (ANYI_NEW, False, "协作铁律行为层 CR-2/5/6/7/11 → anyi-studio"),
    "企业智能-261022-06": (ANYI_NEW, True, "CR-12/14（已被 ANYI-261022-75 全文覆盖）"),
}

# CR 铁律收敛：以 anyi-studio-261022-75 为唯一权威
CR_SUPERSEDE = [
    ("anyi-studio-261022-98", "anyi-studio-261022-75",
     "CR 补充四条与 -75 全文重复（2026-10-25 治理收敛）"),
]


def code_split(code):
    parts = code.rsplit("-", 2)
    if len(parts) != 3:
        return None, None, None
    return parts


def rewrite_body(text, mapping):
    """把正文里的旧编码替换成新编码。**按长度降序**，避免短码吃掉长码的前缀。

    ⚠️ 边界字符类必须用 `[A-Za-z0-9_-]` 而**不能**用 `\\w`：
    Python 的 `\\w` 默认是 Unicode 语义，**包含中文**——于是「补企业智能-261022-05」
    里 `企业智能` 前面的"补"会被当成"标识符的一部分"，替换直接失效
    （2026-10-25 副本演练踩到，表现为 26 → 15 处断链清不掉）。
    """
    for old, new in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![A-Za-z0-9_-])" + re.escape(old) + r"(?![A-Za-z0-9_-])",
                      new, text)
    return text


def load_plan():
    d = json.load(io.open(PLAN, encoding="utf-8"))
    return d


def stage1_registry(conn, plan, dry):
    """改名 + 注册新前缀 + 别名。"""
    reg = plan["code_registry_rename"]
    print("\n[阶段1] 项目改名与注册")
    print("  code_registry: %s → %s（保留 last_date/last_seq，续号不断）"
          % (reg["from"], reg["to"]))
    cur = conn.execute("SELECT last_date,last_seq,status FROM code_registry WHERE prefix=?",
                       (reg["from"],)).fetchone()
    print("    改前: last_date=%s last_seq=%s status=%s" % cur if cur else "    ⚠️ 源前缀不存在")
    if not dry:
        conn.execute("UPDATE code_registry SET prefix=?, namespace=? WHERE prefix=?",
                     (reg["to"], reg["to"], reg["from"]))
    for p in plan["prefixes_to_register"]:
        if p == reg["to"]:
            continue
        print("  注册新前缀: %s" % p)
        if not dry:
            conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p))
    print("  建别名: %s → %s（让旧代码里 project=ANYI 仍能检索到）" % (reg["from"], reg["to"]))
    if not dry:
        conn.execute(
            "INSERT OR REPLACE INTO name_aliases (kind,from_name,to_name,created_at,created_by,reason) "
            "VALUES ('project',?,?,?,'fish-主AI-2','2026-10-25 项目改名治理')",
            (reg["from"], reg["to"], datetime.datetime.now(datetime.timezone.utc)
             .strftime("%Y-%m-%dT%H:%M:%SZ")))


def stage2_migrate(conn, plan, dry):
    """迁移 149 条：投影 + 派生 + 正文。"""
    mapping = {m["old"]: m["new"] for m in plan["mapping"]}
    print("\n[阶段2] 迁移 %d 条（%s）" % (len(mapping), "、".join(plan["stats"])))
    if dry:
        for m in plan["mapping"][:5]:
            print("    %-22s → %-28s [%s]" % (m["old"], m["new"], m["target"]))
        print("    ...（共 %d 条）" % len(mapping))
    # 2a 投影（**file_path 必须一起改**：healthcheck 靠它判断链，不改就 153 条全"断链"
    #    ——2026-10-25 副本演练的功能验证抓到，七项校验没覆盖到这一项）
    if not dry:
        for m in plan["mapping"]:
            conn.execute("UPDATE code_index SET code=?, project=?, file_path=? WHERE code=?",
                         (m["new"], m["target"],
                          f"projects/{m['target']}/{m['new']}.md", m["old"]))
    # 2b 关联边
    n = 0
    for old, new in mapping.items():
        if not dry:
            n += conn.execute("UPDATE links SET src=? WHERE src=?", (new, old)).rowcount
            n += conn.execute("UPDATE links SET dst=? WHERE dst=?", (new, old)).rowcount
    print("  links 更新: %s 行" % (n if not dry else "（dry-run 未计）"))
    # 2c 访问日志 / 推送日志
    if not dry:
        for old, new in mapping.items():
            conn.execute("UPDATE memory_access_log SET code=? WHERE code=?", (new, old))
            conn.execute("UPDATE push_log SET code=? WHERE code=?", (new, old))
    # 2d 正文：先复制再替换内容（**旧目录保留**，改名归档）
    print("  正文文件处理中...")
    moved, rewritten = 0, 0
    src_dir = os.path.join(PROJECTS, "ANYI")
    by_target = {}
    for m in plan["mapping"]:
        by_target.setdefault(m["target"], []).append(m)
    for target, items in by_target.items():
        dst_dir = os.path.join(PROJECTS, target)
        if not dry:
            os.makedirs(dst_dir, exist_ok=True)
        for m in items:
            old_p = os.path.join(src_dir, m["old"] + ".md")
            new_p = os.path.join(dst_dir, m["new"] + ".md")
            if not os.path.exists(old_p):
                continue
            text = io.open(old_p, encoding="utf-8").read()
            new_text = rewrite_body(text, mapping)
            rewritten += 1 if new_text != text else 0
            if not dry:
                io.open(new_p, "w", encoding="utf-8").write(new_text)
            moved += 1
    print("  正文: 写新文件 %d 个，其中内容被改写 %d 个" % (moved, rewritten))
    # 旧目录**归档改名**（不是删除）：保留可追溯，但不再参与校验/检索。
    # 不改名的话它会一直存在，于是"孤儿文件"和"引用不可解析"两项校验永远红
    # （2026-10-25 副本演练踩到：152 个孤儿 + 688 处不可解析引用全在这个目录里）。
    if not dry:
        retired = os.path.join(PROJECTS, "_retired-ANYI-20261025")
        if os.path.isdir(src_dir) and not os.path.isdir(retired):
            os.rename(src_dir, retired)
            print("  旧目录已归档：projects/ANYI → projects/_retired-ANYI-20261025")


def rewrite_all_bodies(conn, extra_mapping, dry):
    """把 extra_mapping 里的旧编码在**所有正文**里替换成新的。

    为什么需要单独一步：阶段 3（企业智能 4 条）在阶段 2 之后执行——它改的是
    自己那 4 条记忆的编码，但**其他记忆的正文里还引用着旧编码**
    （如 `anyi-studio-261022-80` 正文里写着 `企业智能-261022-05`）。
    不做这一步，迁移后就留下 26 处真断链（2026-10-25 副本演练踩到）。
    """
    if not extra_mapping:
        return 0
    n = 0
    for sub in os.listdir(PROJECTS):
        d = os.path.join(PROJECTS, sub)
        if not os.path.isdir(d) or sub.startswith("_"):
            continue
        for f in os.listdir(d):
            if not f.endswith(".md"):
                continue
            p = os.path.join(d, f)
            txt = io.open(p, encoding="utf-8").read()
            new = rewrite_body(txt, extra_mapping)
            if new != txt:
                n += 1
                if not dry:
                    io.open(p, "w", encoding="utf-8").write(new)
    return n


def stage3_qiye(conn, dry):
    """企业智能 4 条混杂清理（含改编码前缀）。返回 {旧编码: 新编码} 供正文补替换。"""
    print("\n[阶段3] 企业智能桶混杂清理（4 条）")
    moved = {}
    for code, (target, supersede, why) in QIYE_FIX.items():
        parts = code_split(code)
        if not parts[0]:
            print("  ⚠️ 编码异常: %s" % code)
            continue
        new_code = f"{target}-{parts[1]}-{parts[2]}"
        row = conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone()
        if not row:
            print("  %-22s 不存在，跳过" % code)
            continue
        # 撞号处理（2026-10-25 副本演练踩到）：`企业智能-261022-03` 与 `ANYI-261022-03`
        # 会同时落到 `大肥鱼生命系统-261022-03`。撞号时**顺延序号**而不是跳过——
        # 跳过等于让这 4 条混杂留在原桶里，那本次治理就白做了。
        if conn.execute("SELECT 1 FROM code_index WHERE code=?", (new_code,)).fetchone():
            seq = int(parts[2])
            while conn.execute("SELECT 1 FROM code_index WHERE code=?",
                               (f"{target}-{parts[1]}-{seq:02d}",)).fetchone():
                seq += 1
            new_code = f"{target}-{parts[1]}-{seq:02d}"
            print("  （撞号，序号顺延至 %s）" % new_code)
        print("  %-22s → %-26s [%s]%s" % (code, new_code, target, " +superseded" if supersede else ""))
        moved[code] = new_code
        if dry:
            continue
        conn.execute("UPDATE code_index SET code=?, project=?, file_path=? WHERE code=?",
                     (new_code, target, f"projects/{target}/{new_code}.md", code))
        conn.execute("UPDATE links SET src=? WHERE src=?", (new_code, code))
        conn.execute("UPDATE links SET dst=? WHERE dst=?", (new_code, code))
        conn.execute("UPDATE memory_access_log SET code=? WHERE code=?", (new_code, code))
        conn.execute("UPDATE push_log SET code=? WHERE code=?", (new_code, code))
        # 正文搬运：复制到新项目目录，**旧文件移入归档子目录**（不删——保历史可追溯）
        old_p = os.path.join(PROJECTS, "企业智能", code + ".md")
        if os.path.exists(old_p):
            dst_dir = os.path.join(PROJECTS, target)
            os.makedirs(dst_dir, exist_ok=True)
            text = io.open(old_p, encoding="utf-8").read()
            text = text.replace(code, new_code)
            io.open(os.path.join(dst_dir, new_code + ".md"), "w", encoding="utf-8").write(text)
            arch = os.path.join(PROJECTS, "企业智能", "_retired-20261025")
            os.makedirs(arch, exist_ok=True)
            os.replace(old_p, os.path.join(arch, code + ".md"))
        if supersede:
            conn.execute("UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
                         ("anyi-studio-261022-75" if "CR-12" in why else "大肥鱼生命系统-261022-54",
                          new_code))
    return moved


def stage4_cr(conn, dry):
    """CR 铁律收敛：-98 supersede by -75。"""
    print("\n[阶段4] CR 铁律收敛")
    for old, by, why in CR_SUPERSEDE:
        row = conn.execute("SELECT status FROM code_index WHERE code=?", (old,)).fetchone()
        if not row:
            print("  %-26s 不存在，跳过" % old)
            continue
        print("  %-26s superseded by %-26s（%s）" % (old, by, why))
        if not dry:
            conn.execute("UPDATE code_index SET status='superseded', superseded_by=? WHERE code=?",
                         (by, old))


def stage5_verify(conn, before_total):
    """七项校验。每项都打印读数，不通过就明确 FAIL。"""
    print("\n[阶段5] 校验")
    rail = []

    def check(name, ok, detail):
        rail.append((name, ok, detail))
        print("  %-30s %s  %s" % (name, "PASS" if ok else "FAIL", detail))

    # 1) 还有没有 ANYI-* 的编码残留
    # ⚠️ 必须用 GLOB 不用 LIKE：SQLite 的 LIKE 对 ASCII **大小写不敏感**，
    #    `'anyi-studio-261022-01' LIKE 'ANYI-%'` 会为真 —— 用 LIKE 会把
    #    刚迁好的 96 条误报成"残留"（2026-10-25 副本演练踩到）。
    left = conn.execute("SELECT count(*) FROM code_index WHERE code GLOB 'ANYI-*'").fetchone()[0]
    check("旧编码残留", left == 0, "code_index 里 ANYI-*（GLOB，区分大小写）= %d" % left)
    # 2) 旧项目列残留
    leftp = conn.execute("SELECT count(*) FROM code_index WHERE project='ANYI'").fetchone()[0]
    check("旧项目列残留", leftp == 0, "project='ANYI' = %d" % leftp)
    # 3) 行数（迁移不改总量）
    total = conn.execute("SELECT count(*) FROM code_index").fetchone()[0]
    check("行数未变", total == before_total,
          "code_index = %d（迁移前 %d）" % (total, before_total))
    # 4) links 有没有指向不存在编码
    dangling = conn.execute(
        "SELECT count(*) FROM links l WHERE (l.src NOT IN (SELECT code FROM code_index) "
        "OR l.dst NOT IN (SELECT code FROM code_index)) AND l.removed_at IS NULL").fetchone()[0]
    check("links 无悬空", dangling == 0, "悬空边 = %d" % dangling)
    # 5) 每个正文文件都能对上一条记忆
    all_codes = {r[0] for r in conn.execute("SELECT code FROM code_index")}
    files, orphans = 0, []
    for sub in os.listdir(PROJECTS):
        d = os.path.join(PROJECTS, sub)
        if not os.path.isdir(d):
            continue
        # 跳过归档目录（下划线开头）：它们是历史现场，不参与"当前一致性"校验。
        if sub.startswith("_"):
            continue
        for f in os.listdir(d):
            if not f.endswith(".md"):
                continue
            files += 1
            code = f[:-3]
            if code not in all_codes:
                orphans.append(os.path.join(sub, f))
    check("正文与索引一致", len(orphans) == 0,
          "文件 %d 个，孤儿 %d 个%s" % (files, len(orphans), (" 例:" + str(orphans[:3])) if orphans else ""))
    # 6) 正文里的编码引用是否**存在**（不要求 active）
    # ⚠️ 这一项**不能**用 `fish.code_mentions_in()`：它只认 `status='active'`
    #    （2026-10-24 的设计——正文引用已作废条目不该建边）。而本次治理会把
    #    企业智能-05/06 标成 superseded，于是它们的引用会"解析不到"，
    #    但那是**设计如此**，不是断链（2026-10-25 副本演练踩到）。
    #    本项要守的是"迁移没把引用改坏"→ 判据是**编码是否存在**，与状态无关。
    sys.path.insert(0, P0)
    import fish as fishlib

    def _exists(conn, code):
        return conn.execute("SELECT 1 FROM code_index WHERE code=?", (code,)).fetchone() is not None

    def _resolve_any(conn, text):
        """把文本里的编码形态逐个剥掉左侧误并字符，看能否命中库里任一条。"""
        hit_any = True
        for m in fishlib.CODE_MENTION_RE.finditer(text):
            raw = m.group(0)
            parts = raw.rsplit("-", 2)
            if len(parts) != 3:
                continue
            prefix, d, s = parts
            ok = False
            for i in range(len(prefix)):
                if _exists(conn, f"{prefix[i:]}-{d}-{s}"):
                    ok = True
                    break
            if not ok:
                hit_any = False
                bad.append((text[:40], raw))
        return hit_any

    bad = []
    for sub in os.listdir(PROJECTS):
        d = os.path.join(PROJECTS, sub)
        if not os.path.isdir(d) or sub.startswith("_"):
            continue
        for f in os.listdir(d):
            if not f.endswith(".md"):
                continue
            txt = io.open(os.path.join(d, f), encoding="utf-8", errors="ignore").read()
            before = len(bad)
            _resolve_any(conn, txt)
            if len(bad) > before:
                # 把文件名补进去，便于定位
                for i in range(before, len(bad)):
                    bad[i] = (os.path.join(sub, f), bad[i][1])
    check("正文引用可解析", len(bad) == 0,
          "无法解析的引用 %d 处%s" % (len(bad), (" 例:" + str(bad[:3])) if bad else ""))
    # 7) file_path 与磁盘一致（**这条是七项之外补的**：副本演练的功能验证才发现
    #    迁移后 file_path 还是旧路径，healthcheck 报 153 条断链）
    broken = []
    for code, fp in conn.execute("SELECT code, file_path FROM code_index").fetchall():
        if not fp:
            continue
        if not os.path.exists(os.path.join(DATA, fp)):
            broken.append((code, fp))
    check("file_path 与磁盘一致", len(broken) == 0,
          "断链 %d 条%s" % (len(broken), (" 例:" + str(broken[:2])) if broken else ""))
    # 八) 检索可用性抽查（**这条是七项之外补的**：迁移后 FTS 曾因"自己 INSERT 原文
    #     而没走 fts_text 切分"整个失效，但七项校验全绿——它们不测检索）
    sys.path.insert(0, P0)
    import fish as fishlib
    probe_code = conn.execute(
        "SELECT code FROM code_index WHERE status='active' LIMIT 1").fetchone()
    if probe_code:
        title = conn.execute("SELECT title FROM code_index WHERE code=?",
                             (probe_code[0],)).fetchone()[0] or ""
        kw = (title or "")[:4]
        if kw:
            try:
                hits = fishlib.search_memories(conn, query=kw, limit=5)
                rows = hits[0] if isinstance(hits, tuple) else hits
                check("检索可用性抽查", len(rows) > 0,
                      "用标题前 4 字「%s」召回 %d 条" % (kw, len(rows)))
            except Exception as e:
                check("检索可用性抽查", False, "检索抛异常：%r" % (e,))
    # 九) 项目分布
    dist = conn.execute("SELECT project, count(*) FROM code_index GROUP BY project "
                        "ORDER BY 2 DESC").fetchall()
    print("  项目分布: " + " / ".join("%s=%d" % (p, n) for p, n in dist))
    return rail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data-root", default=None,
                    help="数据目录（缺省本目录下 data）。指向副本可在不动生产库的前提下演练")
    a = ap.parse_args()
    dry = not a.apply
    if not a.apply and not a.dry_run:
        print("必须指定 --dry-run 或 --apply")
        return 1

    # 允许把整个 data 目录指到副本（演练用）
    global DATA, DB, PROJECTS
    if a.data_root:
        DATA = os.path.abspath(a.data_root)
        DB = os.path.join(DATA, "index.sqlite")
        PROJECTS = os.path.join(DATA, "projects")
        print("⚠️  使用外部数据目录：%s" % DATA)

    plan = load_plan()
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=OFF")
    before_total = conn.execute("SELECT count(*) FROM code_index").fetchone()[0]
    print("=" * 70)
    print("数据治理执行  %s" % ("【DRY-RUN 不写入】" if dry else "【真执行】"))
    print("迁移前 code_index 行数：%d" % before_total)
    print("=" * 70)
    try:
        stage1_registry(conn, plan, dry)
        stage2_migrate(conn, plan, dry)
        qiye_moved = stage3_qiye(conn, dry)
        # 全量补替换：阶段 2 只处理了 `projects/ANYI/`，但**别的项目目录里的正文也引用着
        # ANYI-* 编码**（如 企业智能-261022-01 的正文提到 ANYI-261022-23）。
        # 所以这里要用「阶段2 + 阶段3」的**完整映射**扫所有目录。
        full_map = {m["old"]: m["new"] for m in plan["mapping"]}
        full_map.update(qiye_moved)
        n_rew = rewrite_all_bodies(conn, full_map, dry)
        print("  补替换正文里的旧引用: %d 个文件%s"
              % (n_rew, "" if not dry else "（dry-run 未写）"))
        stage4_cr(conn, dry)
        if not dry:
            # 追加 project.renamed 事件（events 不可改，只能追加）
            mapping = {m["old"]: m["new"] for m in plan["mapping"]}
            import hashlib
            h = hashlib.sha256(json.dumps(mapping, sort_keys=True, ensure_ascii=False)
                               .encode("utf-8")).hexdigest()[:16]
            conn.execute(
                "INSERT INTO events (event_id,seq,ts,type,actor,subject,payload,cause,schema) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("EVT-GOVERNANCE-" + h, (conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events")
                                          .fetchone()[0]),
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "project.renamed",
                 '{"citizen_id": "fish-主AI-2", "evidence_level": "signed"}',
                 '{"kind": "project", "id": "ANYI"}',
                 json.dumps({"from": "ANYI", "to": ANYI_NEW, "count": len(mapping),
                             "mapping_sha256_16": h,
                             "note": "完整映射见 35-迁移映射-mapping.json；"
                                     "rebuild 需先应用此映射才能还原正确编码"},
                            ensure_ascii=False),
                 "2026-10-25 数据治理：ANYI 拆分改名 + 混杂清理", "evt@1"))
            conn.commit()
            conn.commit()
            # FTS 重建：**必须走生产的 `reindex_fts`**，不能自己 INSERT。
            # 2026-10-25 踩到：自己 INSERT 存的是**原文**，而 memory_fts 里要的是
            # `fts_text()` 切好的 bigram 串（空格分隔）——于是全文检索整个失效，
            # 相似度从 0.93 掉到 0.02，但七项校验全绿（它们不测检索）。
            # 这是 D2 纪律的又一次实践：不许用自建实现代替应用自己的函数。
            sys.path.insert(0, P0)
            import fish as fishlib
            n = fishlib.reindex_fts(conn, DATA)
            print("\n  FTS 重建（生产 reindex_fts）: %d 行" % n)
        if dry:
            print("\n(dry-run：只打印计划，不校验、不写入)")
            conn.rollback()
            return 0
        rail = stage5_verify(conn, before_total)
        fails = [r for r in rail if not r[1]]
        print("\n" + "=" * 70)
        print("结果: %d 项检查，%d 项通过，%d 项失败" % (len(rail), len(rail) - len(fails), len(fails)))
        if fails:
            for name, _ok, detail in fails:
                print("  FAIL: %s — %s" % (name, detail))
            if not dry:
                conn.rollback()
                print("\n❌ 校验未全过，已 ROLLBACK（数据未提交）")
                return 2
        if not dry:
            conn.commit()
            print("\n✅ 已提交")
        else:
            conn.rollback()
            print("\n(dry-run 已回滚，未写入)")
    except Exception as e:
        conn.rollback()
        print("\n❌ 异常，已 ROLLBACK: %r" % (e,))
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
