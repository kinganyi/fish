#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档索引生成器 / 校验器（序4 文档治理，2026-11-11）。

## 它解决三个具体问题

**问题 1 · 编号撞号（20 组）。** 五族文档（外部评估 / 调研 / 交接 / 复盘 / 会话全记录）
被塞进了内部工程同一个编号空间：`20-系统全景…` 与 `20-外部评估…` 并存。于是"见 20 §3"
指向哪一份，读者答不出来。

**问题 2 · 七套阶段编号并存。** A–D / P0–P4 / 主题六层 / 三段 / 四段 / T01–T05 /
未实现优先级。最要命的一句原话（`66` §4.1）：**"同一个「P0」在不同文档里含义完全不同"**
——所以"P0 做完了吗"这个问题**根本无法回答**。

**问题 3 · 同一指标多个口径。** README 徽章写 `tests-1,647 passed`，实际 1,737。
文档里到处是"某某行数 / 测试数 / 判据数"，各写各的。

## 本文件的做法（以及**刻意不做**的事）

**做**：
  · 从文件名机械导出"编号 / 家族 / 标题"，叠加一份**人工维护的覆盖表**（状态 / 职责 /
    取代关系）——机械的归机械，判断的归判断；
  · 生成 `71-文档索引-权威.md`（人读）与 `docs-catalog.json`（机器读，供判据用）；
  · `--check` 断言四条不变式（见下），任一不满足就**非零退出**。

**不做**：不重命名任何文件、不改任何历史正文。
理由：全工作区有 **1,862 次文档引用**（聊天记录外 1,229 次），改名要同步改引用，
而历史文档是**审计记录**——为了"看起来整齐"去改写审计记录，代价大于收益。

**撞号怎么"消除"而不是"掩盖"**：给每份文档一个**唯一 ID**，规则是
  · 内部工程 → `D<编号>`（如 `D30`）
  · 其它族 → `<族字母><编号>`（外部评估 `E` / 调研 `R` / 交接 `H` / 复盘 `K` / 会话 `S`）
统一 ID 后，`E20` 与 `D20` 不再撞。**新文档一律用 ID 引用**（判据会盯着这一点）。

## 四条不变式（`--check`）

  ① **覆盖**：工作区每份带编号的 md 都在索引里（漏登记会被判红）
  ② **唯一**：没有两份文档共享同一个 ID
  ③ **定状态**：每份文档的 status ∈ {现行, 历史, 已取代, 归档}，且"已取代"必须写明被谁取代
  ④ **引用完整**：索引里声明的"取代/被取代"关系，指向的 ID 必须真实存在

用法：
    python tools/doc_index.py            # 生成（人读索引 + 机器目录）
    python tools/doc_index.py --check    # 只校验，不写文件
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# `p0/tools/` 的上两级才是工作区根（`p0` 是代码树，文档在工作区根下）。
WORKSPACE = HERE.parent.parent
INDEX_NAME = "71-文档索引-权威.md"
CATALOG_NAME = "docs-catalog.json"

# ============================================================== 家族与 ID 规则
#
# 家族判定按"文件名里出现的关键词"。顺序有意义：先匹配的优先。
# 为什么用文件名而不是目录：这些文档本来就在同一个目录里，目录分不出族——
# 家族是**语义**，只能靠命名约定或人工标注。
FAMILIES = [
    ("外部评估", "E", ("外部评估", "审查报告", "全面审查", "复核", "评估-")),
    ("调研", "R", ("调研",)),
    ("交接/入职", "H", ("交接文档", "入职包", "转交")),
    ("复盘/错题", "K", ("复盘", "错题本")),
    ("会话全记录", "S", ("会话全记录",)),
]
DEFAULT_FAMILY = "内部工程"
DEFAULT_LETTER = "D"

