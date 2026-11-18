#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fish 的 CLI 层：argparse 装配 + 命令分发（F-P1-10 拆分第一刀）。

## 这个模块为什么存在

`fish.py` 曾经有 13,277 行，其中 695 行是「把 84 个参数定义接到 43 个命令实现上」。
那是**装配**，不是领域逻辑——它和记忆、身份、知识库没有语义关系，只是把名字连起来。

放在同一个文件里有两个实际代价：

1. **J4 行数窗口被撑满**（13,300 上限 vs 13,277 实际）。后面任何一条修复想往
   fish.py 加代码都会撞窗，于是人只能"抬窗"——而那正是外部审查批评的事。
2. **改一个参数默认值要滚过 13,000 行**，评审时真正的改动淹没在上下文里。

## 依赖方向

`fish_cli` → `fish`（单向）。命令实现全部留在 `fish.py`，这里只负责"哪个参数
对应哪个函数"。

`fish.py` 在**文件末尾**才 `from fish_cli import main`——那时它自己的定义已经全部
完成，所以 `fish_cli` 里的 `import fish` 拿到的是完整模块，不是半成品。
"""
import argparse
import os
import sys
from pathlib import Path

# ## 为什么不在这里 `import fish`
#
# 试过，两种运行方式只有一种能活：
#   · `import fish`（被别的模块用）→ 正常
#   · `python fish.py ...`（脚本方式）→ **炸**：此时 fish.py 是 `__main__`，
#     这里的 `import fish` 会**再加载一份** fish.py；第二份跑到末尾又
#     `from fish_cli import main`，而 fish_cli 此刻还没初始化完 →
#     `ImportError: cannot import name 'main' from partially initialized module`
#
# 所以改成**显式注入**：fish.py 在末尾把「自己这个模块对象」交给本模块。
# 两种运行方式拿到的是同一个对象，没有第二份。
_f = None
_ns = None


class _LiveNamespace:
    """按名**现查** fish.py 的 globals（不拷贝、不缓存）。

    为什么不用模块对象 + 属性拷贝：
      · `sys.modules[__name__]` 在某些加载方式下不存在（本项目的测试用
        `importlib.util.spec_from_file_location` 直接 exec，不走 sys.modules）；
      · 拷贝一份会在 `monkeypatch.setattr(fish, "cmd_kb", fake)` 之后**看到旧值**
        ——判据会变成"改了没生效"，而那是假红。
    现查两边都没有这个问题。
    """

    __slots__ = ("_d",)

    def __init__(self, d):
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_d")[name]
        except KeyError:
            raise AttributeError(name) from None


def install(namespace):
    """由 fish.py 在文件末尾调用，把它的 `globals()` 交给本模块。

    `namespace` 是 **dict**（`fish.py` 里直接 `install(globals())`），不是模块对象。
    传 dict 的第二个好处：脚本方式（`python fish.py`）与 import 方式拿到的是
    同一份东西，不会出现"第二份 fish.py"。
    """
    global _f, _ns
    _ns = namespace
    _f = _LiveNamespace(namespace)
    # 补齐 main() 里的**裸引用**（`DEFAULT_ROOT`、`SCHEMA` 这类常量/函数）。
    # `if k not in g` 保证不覆盖本模块自己的定义。
    g = globals()
    for k, v in namespace.items():
        if not k.startswith("__") and k not in g:
            g[k] = v


def main():
    # 计划任务/无控制台环境下 stdout 是 GBK，中文与 ⚠ 等符号会直接崩（实测 ExitCode=1）。
    # 这里强制 UTF-8，避免"功能正常但退出码 1"的假失败。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception as exc:
            pass  # A档：顶层兜底不污染 stdout/stderr（hook 按 `key: value` 解析）；见静默清单 §零

    ap = argparse.ArgumentParser(prog="fish", description="大肥鱼生命系统 v0.3 (P1a)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=_f.cmd_init)

    p = sub.add_parser("project-add"); p.add_argument("prefix"); p.add_argument("--name", default="")
    p.set_defaults(func=_f.cmd_project_add)

    # 项目前缀生命周期（retire / restore）。**默认只打印计划**：--apply 才落库。
    # 为什么与 project-add 不同族：add 是"开一个新前缀"（幂等、无副作用面），
    # 而 retire 改的是"以后还能不能发新码"——它必须能先看计划再执行。
    p = sub.add_parser("project", help="项目前缀生命周期（retire / restore；默认只打印计划）")
    psub = p.add_subparsers(dest="project_action", required=True)

    pp = psub.add_parser("retire", help="退役一个前缀：停止分配新编码（不删行、不回收前缀）")
    pp.add_argument("prefix", help="项目前缀（如 测试项目）")
    pp.add_argument("--reason", default="", help="为什么退役（写进事件，可追溯）")
    pp.add_argument("--apply", action="store_true",
                    help="真的落库；不加只打印计划（默认行为）")
    pp.add_argument("--force", action="store_true",
                    help="名下有记忆也退役（只停分配，不动记忆归属）")
    pp.set_defaults(func=_f.cmd_project)

    pp = psub.add_parser("restore", help="把 retired 改回 active（retire 的撤销）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=_f.cmd_project)

    # 2026-10-25（阶段 D · 项目三态）：闭合 / 重开。
    # 与 retire 的区别：retire 回答"这个名字还用不用"（终态，前缀不可再分配）；
    # close 回答"这件事做完了没有"（可逆，随时 reopen 回来继续写）。
    pp = psub.add_parser("close", help="闭合一个项目：这件事做完了（可 reopen，不删任何东西）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=_f.cmd_project)

    pp = psub.add_parser("reopen", help="把 closed 改回 active（闭合的撤销，之后可继续写）")
    pp.add_argument("prefix")
    pp.add_argument("--reason", default="")
    pp.add_argument("--apply", action="store_true", help="真的落库；不加只打印计划")
    pp.set_defaults(func=_f.cmd_project)

    # B2 项目状态表（2026-10-26）：`project status` 是**投影**，读的是
    # code_registry / code_index / boards / presence / project_stints 五张表。
    # 与 retire/restore/close/reopen 同族但独立 func：那四条改状态，这条只读+渲染。
    pst = psub.add_parser("status", help="渲染/查看「项目状态表」（B2；只读投影，不改任何表）")
    pst.add_argument("--include-retired", dest="include_retired", action="store_true",
                     help="把 retired 项目也放进主表（裁决 Q7 的逃生口）")
    pst.add_argument("--as-of", dest="as_of", default=None,
                     help="冻结时间（ISO8601）；缺省=现在。同一 as-of 重渲染逐字节全等")
    pst.add_argument("--home", default=None, help="门面根覆盖（缺省 <项目根>/fish-home）")
    pst.add_argument("--allow-external-home", dest="allow_external_home",
                     action="store_true",
                     help="允许把门面根指到项目目录之外（默认拒绝；盘根/系统目录永远拒绝）")
    pst.add_argument("--json", action="store_true", help="机器可读形态（含每格取值来源）")
    pst.set_defaults(func=_f.cmd_project_status)

    # 名字别名（阶段 D）：只给 CLI，不给 MCP 工具——见 _f.cmd_alias 的 docstring。
    p = sub.add_parser("alias", help="名字别名（单向 from→to；只影响检索，不改写入归属）")
    asub = p.add_subparsers(dest="alias_action", required=True)

    # ⚠️ 变量名纪律：**不要用 `ap`**。`main()` 里 `ap` 是**根 parser**
    # （`ap = argparse.ArgumentParser(...)`，结尾 `ap.parse_args()` 用的就是它）。
    # 2026-10-25 我真踩了一次：把这里的循环变量写成 `ap`，三行之后根 parser 被
    # 覆盖成 `alias list` 子命令，于是 `fish.py --help` 显示的是 list 的用法、
    # 所有子命令都报 "unrecognized arguments"。22 条 CLI 判据当场全红才暴露。
    alp = asub.add_parser("link", help="建立别名：from 就是 to（要求 to 真实存在且不成环）")
    alp.add_argument("kind", choices=list(_ALIAS_KINDS))
    alp.add_argument("from_name")
    alp.add_argument("to_name")
    alp.add_argument("--reason", default="")
    alp.set_defaults(func=_f.cmd_alias)

    alp = asub.add_parser("unlink", help="撤销别名（幂等：不存在也算成功）")
    alp.add_argument("kind", choices=list(_ALIAS_KINDS))
    alp.add_argument("from_name")
    alp.add_argument("--reason", default="")
    alp.set_defaults(func=_f.cmd_alias)

    alp = asub.add_parser("list", help="列出全部别名")
    alp.set_defaults(func=_f.cmd_alias)

    # 看板（P1-b · 2026-10-25）。裁决 `16` §4.1：**数据库做真相 + 文件做渲染**，
    # 所以写入只走这里（AI 不许直接 write 渲染文件），文件删了可随时重建。
    # F-P1-5（2026-11-18 外部审查）：数据面的四个动作原本没有任何产品入口——
    # 资产表/缩略图/引用反查/无引用结论都只能靠作者的临时脚本产出。
    # 全部默认 dry-run、只读：这些操作会遍历 10.7 GB，误触发的代价远高于多打一个 --apply。
    p = sub.add_parser("dataplane", help="数据面：资产扫描 / 引用反查 / 缩略图 / 读数")
    dp = p.add_subparsers(dest="dp_action", required=True)
    for _name, _help in (("stats", "资产层读数（按 kind/status 聚合）"),
                         ("scan", "扫描 vault 写 kb_asset"),
                         ("refs", "从笔记反解引用，写 referenced_by"),
                         ("thumbs", "生成 WebP 缩略图")):
        _sp = dp.add_parser(_name, help=_help)
        _sp.add_argument("--vault", default=None, help="库名（缺省=默认库）")
        _sp.add_argument("--json", action="store_true", help="JSON 输出")
        _sp.add_argument("--limit", type=int, default=None, help="最多处理几个")
        _sp.add_argument("--hash", action="store_true", help="扫描时算 sha256（慢）")
        _sp.add_argument("--apply", action="store_true", help="真写库（默认 dry-run）")
        _sp.set_defaults(func=_f.dataplane_cli)
    _cp = dp.add_parser("classify", help="判定单个文件的 kind 与 magic")
    _cp.add_argument("path")
    _cp.set_defaults(func=_f.dataplane_cli)

    p = sub.add_parser("board", help="看板（show / checkin / post / render / close）")
    bsub = p.add_subparsers(dest="board_action", required=True)

    def _board_common(bp, need_doing=False):
        """看板子命令的公共参数。`--project` 是 `--scope project --key <项目>` 的便捷写法。"""
        bp.add_argument("--scope", default="project", choices=list(BOARD_SCOPES))
        bp.add_argument("--key", default="")
        bp.add_argument("--project", default=None)
        bp.add_argument("--period", default=None)
        # 2026-10-29（体验复测报告 B1）：`board show --token` 曾报 `unrecognized arguments`
        # ——参数面没注册 = 用户照着别的子命令的习惯敲就踩。统一到公共参数，并**真消费**
        # （见 `checkin` 段：token 可替代 `--citizen`；`--citizen` 显式给时仍优先）。
        bp.add_argument("--token", default=None,
                        help="用这个凭证解析身份（可替代 --citizen；显式 --citizen 优先）")
        return bp

    # show：读看板（默认动作）
    bsh = _board_common(bsub.add_parser("show", help="读看板（不渲染）"))
    bsh.add_argument("--json", dest="as_json", action="store_true",
                     help="机器视图（JSON：含 citizen_id 与会话键，供脚本消费）")
    bsh.set_defaults(func=_f.cmd_board)

    # checkin：签到（doing 必填——它是看板的核心价值）
    bck = _board_common(bsub.add_parser("checkin", help="签到：登记你在做什么"))
    bck.add_argument("--doing", required=True, help="一句话：在做什么（≤80 字）")
    bck.add_argument("--surface", default="", help="占用面（文件/目录前缀，逗号分隔）")
    bck.add_argument("--citizen", default=None, help="公民 id（默认取系统主人）")
    bck.add_argument("--agent", default=None, help="会话/宿主侧标识")
    # 2026-10-26（撞车自撞修复 T02）：终端里没有 token，所以会话键必须能显式给。
    # 缺省读环境变量 `FISH_SESSION_KEY`（WorkBuddy/hook 侧就是这么传的）。
    bck.add_argument("--session-key", dest="session_key", default=None,
                     help="会话键（如 dsh:abc…）；缺省读环境变量 FISH_SESSION_KEY。"
                          "不传 = 无会话归属（''，与今天的行为相同）")
    # 2026-10-25（`EVAL-261025-14`）：`status='left'` 在 DDL 里写着，但**没有任何入口**
    # ——想「收工下板」的人做不到，行只能自然变 idle、或等 sweep 归档。
    # 补上之后，`left` 才从一个词汇变成一个动作。
    bck.add_argument("--status", default="active", choices=["active", "left"],
                     help="active=在场（默认）/ left=收工下板（之后不再计入重叠）")
    bck.set_defaults(func=_f.cmd_board)

    # post：往今日动态加一条
    bpo = _board_common(bsub.add_parser("post", help="往今日动态加一条（≤200 字）"))
    bpo.add_argument("text", help="动态内容")
    bpo.add_argument("--kind", default="note", choices=["checkin", "progress", "done", "note"])
    bpo.add_argument("--citizen", default=None)
    bpo.set_defaults(func=_f.cmd_board)

    # render：重新渲染（自愈）
    brd = _board_common(bsub.add_parser("render", help="重新渲染看板文件（可自愈）"))
    brd.add_argument("--all", dest="render_all", action="store_true",
                     help="渲染 boards 表里的全部看板（含根板）；缺省只渲染当前 scope/key")
    brd.set_defaults(func=_f.cmd_board)

    # close：归档（不删）
    bcl = _board_common(bsub.add_parser("close", help="归档看板（closed 后不再收签到，但不删）"))
    bcl.set_defaults(func=_f.cmd_board)

    # sweep：批量归档 N 天前的 open 板（2026-10-25 · 要求 C）
    bsw = bsub.add_parser("sweep", help="把 N 天前的 open 看板自动归档（只改状态，不删）")
    bsw.add_argument("--days", type=int, default=None,
                     help="保留最近 N 天（缺省取 config board.sweep_days，默认 7）")
    bsw.set_defaults(func=_f.cmd_board)

    # B1 门面（2026-10-26）：`fish-home/` 的渲染与体检。
    # **`render` 必须带 `--all`**：本批只做全量渲染（`--only` 是 P2/裁决不做，
    # parser 刻意不提供该参数 → argparse 报 unrecognized → exit 2，与契约一致）。
    p = sub.add_parser("layout", help="fish-home 门面：render（渲染）/ verify（体检）")
    lsub = p.add_subparsers(dest="layout_action", required=True)

    lr = lsub.add_parser("render", help="渲染门面全部产物（生成物，可删可重建）")
    lr.add_argument("--all", dest="render_all", action="store_true",
                    help="渲染五个一级目录的全部产物（**必填**；缺省 → exit 2）")
    lr.add_argument("--as-of", dest="as_of", default=None,
                    help="冻结时间（ISO8601）；同一 as-of 连跑两次逐字节全等")
    lr.add_argument("--home", default=None, help="门面根覆盖（缺省 <项目根>/fish-home）")
    lr.add_argument("--allow-external-home", dest="allow_external_home",
                    action="store_true",
                    help="允许把门面根指到项目目录之外（默认拒绝；盘根/系统目录永远拒绝）")
    lr.add_argument("--force-readme", dest="force_readme", action="store_true",
                    help="覆盖已存在的五个一级 README（缺省：存在即不覆盖，裁决 C5）")
    lr.set_defaults(func=_f.cmd_layout)

    lv = lsub.add_parser("verify", help="体检门面：缺失/多余/超深/越界/README/链接/正文副本/凭证")
    lv.add_argument("--json", action="store_true", help="机器可读形态（同一套 code）")
    lv.add_argument("--home", default=None, help="门面根覆盖")
    lv.add_argument("--allow-external-home", dest="allow_external_home",
                    action="store_true",
                    help="允许把门面根指到项目目录之外（默认拒绝；盘根/系统目录永远拒绝）")
    lv.set_defaults(func=_f.cmd_layout)

    # B3 知识库（2026-10-26）：本批只登记 CLI 契约（T04 实装）。
    # 为什么现在就把子命令全注册：CLI 面一旦进文档就是契约，后补参数等于破坏性变更；
    # 占位实现统一 exit 2（见 _f.cmd_kb），不会假装成功。
    p = sub.add_parser("kb", help="知识库（vault / tree / open / search / write / rollback / stats / doctor）")
    ksub = p.add_subparsers(dest="kb_action", required=True)

    kv = ksub.add_parser("vault", help="登记 / 列出 / 设默认 / 改模式 / 扫描 / 移除")
    kvsub = kv.add_subparsers(dest="kb_vault_action", required=True)

    kva = kvsub.add_parser("add", help="登记一个 vault（不搬运任何文件）")
    kva.add_argument("--name", required=True, help="人给的名字（缺省取根目录名）")
    kva.add_argument("--path", required=True, help="vault 绝对路径（原地不动）")
    kva.add_argument("--mode", default="ro", choices=["ro", "rw"],
                     help="ro=只读（默认）/ rw=允许写入")
    kva.add_argument("--ignore", action="append", default=[],
                     help="额外忽略规则（glob，可重复），如 \"**/_media/**\"")
    kva.add_argument("--default", action="store_true", help="设为默认 vault（唯一）")
    kva.add_argument("--note", default=None, help="备注（给人看）")
    kva.set_defaults(func=_f.cmd_kb)

    kvsub.add_parser("list", help="列出全部 vault").set_defaults(func=_f.cmd_kb)

    kvs = kvsub.add_parser("set-default", help="指定默认 vault")
    kvs.add_argument("key", help="name 或 vault_id")
    kvs.set_defaults(func=_f.cmd_kb)

    kvm = kvsub.add_parser("set-mode", help="改读写模式（ro/rw）")
    kvm.add_argument("key", help="name 或 vault_id")
    kvm.add_argument("mode", choices=["ro", "rw"])
    kvm.set_defaults(func=_f.cmd_kb)

    kvsc = kvsub.add_parser("scan", help="walk+stat 汇总并写 last_scan_*（不读正文）")
    kvsc.add_argument("key", nargs="?", default=None, help="name 或 vault_id；缺省=默认")
    kvsc.set_defaults(func=_f.cmd_kb)

    kvr = kvsub.add_parser("remove", help="只删注册行，**不动磁盘**")
    kvr.add_argument("key", help="name 或 vault_id")
    kvr.set_defaults(func=_f.cmd_kb)

    kt = ksub.add_parser("tree", help="看目录（只统计不读正文；预算受限）")
    kt.add_argument("path", nargs="?", default="", help="起点（vault 内相对路径；缺省=根）")
    kt.add_argument("--vault", default=None)
    kt.add_argument("--depth", type=int, default=2)
    kt.add_argument("--max-chars", dest="max_chars", type=int, default=8000)
    kt.add_argument("--json", action="store_true")
    kt.set_defaults(func=_f.cmd_kb)

    # T05：目录快照（36/37 号"档 1"最小版）——分类概览 + 前 3 标题 + 分页。
    ktc = ksub.add_parser("toc", help="目录快照（分类概览 + 前 3 标题 + 分页；海量库省 token）")
    ktc.add_argument("path", nargs="?", default="", help="起点（vault 内相对路径；缺省=根）")
    ktc.add_argument("--vault", default=None)
    ktc.add_argument("--depth", type=int, default=2)
    ktc.add_argument("--limit", type=int, default=None, help="每页最多几项（缺省=全部）")
    ktc.add_argument("--offset", type=int, default=0, help="跳过的项数（翻页）")
    ktc.add_argument("--max-chars", dest="max_chars", type=int, default=8000)
    ktc.add_argument("--json", action="store_true")
    ktc.set_defaults(func=_f.cmd_kb)

    ko = ksub.add_parser("open", help="读单文件（必须给显式相对路径）")
    ko.add_argument("path")
    ko.add_argument("--vault", default=None)
    ko.add_argument("--max-chars", dest="max_chars", type=int, default=64000)
    # R15：敏感名单（凭证/密钥/.env…）默认**不**返回正文，要显式二次授权
    ko.add_argument("--allow-sensitive", dest="allow_sensitive", action="store_true",
                    help="允许读敏感名单命中的文件正文（默认只回文件名+大小+建议）")
    ko.add_argument("--json", action="store_true")
    ko.set_defaults(func=_f.cmd_kb)

    kse = ksub.add_parser("search", help="在 vault 范围内确定性检索（不建索引）")
    kse.add_argument("query")
    kse.add_argument("--vault", default=None)
    kse.add_argument("--limit", type=int, default=10)
    kse.add_argument("--json", action="store_true")
    kse.set_defaults(func=_f.cmd_kb)

    kw = ksub.add_parser("write", help="写三档：create / append / update（update 必带 --base-sha256）")
    kw.add_argument("path")
    kmode = kw.add_mutually_exclusive_group(required=True)
    kmode.add_argument("--create", action="store_true")
    kmode.add_argument("--append", action="store_true")
    kmode.add_argument("--update", action="store_true")
    kw.add_argument("--base-sha256", dest="base_sha256", default=None)
    kw.add_argument("--content-file", dest="content_file", required=True)
    kw.add_argument("--frontmatter", action="append", default=[], metavar="k=v")
    kw.add_argument("--reason", default=None)
    # S6（第四轮）：写敏感文件（凭证/.env…）默认拒绝；这是"人显式授权"的本地逃生口。
    kw.add_argument("--allow-sensitive", dest="allow_sensitive", action="store_true",
                    help="允许改写敏感名单命中的文件（默认拒绝：凭证交 Obsidian/人工）")
    kw.add_argument("--vault", default=None)
    kw.set_defaults(func=_f.cmd_kb)

    krb = ksub.add_parser("rollback", help="按 write_id 撤回一次写")
    krb.add_argument("write_id")
    krb.add_argument("--force", action="store_true")
    krb.set_defaults(func=_f.cmd_kb)

    kst = ksub.add_parser("stats", help="未读清单 / 最近变更 / 每 vault 读写计数")
    kst.add_argument("--vault", default=None)
    kst.add_argument("--json", action="store_true")
    kst.set_defaults(func=_f.cmd_kb)

    kd = ksub.add_parser("doctor", help="逐 vault 体检 + obsidian_cli 探测 + 备份体积读数")
    kd.add_argument("--json", action="store_true")
    kd.set_defaults(func=_f.cmd_kb)

    # N2（安全 F10）：备份保留策略**只提供显式命令**，默认永不自动删。
    kbu = ksub.add_parser("backup", help="备份维护（prune：按天清理旧回滚点；默认不动手）")
    kbusub = kbu.add_subparsers(dest="kb_backup_action", required=True)
    kbup = kbusub.add_parser("prune", help="删掉 meta.ts 早于 N 天的备份目录（显式命令）")
    kbup.add_argument("--keep-days", dest="keep_days", type=int, required=True,
                      help="保留最近 N 天的回滚点（必须 > 0）")
    kbup.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="只统计不删除")
    kbup.set_defaults(func=_f.cmd_kb)

    p = sub.add_parser("remember")
    p.add_argument("project"); p.add_argument("title")
    p.add_argument("--body", default="")
    p.add_argument("--type", default="fact",
                   choices=["identity", "rule", "fact", "episode", "lesson", "artifact"])
    p.add_argument("--visibility", default="shared", choices=["private", "shared"],
                   help=("private=只给 owner 看。默认不生效："
                         "privacy.visibility_enforced 关时拒绝写入，避免变成一个骗人的标签"))
    p.add_argument("--tags", nargs="*", default=[])
    p.add_argument("--owner", default=None,
                   help="公民 ID（不是显示名）。缺省=系统主人（citizens.is_owner）")
    p.add_argument("--cause", default=None)
    p.add_argument("--occurred-at", dest="occurred_at", default=None,
                   help="这件事什么时候发生（ISO8601 UTC，如 2026-10-22T04:00:00Z）；"
                        "缺省=现在。用于补记过去的事，与 recorded_at 区分")
    p.add_argument("--attach", action="append", default=[],
                   help="附件路径，可重复（图片/文档等，按内容寻址存入 blobs/）")
    # F-P1-3：附件默认受四道校验（大小/敏感名单/系统目录/流式哈希）；
    # 这个开关只关掉"敏感名单 + 系统目录"两条，大小上限与流式始终生效。
    p.add_argument("--allow-sensitive-attach", dest="allow_sensitive_attach",
                   action="store_true",
                   help="允许附件命中敏感名单/系统目录（默认拒绝）")
    p.add_argument("--session-actor", dest="session_actor", default=None,
                   help="会话级身份（如 anyi-dev），用于区分同一宿主下的不同会话")
    # F-P1-2：`private` 在开关关着时不生效。要"只要标签"必须显式说出口。
    p.add_argument("--allow-unenforced-private",
                   dest="allow_unenforced_private", action="store_true",
                   help="允许写 private 但不启用过滤（它不会保护任何东西）")
    # FB-11：CLI 侧的“写新的同时置旧的”。可重复给（一条新记忆替代多条旧条目）。
    p.add_argument("--supersedes", action="append", default=[], metavar="OLD_CODE",
                   help="把这条替代掉的旧编码（可重复）。**新条目写成功之后**才置旧条目"
                        " superseded；旧编码不存在不会撤销新条目（只提示）")
    p.set_defaults(func=_f.cmd_remember)

    p = sub.add_parser("recall")
    # 2026-10-26（FB-01）：`query` 从必填改为可选（`nargs="?"`）——不传时走**列出模式**
    # （`--project` [+ `--tags`] 过滤后按时间倒序）。只在"没有项目也没有关键词"时
    # 才给出引导，而不是把"没给词"当成错误（老用法一字不变：`fish recall 关键词`）。
    p.add_argument("query", nargs="?", default=None,
                   help="关键词（省略时按 --project / --tags 列出）")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--by", choices=["priority", "relevance"], default="priority",
                   help="排序：priority（编码>标题>正文，默认）/ relevance（bm25 相关度）")
    p.add_argument("--scope", choices=["all", "title", "body"], default="all",
                   help="检索范围：all（默认）/ title / body")
    p.add_argument("--code", default=None, help="精确编码（与 query 二选一或并用）")
    p.add_argument("--project", default=None, help="只查该项目前缀")
    p.add_argument("--tags", nargs="*", default=[], help="标签过滤（需同时满足）")
    p.set_defaults(func=_f.cmd_recall)

    p = sub.add_parser("neighbors")
    p.add_argument("code")
    p.add_argument("--n", type=int, default=None)
    p.add_argument("--strategy", default=None, choices=["code", "time", "tag", "type", "mixed"])
    p.add_argument("--cross-project", dest="cross_project", action="store_true", default=None)
    p.add_argument("--include-dormant", dest="include_dormant", action="store_true", default=None)
    p.set_defaults(func=_f.cmd_neighbors)

    sub.add_parser("list").set_defaults(func=_f.cmd_list)

    p = sub.add_parser("forgetting"); p.add_argument("--apply", action="store_true")
    p.set_defaults(func=_f.cmd_forgetting)

    # 提炼建议（FB-15，16 §11.4）：同主题 episode 聚类 → 建议文件。
    # **默认演练**：不落文件、不写记忆、不改状态；`--write` 才产出建议。
    p = sub.add_parser(
        "distill",
        help="提炼建议：同 tag 的 episode 聚成建议（默认演练；--write 落文件，不入库）")
    p.add_argument("project", help="项目名（别名按检索口径展开）")
    p.add_argument("--days", type=int, default=DISTILL_DEFAULT_DAYS,
                   help=f"回看窗口天数（缺省 {DISTILL_DEFAULT_DAYS}）")
    p.add_argument("--min", type=int, default=DISTILL_MIN_GROUP,
                   help=f"组内至少几条才成组（缺省 {DISTILL_MIN_GROUP}）")
    p.add_argument("--max-groups", type=int, default=DISTILL_MAX_GROUPS,
                   help=f"最多输出几组（缺省 {DISTILL_MAX_GROUPS}）")
    p.add_argument("--write", action="store_true",
                   help="真的写建议文件；不加只打印（演练）")
    p.set_defaults(func=_f.cmd_distill)

    p = sub.add_parser("citizen-add")
    p.add_argument("name"); p.add_argument("--kind", default="visitor", choices=list(KINDS))
    p.add_argument("--role", default=None); p.add_argument("--host", default=None)
    p.set_defaults(func=_f.cmd_citizen_add)

    # 第三轮评审：MCP 报错让人跑这个命令，而它当时不存在。
    p = sub.add_parser("citizen-set-kind",
                       help="给公民改 kind（主人本机操作；resident/role 可写规矩）")
    p.add_argument("citizen_id")
    p.add_argument("kind")
    p.set_defaults(func=_f.cmd_citizen_set_kind)

    p = sub.add_parser("citizen-list")
    p.add_argument("--all", action="store_true",
                   help="连归档的公民一起列（归档是搬行，不在 citizens 里）")
    p.set_defaults(func=_f.cmd_citizen_list)

    p = sub.add_parser("citizen-merge",
                       help="把 src 的归属合并到 dst（默认 dry-run，加 --apply 才写）")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=_f.cmd_citizen_merge)
    p = sub.add_parser("citizen-restore")
    p.add_argument("citizen_id")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只报告会发生什么，不改库")
    p.set_defaults(func=_f.cmd_citizen_restore)

    p = sub.add_parser("citizen-retire")
    p.add_argument("citizen_id")
    p.add_argument("--reason", default=None)
    p.add_argument("--migrate-to", dest="migrate_to", default=None,
                   help="把该公民名下的记忆迁给另一个公民")
    p.add_argument("--force", action="store_true",
                   help="有记忆但不迁移（保持原归属，仅退役身份）")
    p.set_defaults(func=_f.cmd_citizen_retire)

    # 身份层 P1-b：session 命令族。**CLI 专属**——不注册成 MCP 工具（18 §4 / 评审 R-2）：
    # MCP 面签不出 machine 级，能签的只有"读得到宿主 env"的 CLI。
    p = sub.add_parser("session", help="会话身份（CLI 专属命令族；MCP 面不签发凭证）")
    sess = p.add_subparsers(dest="session_action", required=True)

    sp = sess.add_parser("start", help="签发会话凭证（幂等：同会话重跑 → 同一个公民）")
    sp.add_argument("--bind-token", dest="bind_token", default=None,
                    help="绑定已有公民时的证明：你现有的 fish token"
                         "（S-01：不带的话，认领已有身份会被拒绝）")
    sp.add_argument("--project", action="append", default=None, metavar="PREFIX",
                    help="顺带开一个项目档期（如 ANYI）。**可重复给**："
                         "一个会话可以同时在多个项目上开档（--project A --project B）")
    sp.add_argument("--account", default=None,
                    help="AI 账号（跨会话聚合锚点）。缺省取 FISH_ACTOR；**不再退到 --alias**"
                         "（2026-10-25 修：alias 是会话标签，不是账号，"
                         "把前者当后者会静默建出第二个公民）")
    sp.add_argument("--citizen", default=None, metavar="CITIZEN_ID",
                    help="把这条会话**直接绑到已存在的公民**上（跳过 find_or_create）。"
                         "用于「MCP 侧已登记、CLI 侧 host_session_key 不同」导致的归属分裂")
    sp.add_argument("--alias", default=None,
                    help="会话别名（没有宿主 session id 时，它就是幂等键；**只是标签**）")
    sp.add_argument("--cwd", default=None, metavar="PATH",
                    help="当前工作目录：按 workspace_map 最长前缀解析成项目并自动开档期。"
                         "**显式给了 --project 时以 --project 为准**（这里只做兜底）")
    sp.set_defaults(func=_f.cmd_session_start)

    sp = sess.add_parser("status", help="当前会话的身份 / 档期 / 凭证状态")
    sp.add_argument("--token", default=None, help="查某条凭证（缺省=用 env 定位当前会话）")
    sp.set_defaults(func=_f.cmd_session_status)

    sp = sess.add_parser("end", help="吊销凭证 + 关档期 + 刷新活跃时间（幂等）")
    sp.add_argument("--token", default=None, help="吊销这条凭证（缺省=吊销当前会话的全部）")
    sp.set_defaults(func=_f.cmd_session_end)

    sp = sess.add_parser("agent", help="为子 agent 签 inherited 凭证（只允许一跳）")
    sp.add_argument("--parent", required=True, help="父会话的 token 明文")
    sp.add_argument("--role", required=True, help="子 agent 的角色（会拼进 session_key）")
    sp.set_defaults(func=_f.cmd_session_agent)

    p = sub.add_parser("whoami", help="我是谁：身份段 + 凭证状态 + 身份来源")
    p.add_argument("--citizen", default=None)
    p.add_argument("--token", default=None,
                   help="会话凭证（缺省取 env FISH_SESSION_TOKEN）；"
                        "决定「凭证状态」「身份来源」两行")
    # 2026-10-26（评审 🟠3）：CLI 补 `--all`——MCP 的 whoami 早有 `all=true`。
    # 渲染与 MCP 共用 `fish.related_citizens_lines`（文案逐字一致）。
    p.add_argument("--all", dest="all", action="store_true",
                   help="附带「相关公民」清单（按 会话/同名/账号 关联，不是全库清单）")
    p.set_defaults(func=_f.cmd_whoami)

    p = sub.add_parser("owner")
    p.add_argument("action", choices=["set", "show"])
    p.add_argument("citizen_id", nargs="?", default=None)
    p.set_defaults(func=_f.cmd_owner)

    p = sub.add_parser("owner-fix", help="修正一条记忆的归属（追加事件 + 更新投影）")
    p.add_argument("--code", required=True, help="要修正的编码，如 ANYI-261022-01")
    p.add_argument("--citizen", dest="citizen_id", required=True,
                   help="新的归属公民 ID")
    p.add_argument("--reason", default=None, help="修正理由（写进 memory.owner_fixed 事件）")
    p.set_defaults(func=_f.cmd_owner_fix)

    # 工作区 → 项目映射（R1 根治）：项目归属的权威来源。
    # **CLI 专属**，不注册成 MCP 工具：维护映射是人的决定，不是 AI 每次调用顺手改的东西。
    p = sub.add_parser("workspace", help="工作区 → 项目映射（项目归属的权威来源）")
    wsub = p.add_subparsers(dest="workspace_action", required=True)

    wp = wsub.add_parser("set", help="新增/更新一条映射")
    wp.add_argument("path", help="工作区路径（如 I:/app/anyi-studio）；存库前会归一化")
    wp.add_argument("project", help="项目前缀（如 ANYI）")
    wp.add_argument("--note", default=None, help="备注（给人看的，如“主代码库”）")
    wp.set_defaults(func=_f.cmd_workspace)

    wsub.add_parser("list", help="列出全部映射").set_defaults(func=_f.cmd_workspace)

    wp = wsub.add_parser("remove", help="删除一条映射（幂等）")
    wp.add_argument("path")
    wp.set_defaults(func=_f.cmd_workspace)

    wp = wsub.add_parser("seed", help="从现有记忆反推候选（只打印，不写入）")
    wp.set_defaults(func=_f.cmd_workspace)

    p = sub.add_parser("inject")
    p.add_argument("--citizen", default=None)
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--project", default=None,
                   help="显式声明当前项目前缀（最高优先）；不传则按"
                        "会话档期 → workspace_map(--cwd) → 最近更新推断")
    p.add_argument("--cwd", default=None,
                   help="工作目录：用于 workspace_map 最长前缀匹配（不传则不查映射）")
    # 2026-10-26（评审 🟠3）：CLI 补上 MCP 早有的两个段选择旋钮（同一份归一实现）。
    p.add_argument("--sections", default=None,
                   help="只构建这些段（逗号或空格分隔，缺省=全段）："
                        + "/".join(INJECTION_SECTIONS)
                        + "；L0 身份段永远保留（不可关闭）")
    p.add_argument("--no-board", dest="no_board", action="store_true",
                   help="不要看板段（等价于 --sections 里不含 board；MCP 的 "
                        "include_board=false）")
    p.add_argument("--auto-presence", dest="auto_presence", action="store_true",
                   help="顺带自动签到（WorkBuddy 会话开场用；MCP 的 inject 不写）")
    p.add_argument("--session-key", dest="session_key", default=None,
                   help="自动签到用的会话键（缺省读环境变量 FISH_SESSION_KEY）；"
                        "只与 --auto-presence 一起生效")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_f.cmd_inject)

    p = sub.add_parser("report")
    p.add_argument("--days", type=int, default=1, help="统计最近 N 天（默认 1=今日）")
    p.add_argument("--save", action="store_true", help="同时写入 data/_reports/")
    p.set_defaults(func=_f.cmd_report)

    p = sub.add_parser("config")
    p.add_argument("action", choices=["get", "set", "list"])
    p.add_argument("key", nargs="?", default=None)
    p.add_argument("value", nargs="?", default=None)
    p.set_defaults(func=_f.cmd_config)

    sub.add_parser("healthcheck").set_defaults(func=_f.cmd_healthcheck)
    sub.add_parser("rebuild").set_defaults(func=_f.cmd_rebuild)
    sub.add_parser("reindex").set_defaults(func=_f.cmd_reindex)

    # `fish ui serve|dump`（2026-10-28 从生产树移植）：只读读口（给界面用）。
    # 纪律全落在 fish_ui.py 里（mode=ro + query_only=1 + 只绑回环）。
    p = sub.add_parser("ui", help="只读读口：把库投影成界面要的 JSON（serve / dump）")
    usub = p.add_subparsers(dest="ui_action", required=True)
    us = usub.add_parser("serve", help="起只读 HTTP 读口（默认只绑 127.0.0.1）")
    us.add_argument("--host", default="127.0.0.1")
    us.add_argument("--port", type=int, default=8787)
    us.add_argument("--minutes", type=int, default=30)
    us.set_defaults(func=_f.cmd_ui)
    ud = usub.add_parser("dump", help="打印一份快照就退出（调试 / 对拍用）")
    ud.add_argument("--minutes", type=int, default=30)
    ud.set_defaults(func=_f.cmd_ui)

    # 归档（2026-10-25 · 用户要求）：把已失效的正文移进 archive/<年>/，**只改位置不改语义**。
    # 判据直接复用已有的 status（superseded / withdrawn）——不新造"归档状态"，
    # 所以推送/召回（它们硬过滤 status='active'）的行为一个字节都不会变。
    p = sub.add_parser("archive",
                       help="归档：把 superseded/withdrawn 的正文移进 archive/<年>/（不动 active）")
    p.add_argument("action", nargs="?", default="sweep",
                   choices=["sweep", "list", "one"],
                   help="list=只看清单 / one=单条 / sweep=批量（默认）")
    p.add_argument("code", nargs="?", default=None, help="one：要归档的编码")
    p.add_argument("--dry-run", action="store_true", help="只看要动哪些，不真移")
    p.add_argument("--year", default=None, help="归档到哪一年（缺省取记录年份）")
    p.add_argument("--limit", type=int, default=None, help="最多处理几条")
    p.add_argument("--no-withdrawn", dest="withdrawn", action="store_false",
                   default=True, help="只归档 superseded，不含 withdrawn")
    # FB-12：同一条命令承担两个归档面——正文（默认）与公民（--citizens）。
    # 两边都默认演练、都靠 --apply 真执行，口径统一。
    p.add_argument("--citizens", action="store_true",
                   help="改归档**公民**（闲置 ≥N 天且无档期/无凭证/非主人）；"
                        "与正文归档互斥，缺省是归档正文")
    p.add_argument("--days", type=int, default=None,
                   help="--citizens：闲置多少天算可归档（缺省取 config "
                        "session.archive_after_days，其值 0 = 不自动归档）")
    p.add_argument("--apply", action="store_true",
                   help="--citizens：真的搬行；不加只打印计划（默认演练）")
    p.set_defaults(func=_f.cmd_archive)

    # 推送（P1-a · E 批）。变量名刻意避开 `ap` / `parser`：
    # 2026-10-25 踩过的坑是"子命令循环变量覆盖了 main() 的根 parser ap"，
    # 症状是整个 CLI 崩掉（22 条判据红）。这里的名字与根 parser 无重合。
    pushp = sub.add_parser("push", help="推送：stats 效果度量 / status 当前配置")
    pushsub = pushp.add_subparsers(dest="action", required=True)
    push_stats_p = pushsub.add_parser("stats", help="四个指标分开报（采纳/盲区/违规/方差）")
    push_stats_p.add_argument("--days", type=int, default=30)
    push_stats_p.set_defaults(func=_f.cmd_push)
    pushsub.add_parser("status", help="看 push.* 配置现值").set_defaults(func=_f.cmd_push)
    pushsub.add_parser("sweep",
                       help="把超窗仍无反应的推送判为 ignored（幂等；日报也跑它）"
                       ).set_defaults(func=_f.cmd_push)

    # 动作流（2026-10-26 · T02）：stats 是"一小时多少写动作"的固定读数；prune 供日报/手动清理。
    # **CLI 专属**：动作流是审计数据，看它的是人（跑日报、查误报），不是模型每次对话都调的东西。
    actp = sub.add_parser("activity", help="动作流：stats 读数 / prune 清理（CLI 专属）")
    actsub = actp.add_subparsers(dest="action", required=True)
    act_stats = actsub.add_parser("stats", help="写动作统计（行数/会话/高频目录/按小时）")
    act_stats.add_argument("--days", type=int, default=1)
    act_stats.set_defaults(func=_f.cmd_activity)
    act_prune = actsub.add_parser("prune", help="按保留期清理（缺省取 activity.retention_days）")
    act_prune.add_argument("--days", type=int, default=None)
    act_prune.set_defaults(func=_f.cmd_activity)

    # 原始对话语料层（T02）：子命令面在 `fish_corpus.register_cli`（J4 行数预算把
    # argparse 注册赶回领域模块；`_f.cmd_corpus` 是薄壳，由这里传入）。
    fish_corpus.register_cli(sub, _f.cmd_corpus)


    # 提示词递送（2026-10-26 · T04）：**读 stdin payload**，输出纯文本。
    # 只给 hook 用，不是给人直接敲的命令（所以 help 里写明输入形态）。
    ppush = sub.add_parser("prompt-push",
                           help="提示词时机递记忆：读 stdin 的 hook payload，输出纯文本")
    ppush.add_argument("--host", default="dsh", choices=["dsh", "workbuddy", "codebuddy"],
                       help="宿主方言（决定会话键前缀）")
    ppush.set_defaults(func=_f.cmd_prompt_push)

    p = sub.add_parser("withdraw")
    p.add_argument("code")
    p.add_argument("--reason", default="")
    p.set_defaults(func=_f.cmd_withdraw)

    # 替代（FB-11）：MCP 早就有一个 `supersede` 工具，CLI 侧却没有任何入口，
    # 于是"两条路"里的另一条只能靠人手改库。补上它，且**回执与 MCP 同口径**。
    p = sub.add_parser("supersede",
                       help="把旧编码标记为被新编码替代（回执与 MCP 的 supersede 工具一致）")
    p.add_argument("code", help="被替代的旧编码")
    p.add_argument("--by", required=True, metavar="NEW_CODE", help="替代它的新编码")
    p.add_argument("--reason", default="", help="为什么替代（写进 memory.superseded 事件）")
    p.set_defaults(func=_f.cmd_supersede)

    p = sub.add_parser("events"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_f.cmd_events)

    # ---- 主人画像（R1-1/R1-2/R1-4/R1-5/R1-6，2026-11-11）----
    # 人通道：set/remove/accept/reject/render/sync/ask/defer。
    # **AI 通道（MCP）没有 set**——写权分离发生在接口层，不是靠自觉。
    p = sub.add_parser("profile", help="主人画像：固定槽位（一格一值，覆盖不追加）")
    psub = p.add_subparsers(dest="profile_action", required=True)

    pp = psub.add_parser("show", help="看当前画像（含待采纳建议数）")
    pp.add_argument("--group", default=None)
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("set", help="【人】写一格（覆盖，不是追加）")
    pp.add_argument("group", help="称呼/生活/工作/性格/近期动态")
    pp.add_argument("name", help="该组下的槽名")
    pp.add_argument("value")
    pp.add_argument("--reason", default="")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("remove", help="【人】清空一格")
    pp.add_argument("slot_key", help="形如 称呼.名字")
    pp.add_argument("--reason", default="")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("propose", help="【AI】提建议——**不改画像**，等人采纳")
    pp.add_argument("group"); pp.add_argument("name")
    pp.add_argument("value", nargs="?", default=None)
    pp.add_argument("--remove", action="store_true", help="建议清空这一格")
    pp.add_argument("--rationale", default="")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("proposals", help="看待采纳建议")
    pp.add_argument("--status", default="pending")
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("accept", help="【人】采纳一条建议（此刻才改画像）")
    pp.add_argument("proposal_id")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("reject", help="【人】拒绝一条建议")
    pp.add_argument("proposal_id")
    pp.add_argument("--note", default="")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("render", help="渲染 <data_root>/profile/画像.md（人的编辑面）")
    pp.add_argument("--force", action="store_true", help="丢弃文件里人在库外的改动")
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("sync", help="把文件里的编辑导入（文件 → 事件 → 槽位）")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("ask", help="看三问 / 回答（R1-4 提问节律）")
    pp.add_argument("--answer", default=None)
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)

    pp = psub.add_parser("defer", help="本轮不回答（会留痕，同样清零）")
    pp.add_argument("--as", dest="as_actor", default=None)
    pp.set_defaults(func=_f.cmd_profile)


    args = ap.parse_args()
    # 2026-10-26（复审 🟡N4）：**子命令的返回码必须传播出去。**
    #
    # 旧实现只写 `args.func(args)`，返回值被丢掉 —— 于是所有"参数错误/失败"的
    # 返回码（`_f.cmd_board` 缺 `--citizen` 的 2、`_f.cmd_archive one` 缺 code 的 2、
    # `archive sweep` 有错误时的 1）在 CLI 边界全部变成 **exit 0**。
    # 后果不是"少个提示"：脚本与 CI 读到的是"成功"，而动作根本没发生。
    # 这里只传播**非零 int**（None = 正常结束，保持原样）。
    rc = args.func(args)
    if isinstance(rc, int) and not isinstance(rc, bool) and rc:
        sys.exit(rc)


