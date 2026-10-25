#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据治理 · 迁移计划生成器（**只读，不写任何数据**）

职责：把「ANYI 里的每条记忆该去哪个项目」算出来，落成 `mapping.json`。
执行迁移是另一个脚本（`gov_apply.py`）的事——**先审计划，再动数据**。

分类来源（按优先级）：
  1. 用户 2026-10-25 明确指定的 4 条（见 USER_OVERRIDES）
  2. 会话名（anyi-studio-* → anyi-studio；fish* / dsh* → 大肥鱼生命系统）
  3. 标题关键词计数
  4. 正文关键词计数
  5. 兜底：跟随 ANYI 改名后的默认去向（anyi-studio），并在 reason 里标 low
"""
import io
import json
import os
import sqlite3
import sys

P0 = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(P0, "data")
DB = os.path.join(DATA, "index.sqlite")
PROJECTS = os.path.join(DATA, "projects")
OUT = os.path.join(os.path.dirname(P0), "35-迁移映射-mapping.json")

ANYI = "anyi-studio"
FISH = "大肥鱼生命系统"
WORK = "工作"

# 用户 2026-10-25 04:22 明确指定
USER_OVERRIDES = {
    "ANYI-261024-13": ANYI,
    "ANYI-261022-21": WORK,
    "ANYI-261022-22": WORK,
    "ANYI-261022-11": FISH,
    # ── 以下三条是**我审计划时发现的自动分类误判**（2026-10-25 04:25）──
    # 判据：这次治理的目的就是"消除混杂"，把业务内容判进 fish 等于制造新的混杂。
    "ANYI-261022-20": ANYI,   # 「把业务码当 HTTP 码判，积分不足变永久锁死」= anyi-studio 业务判据
    "ANYI-261022-25": ANYI,   # 「主图 AI 效率数据：两套对外口径」= anyi-studio 业务内容
    "ANYI-261022-45": FISH,   # 「owner 归一验证」= owner 是 fish 的概念（自动分类走了"跟随主体"兜底）
    "ANYI-261022-28": ANYI,   # 「飞书跳组织文档读写：第二应用 bot 身份」= anyi-studio 的集成业务
                              # （虽是 withdrawn，但作废条目的归属也要对——它会被 rebuild 重放）
}

FISH_KW = ["fish", "大肥鱼", "记忆系统", "注入包", "注入", "编码分配", "事件溯源", "衰减",
           "检索", "MCP", "hook", "身份", "凭证", "token", "推送", "push", "tool_",
           "工具表", "迁移", "工作空间索引", "全景", "错题本", "会话身份", "recall",
           "remember", "neighbors", "项目档期", "ADR", "基线", "判据", "测试"]
ANYI_KW = ["anyi-studio", "积分", "送审", "上线", "修复批", "生成防重", "模板", "审查A",
           "审查B", "ER-", "CR-", "EXP-", "PIT-", "协作铁律", "资金", "用户表", "支付",
           "PHP", "内测", "回执", "部署", "余额", "渠道", "多站点", "tenant", "paid_",
           "cost_", "task.validate"]


def body_of(code):
    p = os.path.join(PROJECTS, "ANYI", code + ".md")
    if os.path.exists(p):
        try:
            return io.open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            return ""
    return ""


def classify(code, title, sess, project):
    t, s = title or "", sess or ""
    if code in USER_OVERRIDES:
        return USER_OVERRIDES[code], "用户指定", "high"
    if "anyi-studio" in s or s.startswith("anyi-") or s == "codex-anyi":
        return ANYI, "会话=" + s, "high"
    if s.startswith("dsh") or "fish" in s:
        return FISH, "会话=" + (s or "空"), "high"
    a = sum(1 for k in ANYI_KW if k.lower() in t.lower())
    f = sum(1 for k in FISH_KW if k.lower() in t.lower())
    if a or f:
        if a > f:
            return ANYI, "标题 anyi%d:fish%d" % (a, f), "mid"
        if f > a:
            return FISH, "标题 anyi%d:fish%d" % (a, f), "mid"
    b = body_of(code)
    a = sum(1 for k in ANYI_KW if k.lower() in b.lower())
    f = sum(1 for k in FISH_KW if k.lower() in b.lower())
    if a or f:
        if a > f:
            return ANYI, "正文 anyi%d:fish%d" % (a, f), "low"
        if f > a:
            return FISH, "正文 anyi%d:fish%d" % (a, f), "low"
    # 兜底：ANYI 改名后的默认去向（不是"猜对"，是"跟主体走"）
    return ANYI, "无信号，跟随主体", "low"


def main():
    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT code, title, COALESCE(session_actor,''), status, mem_type "
        "FROM code_index WHERE project='ANYI' ORDER BY code").fetchall()

    # 目标前缀下已有的编码（避免撞号）
    existing = {r[0] for r in conn.execute("SELECT code FROM code_index")}
    prefixes = {r[0] for r in conn.execute("SELECT prefix FROM code_registry")}

    mapping, problems, stat = [], [], {}
    for code, title, sess, status, mt in rows:
        target, reason, conf = classify(code, title, sess, "ANYI")
        # 新编码 = 目标前缀 + 原日期 + 原序号（日期/序号不变，只换前缀）
        parts = code.rsplit("-", 2)
        if len(parts) != 3:
            problems.append({"code": code, "issue": "编码格式异常，无法解析"})
            continue
        _old_prefix, date_part, seq = parts
        new_code = f"{target}-{date_part}-{seq}"
        if new_code in existing:
            problems.append({"code": code, "issue": f"目标编码已存在：{new_code}"})
            continue
        mapping.append({
            "old": code, "new": new_code, "target": target,
            "title": title or "", "session": sess or "", "status": status,
            "mem_type": mt, "reason": reason, "confidence": conf,
        })
        stat[target] = stat.get(target, 0) + 1
        existing.add(new_code)

    # 目标前缀是否已注册
    need_prefixes = sorted({m["target"] for m in mapping} | {"anyi-studio"})
    to_register = [p for p in need_prefixes if p not in prefixes and p != "工作"]

    plan = {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_project": "ANYI",
        "note": "本文件是**迁移计划**，尚未执行任何写入。审完再跑 gov_apply.py。",
        "user_overrides": USER_OVERRIDES,
        "targets": {
            "anyi-studio": {"display": "anyi-studio", "note": "原 ANYI 改名"},
            "大肥鱼生命系统": {"display": "大肥鱼生命系统", "note": "fish 迁出（需新建）"},
            "工作": {"display": "工作", "note": "通用元纪律（已存在）"},
        },
        "prefixes_to_register": to_register,
        "code_registry_rename": {"from": "ANYI", "to": "anyi-studio"},
        "stats": stat,
        "total": len(mapping),
        "problems": problems,
        "mapping": mapping,
    }
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(plan, ensure_ascii=False, indent=2))

    print("计划已生成：", OUT)
    print("总数：%d" % len(mapping))
    for k, v in sorted(stat.items(), key=lambda kv: -kv[1]):
        print("  %-14s %d" % (k, v))
    print("需新注册的前缀：", to_register)
    print("问题：%d 条" % len(problems))
    for p in problems:
        print("  ", p)


if __name__ == "__main__":
    main()