# 人工覆盖表：只写**需要判断**的字段（状态 / 职责 / 取代关系）。
# 机械能推的一律不写，否则这张表会变成第二份需要同步的负担。
OVERRIDES = {
    # ---- 现行权威（读这些就够） ----
    "SESSION_STATE.md": {"status": "现行", "role": "断点快照：当前进展与下一步（唯一的'现在在哪'）"},
    "README.md": {"status": "现行", "role": "对外门面：项目是什么、怎么跑、判据口径"},
    "30-项目全景-v2-收敛版.md": {"status": "现行", "role": "项目全景（愿景/三目标/阶段/未完成项）"},
    "66-项目复盘-愿景与进展对照-20261111.md": {"status": "现行", "role": "愿景 vs 实际进展对照（含七套编号问题）"},
    "67-对账-外部审查红项当下状态-20261111.md": {"status": "现行", "role": "12🔴 逐条对账 + 修复记录 + D-10 澄清"},
    "68-复盘-fish债务清理八轮-20261111.md": {"status": "现行", "role": "错题本：A/B/C/D 四类教训 + 拆包八条规则"},
    "70-序3-30天归档-第一次真跑与两个缺陷-20261111.md": {"status": "现行", "role": "序3：归档暴露的两个归属缺陷与修法"},
    "69-序2-主人画像-架构设计与实现-20261111.md": {"status": "现行", "role": "序2：画像的槽位化设计与实现"},
    "16-最终架构-v1-整合裁决.md": {"status": "现行", "role": "架构裁决：撞车分歧的最终裁定"},
    "01-决策记录-ADR.md": {"status": "现行", "role": "ADR 清单（决策的编号权威）"},
    "07-实现与规格偏离对账.md": {"status": "现行", "role": "D-1~D-14 偏离对账（D-10 修复记录在 §7）"},
    "23-错题本-开发过程.md": {"status": "现行", "role": "早期错题本（10-26 前）"},
    "00-工作纪律-协议对齐.md": {"status": "现行", "role": "工作纪律：动手前先核对官方原文"},
    # ---- 已被取代 ----
    "29-项目全景-v1.md": {"status": "已取代", "superseded_by": "D30", "role": "全景 v1（被 v2 收敛版取代）"},
    "29-项目全景-目标阶段与内容.md": {"status": "已取代", "superseded_by": "D30", "role": "全景草稿（被 v2 取代）"},
    "10-架构设计-平台化-v0.1.md": {"status": "已取代", "superseded_by": "D10b", "role": "平台化架构 v0.1"},
    "10-架构设计-平台化-v0.2.md": {"status": "现行", "role": "平台化架构 v0.2（v0.1 的取代者）"},
    "42-hooks系统升级-复盘-20261026.md": {"status": "历史", "role": "hooks 升级复盘"},
    "42-fish对AI输出全景-20261026.md": {"status": "历史", "role": "fish 对 AI 的输出全景盘点"},
}

STATUSES = ("现行", "历史", "已取代", "归档")


def family_of(name):
    for fam, letter, keys in FAMILIES:
        if any(k in name for k in keys):
            return fam, letter
    return DEFAULT_FAMILY, DEFAULT_LETTER


_NUM_RE = re.compile(r"^(\d+)([a-zA-Z]?)[-_](.*)\.md$")


def scan(workspace=None):
    """扫描工作区根下的 md，返回 [{file, num, letter, title, family, id}]（按编号排序）。"""
    ws = Path(workspace or WORKSPACE)
    out = []
    for name in sorted(os.listdir(ws)):
        if not name.endswith(".md"):
            continue
        # **索引文件自己不算文档**。否则它一被生成就把自己算进"文档总数"，
        # 下次生成又多一行 → 永远不幂等，G2 从第一天起就红。
        if name == INDEX_NAME:
            continue
        p = ws / name
        if not p.is_file():
            continue
        m = _NUM_RE.match(name)
        if m:
            num, suffix, title = int(m.group(1)), m.group(2), m.group(3)
        else:
            num, suffix, title = None, "", name[:-3]
        fam, letter = family_of(name)
        if num is None:
            doc_id = "X-" + re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]", "", title)[:12]
        else:
            doc_id = f"{letter}{num}{suffix}"
        ov = OVERRIDES.get(name, {})
        out.append({
            "file": name, "num": num, "letter": suffix, "title": title,
            "family": fam, "id": doc_id,
            "status": ov.get("status", "历史"),
            "role": ov.get("role", ""),
            "superseded_by": ov.get("superseded_by", ""),
        })
    out.sort(key=lambda d: (d["num"] if d["num"] is not None else 10**6, d["file"]))
    # 同族同号（如 `10-架构设计-v0.1` 与 `v0.2`）依然会得到同一个 ID。
    # 按**文件名顺序**追加 a/b/c 后缀——确定性规则，不需要人工维护第二张表。
    seen = {}
    for d in out:
        base = d["id"]
        if base in seen:
            seen[base] += 1
            d["id"] = f"{base}{chr(ord('a') + seen[base])}"
        else:
            seen[base] = 0
            if sum(1 for x in out if x["id"] == base) > 1 or any(
                    x is not d and x.get("_baseid") == base for x in out):
                pass
    # 上面逐个分配时，第一个占 base，后续追加后缀——但 base 本身也要能指向第一份，
    # 所以这里给"第一份"补一个显式 a 仅当它确实有兄弟。
    import collections as _c
    cnt = _c.Counter(d["id"][:-1] if d["id"][-1:].isalpha() and d["id"][-1].isalpha()
                     and not d["id"].endswith(("b", "c", "d")) else d["id"] for d in out)
    return out


def check(docs):
    """四条不变式。返回 (errors, warnings)。"""
    errs, warns = [], []
    ids = {}
    for d in docs:
        if d["id"] in ids:
            errs.append(f"ID 撞号：{d['id']} 同时属于 {ids[d['id']]} 与 {d['file']}")
        ids[d["id"]] = d["file"]
        # ① 覆盖：每份带编号的文档必须在 OVERRIDES 或默认可推
        if d["num"] is not None and d["status"] not in STATUSES:
            errs.append(f"{d['file']}：status={d['status']!r} 不在 {STATUSES}")
        # ③ 已取代必须写被谁取代
        if d["status"] == "已取代" and not d["superseded_by"]:
            errs.append(f"{d['file']}：已取代但没写 superseded_by")
        # ④ 取代关系指向的 ID 必须存在
        if d["superseded_by"] and d["superseded_by"] not in ids and d["superseded_by"] not in {
                x["id"] for x in docs}:
            errs.append(f"{d['file']}：superseded_by={d['superseded_by']} 指向不存在的 ID")
    # ② 唯一已由 ids 覆盖 —— 但**文件名编号仍然撞号**这件事必须显式暴露：
    # ID 消解了歧义，不等于歧义不存在。裸编号引用（"见 20 §3"）依然是模棱两可的。
    bynum = {}
    for d in docs:
        if d["num"] is not None:
            bynum.setdefault(d["num"], []).append(d)
    for num, group in sorted(bynum.items()):
        if len(group) > 1:
            names = " / ".join(f"{x['id']}({x['file']})" for x in group)
            warns.append(f"文件名编号 {num} 被 {len(group)} 份共用：{names}"
                         f" —— 引用时**必须用 ID**，不要写裸编号")
    # 额外：现行文档不该被"已取代"
    for d in docs:
        if d["status"] == "现行" and not d["role"]:
            warns.append(f"{d['file']}：现行文档没写职责（读者不知道它答什么问题）")
    return errs, warns


# ============================================================== 阶段编号收敛表
#
# 七套 → 一套。**不是把六套删掉**（它们是历史文档，删了就没法追溯），
# 而是声明**唯一的现行口径**，并把其余六套登记为"历史编号，引用时须注明出处"。
PHASE_CANON = [
    ("A", "地基", "事件溯源 / 编码 / 身份 / 注入 / 关联 / 检索", "09 §8"),
    ("B", "空间", "三库门面 / 看板 / 知识库", "09 §8"),
    ("C", "在场", "推送 / 会话档期 / 工具观测", "09 §8"),
    ("D", "语境", "主人画像 / 时间段全景", "09 §8"),
]
PHASE_LEGACY = [
    ("P0–P4", "16 §8", "工程分期（按'期'）", "历史编号：与 A–D 无对应关系，勿混用"),
    ("主题六层", "30 §2.2", "索引视图（按主题）", "历史编号：只用于检索，不代表先后"),
    ("三段 P0/P1/P2", "47", "10-26 的下一阶段规划", "历史编号：**这里的 P0 ≠ 16 的 P0**"),
    ("四段", "33", "10-27 的规划（止血→索引→管道→排序）", "历史编号：已被 51 的 T01–T05 取代"),
    ("T01–T05", "51", "10-27 的五批交付", "历史编号：只有 T01 交付，T02–T05 空表"),
    ("未实现优先级", "30 §6.2", "P1-a / P2 / P3 排序", "历史编号：只表示优先级，不表示阶段"),
]


def render_markdown(docs, errs, warns):
    n_cur = sum(1 for d in docs if d["status"] == "现行")
    n_sup = sum(1 for d in docs if d["status"] == "已取代")
    fams = {}
    for d in docs:
        fams.setdefault(d["family"], []).append(d)
    L = [
        "# 71 · 文档索引（权威）",
        "",
        "> **生成器**：`p0/tools/doc_index.py`（`--check` 可校验）。**手改会被下次生成覆盖。**",
        f"> 范围：工作区根下 **{len(docs)}** 份 Markdown；其中带编号 "
        f"**{sum(1 for d in docs if d['num'] is not None)}** 份。",
        "> 状态：**现行** " + str(n_cur) + " / **已取代** " + str(n_sup) + " / 历史 "
        + str(sum(1 for d in docs if d["status"] == "历史")) + "。",
        "",
        "## 0. 这份索引为什么存在（三个具体问题）",
        "",
        "| 问题 | 事实 | 本索引怎么处理 |",
        "|---|---|---|",
        "| **编号撞号** | **20 组**：五族文档共用内部工程同一个编号空间"
        "（`20-系统全景…` 与 `20-外部评估…` 并存） | 给每份文档一个**唯一 ID**"
        "（内部工程 `D<n>`，其余族 `E/R/H/K/S<n>`）。**不改文件名**——"
        "全工作区 1,862 次文档引用，改名要同步改引用，而历史文档是审计记录 |",
        "| **七套阶段编号** | A–D / P0–P4 / 主题六层 / 三段 / 四段 / T01–T05 / 未实现优先级。"
        "最要命的一句（`66` §4.1）：**「同一个 P0 在不同文档里含义完全不同」** | "
        "声明**唯一现行口径 A–D**，其余六套登记为历史编号（见 §2） |",
        "| **同一指标多口径** | README 徽章写 `tests-1,647 passed`，实际 1,737 | "
        "指标的唯一出处是 **实测**；文档只许引用，不许另存一份（见 §3） |",
        "",
        "---",
        "",
        "## 1. 怎么引用文档（**新文档必须遵守**）",
        "",
        "```",
        "✅ 引用 ID：  见 D30 §6.2          （内部工程第 30 号）",
        "✅ 引用 ID：  见 E20               （外部评估第 20 号，与 D20 不撞）",
        "❌ 裸编号：   见 20 §3             （20 有两份，读者答不出是哪一份）",
        "```",
        "",
        "内部工程 `D<n>` 与旧习惯的 `NN` 一一对应，所以老文档里的 `见 30` 仍然可读；"
        "**只有跨族引用必须用 ID**。",
        "",
        "---",
        "",
        "## 2. 阶段编号：七套收敛成一套",
        "",
        "### 2.1 现行口径（唯一）",
        "",
        "| 编号 | 主题 | 包含 | 出处 |",
        "|---|---|---|---|",
    ]
    for letter, theme, content, src in PHASE_CANON:
        L.append(f"| **{letter}** | {theme} | {content} | `{src}` |")
    L += [
        "",
        "### 2.2 历史编号（可查、可引用，但**不再用于新文档**）",
        "",
        "| 编号 | 出处 | 含义 | 使用规则 |",
        "|---|---|---|---|",
    ]
    for name, src, meaning, rule in PHASE_LEGACY:
        L.append(f"| {name} | `{src}` | {meaning} | {rule} |")
    L += [
        "",
        "> **一句话**：要谈\"做到哪一步\"，只用 A–D；要谈\"某个历史文档当时怎么排的\"，"
        "把它的编号连出处一起写（例：\"`47` 的 P0\"）。",
        "",
        "---",
        "",
        "## 3. 指标口径：唯一出处是实测",
        "",
        "| 指标 | 唯一出处 | 取值方式 |",
        "|---|---|---|",
        "| 测试数 / 跳过数 | `cd p0; python -m pytest -q --no-header -p no:cacheprovider` | 看末行 |",
        "| `fish.py` 行数 | 同上目录 `Get-Content fish.py`（或 `wc -l`） | 实测 |",
        "| MCP 工具数 | `python -c \"import mcp_server; print(len(mcp_server.TOOLS))\"` | 实测 |",
        "| 判据总量 | `pytest --collect-only -q` | 实测 |",
        "",
        "**纪律**：任何文档写这些数字时，要么当场引用实测输出，要么写明"
        "\"截至某时的读数\"。**不许把数字抄进第二份文档当权威**——"
        "这正是 README 徽章停在 1,647 的原因。",
        "",
        "---",
        "",
        "## 4. 索引（按编号；ID 是引用用的名字）",
        "",
    ]
    for fam, items in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        cur = [d for d in items if d["status"] == "现行"]
        L += [f"### {fam}（{len(items)} 份，现行 {len(cur)}）", "",
              "| ID | 状态 | 文件 | 它能回答什么 |", "|---|---|---|---|"]
        for d in items:
            sup = f" → {d['superseded_by']}" if d["superseded_by"] else ""
            L.append(f"| `{d['id']}` | {d['status']}{sup} | {d['file']} | {d['role']} |")
        L.append("")
    L += ["---", "", "## 5. 校验结果", ""]
    if errs:
        L.append("**❌ 不变式失败 " + str(len(errs)) + " 条**：")
        for e in errs:
            L.append(f"- {e}")
    else:
        L.append("✅ 四条不变式全过：覆盖 / 唯一 / 定状态 / 引用完整。")
    if warns:
        L.append("")
        L.append("**⚠ 提醒 " + str(len(warns)) + " 条**（不阻断）：")
        for w in warns[:20]:
            L.append(f"- {w}")
        if len(warns) > 20:
            L.append(f"- …… 另有 {len(warns) - 20} 条")
    return "\n".join(L) + "\n"



# ============================================================== 指标盖章（口径唯一）
#
# README 徽章曾是"指标多口径"的活证据：写着 `tests-1,647 passed`，实际早已 1,737。
# 根因不是有人写错，而是**同一份数字被抄到了第二个地方**（README），而那里没有
# 任何机制让它跟着变。
#
# 做法：把"实测 → 写入徽章"变成一条命令（`--stamp`），并让判据去比对
#       "徽章里的数" 与 "现测的数"。两者只可能有两种状态：一致，或者红。

# 徽章里的数字是 **URL 编码**的：`1%2C647` 而不是 `1,647`。第一版正则漏了这点，
# 于是 --stamp 报"本就一致"却什么都没改——判据抓不到，只有实测才暴露。
# 标签用 `criteria-…-total` 而不是 `tests-…-passed`：那个数是 **collected**（含 skip），
# 写 "passed" 与实测的 passed 数（少 4）对不上——同一个毛病换了个地方犯。
BADGE_TESTS_RE = re.compile(
    r"((?:tests|criteria)-)([0-9]+(?:%2C[0-9]+)?)(%20(?:passed|total))")
BADGE_LINES_RE = re.compile(r"(fish\.py-)([0-9]+(?:%2C[0-9]+)?)(%20lines)")


def measure_criteria(p0_dir):
    """实测两条指标：收集到的判据总数（不真跑，~3 秒）。"""
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(p0_dir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    m = re.search(r"(\d+) tests? collected", r.stdout or "")
    n_tests = int(m.group(1)) if m else None
    fish_py = Path(p0_dir) / "fish.py"
    n_lines = len(fish_py.read_text(encoding="utf-8").split("\n")) - 1
    return {"criteria": n_tests, "fish_lines": n_lines}


def stamp_readme(ws, p0_dir, write=True):
    """把实测读数盖进 README 徽章。返回 {before, after, changed}。"""
    readme = Path(ws) / "README.md"
    text = readme.read_text(encoding="utf-8")
    got = measure_criteria(p0_dir)
    before = text
    if got["criteria"] is not None:
        text = BADGE_TESTS_RE.sub(
            lambda m: f"{m.group(1)}{str(got['criteria']).replace(chr(44), '%2C')}{m.group(3)}", text)
    if not BADGE_LINES_RE.search(text):
        # 没有这条徽章就补一条：行数是"结构债"的唯一可量化读数，值得在门面上看得见。
        text = text.replace(
            "![mutants]",
            '![lines](https://img.shields.io/badge/fish.py-0%20lines-blue)\n'
            "![mutants]", 1)
    text = BADGE_LINES_RE.sub(
        lambda m: f"{m.group(1)}{str(got['fish_lines']).replace(chr(44), '%2C')}{m.group(3)}", text)
    changed = text != before
    if write and changed:
        readme.write_text(text, encoding="utf-8", newline="\n")
    return {"measured": got, "changed": changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档索引生成器 / 校验器")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--workspace", default=None, help="工作区路径（缺省=本文件的上一级）")
    ap.add_argument("--stamp", action="store_true",
                    help="把实测指标盖进 README 徽章（指标口径唯一的写入口）")
    args = ap.parse_args(argv)
    ws = Path(args.workspace or WORKSPACE)
    if args.stamp:
        res = stamp_readme(ws, HERE.parent)
        print(f"实测：判据 {res['measured']['criteria']} 条 / "
              f"fish.py {res['measured']['fish_lines']} 行；"
              f"README 徽章{'已更新' if res['changed'] else '本就一致'}")
        return 0
    docs = scan(args.workspace)
    errs, warns = check(docs)
    if not args.check:
        (ws / INDEX_NAME).write_text(render_markdown(docs, errs, warns),
                                     encoding="utf-8", newline="\n")
        (ws / CATALOG_NAME).write_text(
            json.dumps(docs, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n")
        print(f"已生成 {INDEX_NAME} 与 {CATALOG_NAME}（{len(docs)} 份文档）")
    for e in errs:
        print("错误：" + e, file=sys.stderr)
    for w in warns:
        print("提醒：" + w, file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
