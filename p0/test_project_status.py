#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B2 · 项目状态表（`fish project status`）判据（2026-10-26）。

被测定义（PST-1~9 + 裁决 Q4/Q5/Q6/Q7）：
  六列严格顺序（项目 / 状态 / 最近活跃 / 看板入口 / 主责 AI / 未了事项）；
  三种显式 `—`（无记忆 / 今日无看板 / 无开档期）+ 未了事项 0 的裸 `—`；
  每格都带来源（`sources`）；`retired` 折进文末且计数与 registry 一致；
  同一 `--as-of` 两次渲染逐字节全等；`--json` 不写文件。

## 变异自证（X-1：每条判据都必须能变红）

变异 A —— 对应 `test_fake_prefix_appears_and_disappears`：
  把项目来源改成硬编码列表（比如写死 `["P1"]`）→ 插入假前缀 `FISHTEST` 后表里
  不出现它 → 本测试红。**这条是本文件最重要的一条**：状态表的价值等于
  "它真的在读库"，一旦变成硬编码，它就成了第二份真相。

变异 B —— 对应 `test_open_loop_window_uses_30_days`：
  把 `PST_OPEN_WINDOW_DAYS` 改成 0 → 5 天前那条未闭合记忆不再被计数 →
  "未了事项 = 1" 的断言红。

变异 C —— 对应 `test_retired_folded_with_registry_count`：
  把 retired 混进主表（`visible = rows` 写死）→ 主表出现 `OLD` 且折叠区消失 → 红。

变异 D —— 对应 `test_null_cells_are_explicit_dashes`：
  把 `None` 直接格式化（`str(None)`）→ 表里出现 `None` → 红。

纪律（K12）：`tempfile.mkdtemp()` 隔离；**不指向 `p0/data`**。
"""
import contextlib
import datetime
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
AS_OF = datetime.datetime(2026, 10, 26, 0, 0, 0, tzinfo=TZ8)
CID_A = "01PSTA0000000000000000A1"
CID_B = "01PSTB0000000000000000B2"
CID_C = "01PSTC0000000000000000C3"


def _z(days_ago=0, hours_ago=0):
    """as-of 前 N 天 → 数据库口径（UTC + Z），免得夹具里手算时区。"""
    dt = AS_OF - datetime.timedelta(days=days_ago, hours=hours_ago)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProjectStatusTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-pst-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.home = fish.resolve_home_root(self.root, conn=self.conn)
        self._citizen(CID_A, "小甲")
        self._citizen(CID_B, "小乙")
        self._citizen(CID_C, "小丙")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- 夹具
    def _citizen(self, cid, name):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,"
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (cid, name, "resident", "member", None, "active", fish.now_utc()))

    def _registry(self, prefix, namespace="", status="active"):
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES (?,?,NULL,0,?)", (prefix, namespace, status))
        self.conn.commit()

    def _memory(self, code, project, *, title="普通记忆", owner=CID_A,
                recorded_at=None, status="active"):
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,status,"
            "file_path,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (code, title, project, owner, "fact", status,
             f"projects/{project}/{code}__x.md", recorded_at or _z(1)))
        self.conn.commit()

    def _stint(self, cid, project, *, days_ago=1, to_ts=None):
        self.conn.execute(
            "INSERT INTO project_stints (citizen_id,project,from_ts,to_ts) "
            "VALUES (?,?,?,?)", (cid, project, _z(days_ago), to_ts))
        self.conn.commit()

    def _render(self, **kw):
        return fish.render_project_status(self.conn, self.root, self.home,
                                          as_of=kw.pop("as_of", AS_OF), **kw)

    def _text(self):
        return (self.home / fish.LAYOUT_STATUS_TABLE).read_text(encoding="utf-8")

    def _rows(self, **kw):
        return fish.project_status_rows(self.conn, self.root,
                                        as_of=kw.pop("as_of", AS_OF),
                                        home_root=self.home, **kw)

    def _run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["fish.py"] + argv
        out, err = io.StringIO(), io.StringIO()
        code = None
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    fish.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    # ============================================================ 结构
    def test_six_columns_strict_order(self):
        """PST-1：六列严格顺序（函数契约 + 渲染表头两处都断言）。"""
        self._registry("P1", "项目一")
        res = self._rows()
        self.assertEqual(res["columns"], list(fish.PST_COLUMNS))
        self.assertEqual(res["columns"],
                         ["项目", "状态", "最近活跃", "看板入口", "主责 AI", "未了事项"])
        self._render()
        header = [ln for ln in self._text().splitlines() if ln.startswith("| 项目 |")]
        self.assertTrue(header, "状态表缺六列表头")
        cells = [c.strip() for c in header[0].strip("|").split("|")]
        self.assertEqual(cells, list(fish.PST_COLUMNS), "表头顺序必须与 PST_COLUMNS 一致")

    def test_three_explicit_dashes_and_no_none(self):
        """PST-5 / 变异 D：三种 `—` 各自出现，且表里绝不出现 `None`/空串冒充。

        ⚠️ 判据真值**写字面量**、不读常量：第一版写成 `assertIn(fish.PST_DASH_NO_OWNER, text)`，
        变异"把常量改成空串"时两边一起变，`assertIn("", text)` 恒真 → 变异抓不住。
        这正是"判据真值必须独立于被测实现"的具体例子（脚本 `mutation_b2b3_selfcheck.py`
        的 M-B2-4 就是把它抓出来的那条变异）。
        """
        self._registry("EMPTY", "什么都没有的项目")
        self._render()
        text = self._text()
        self.assertIn("—（无记忆）", text, "缺「—（无记忆）」")
        self.assertIn("—（今日无看板）", text, "缺「—（今日无看板）」")
        self.assertIn("—（无开档期）", text, "缺「—（无开档期）」")
        self.assertEqual(fish.PST_DASH_NO_OWNER, "—（无开档期）", "常量与判据字面量必须一致")
        self.assertEqual(fish.PST_DASH_NO_MEMORY, "—（无记忆）")
        self.assertEqual(fish.PST_DASH_NO_BOARD, "—（今日无看板）")
        self.assertNotIn("None", text, "变异 D 的证明点：None 不许漏进产物")
        row = self._rows()["rows"][0]
        self.assertEqual(row["last_active"], fish.PST_DASH_NO_MEMORY)
        self.assertEqual(row["board_entry"], fish.PST_DASH_NO_BOARD)
        self.assertEqual(row["owners"], fish.PST_DASH_NO_OWNER)
        self.assertEqual(row["open_loops"], 0)

    def test_every_cell_has_source(self):
        """PST-3：每行 5 个来源键（项目列不需要来源——它就是 registry 的 key）。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1")
        self._stint(CID_A, "P1")
        row = self._rows()["rows"][0]
        self.assertEqual(set(row["sources"]),
                         {"status", "last_active", "board_entry", "owners", "open_loops"})
        for key in ("status", "last_active", "board_entry", "owners", "open_loops"):
            self.assertTrue(row["sources"][key], f"{key} 的 sources 不该是空串")
        self.assertEqual(row["sources"]["status"], "code_registry.status")
        self.assertIn("project_stints", row["sources"]["owners"])

    def test_search_table_includes_project_and_status_label(self):
        """状态标签：active→进行中 / closed→已闭合；项目列带 `prefix`。"""
        self._registry("P1", "项目一", "active")
        self._registry("P2", "", "closed")
        rows = self._rows()["rows"]
        by_prefix = {r["prefix"]: r for r in rows}
        self.assertEqual(by_prefix["P1"]["status_label"], "进行中")
        self.assertEqual(by_prefix["P2"]["status_label"], "已闭合")
        self._render()
        text = self._text()
        self.assertIn("项目一（`P1`）", text)
        self.assertIn("`P2`", text, "无 namespace 时至少显示前缀")

    def test_unknown_status_is_labelled_and_footnoted(self):
        """未知状态原样显示 + 标注「未登记状态」（不静默当成 active）。"""
        self._registry("WEIRD", "怪状态", "paused")
        row = self._rows()["rows"][0]
        self.assertIn("paused", row["status_label"])
        self.assertIn("未登记状态", row["status_label"])

    # ============================================================ 数据源真实
    def test_fake_prefix_appears_and_disappears(self):
        """**变异 A**：状态表必须真的读 `code_registry`（假前缀必现 / 删后必消失）。"""
        self._registry("P1", "项目一")
        self._registry("FISHTEST", "假前缀")
        self._render()
        self.assertIn("FISHTEST", self._text(), "新前缀没出现在状态表里——它在读硬编码？")
        self.conn.execute("DELETE FROM code_registry WHERE prefix='FISHTEST'")
        self.conn.commit()
        self._render()
        self.assertNotIn("FISHTEST", self._text(), "删掉前缀后状态表仍显示它")

    def test_last_active_from_code_index(self):
        """最近活跃 = `max(code_index.recorded_at)`，显示到日。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", recorded_at=_z(3))
        self._memory("P1-261026-02", "P1", recorded_at=_z(1))
        row = self._rows()["rows"][0]
        self.assertEqual(row["last_active"],
                         (AS_OF - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))
        self.assertIn("code_index", row["sources"]["last_active"])

    def test_last_active_respects_as_of_upper_bound(self):
        """as-of 之后写的记忆不得出现在"历史快照"里（否则快照语义是假的）。"""
        self._registry("P1", "项目一")
        self._memory("P1-261027-01", "P1", recorded_at=_z(-1))   # 1 天后
        row = self._rows()["rows"][0]
        self.assertEqual(row["last_active"], fish.PST_DASH_NO_MEMORY)

    # ============================================================ 主责 AI（Q4）
    def test_owners_open_stint_priority(self):
        """① 开档期优先，并给出显示名（不是 ULID）。"""
        self._registry("P1", "项目一")
        self._stint(CID_A, "P1")
        self._memory("P1-261026-01", "P1", owner=CID_B)   # 有记忆但没开档 → 不采用
        row = self._rows()["rows"][0]
        self.assertEqual(row["owners"], "小甲")
        self.assertIn("project_stints", row["sources"]["owners"])

    def test_owners_multiple_shows_first_two_and_total(self):
        """多名：前 2 + 「等 N 人」（N = 该项目总人数）。"""
        self._registry("P1", "项目一")
        self._stint(CID_A, "P1", days_ago=3)
        self._stint(CID_B, "P1", days_ago=2)
        self._stint(CID_C, "P1", days_ago=1)
        row = self._rows()["rows"][0]
        self.assertEqual(row["owners"], "小甲、小乙等 3 人")

    def test_owners_fallback_recent_owner_is_marked(self):
        """② 无开档期 → 近 7 天记忆 owner，来源必须显式标 `recent_owner`。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", owner=CID_B, recorded_at=_z(2))
        row = self._rows()["rows"][0]
        self.assertEqual(row["owners"], "小乙")
        self.assertIn("recent_owner", row["sources"]["owners"])

    def test_owners_fallback_window_is_7_days(self):
        """② 的窗口是 7 天：8 天前的记忆不能冒充"最近在干的人"。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", owner=CID_B, recorded_at=_z(8))
        row = self._rows()["rows"][0]
        self.assertEqual(row["owners"], fish.PST_DASH_NO_OWNER)
        self.assertEqual(row["sources"]["owners"], "none")

    def test_closed_stint_does_not_count(self):
        """只有 `to_ts IS NULL` 的开档期才算"当前主责"；已关档期不算。"""
        self._registry("P1", "项目一")
        self._stint(CID_A, "P1", days_ago=5, to_ts=_z(2))
        row = self._rows()["rows"][0]
        self.assertEqual(row["owners"], fish.PST_DASH_NO_OWNER)

    # ============================================================ 看板入口
    def test_board_entry_today(self):
        """当日项目板 → 相对链接（相对状态表所在目录）。"""
        self._registry("P1", "项目一")
        fish.board_ensure(self.conn, "project", "P1", period_key="261026")
        row = self._rows()["rows"][0]
        self.assertIn("看板-261026.md", row["board_entry"])
        self.assertIn("P1/看板-261026.md", row["board_entry"])
        self.assertIn("period=261026", row["sources"]["board_entry"])

    def test_board_entry_falls_back_to_latest_open(self):
        """今日无板 → 最近一条 open 板（来源标注不同）。"""
        self._registry("P1", "项目一")
        fish.board_ensure(self.conn, "project", "P1", period_key="261020")
        row = self._rows()["rows"][0]
        self.assertIn("看板-261020.md", row["board_entry"])
        self.assertIn("最近 open 板", row["sources"]["board_entry"])

    def test_board_entry_ignores_closed_old_board(self):
        """非当日且已闭的板不算入口（旧板归档后不该继续挂在主表上）。"""
        self._registry("P1", "项目一")
        b = fish.board_ensure(self.conn, "project", "P1", period_key="261010")
        self.conn.execute("UPDATE boards SET status='closed' WHERE id=?", (b["id"],))
        self.conn.commit()
        row = self._rows()["rows"][0]
        self.assertEqual(row["board_entry"], fish.PST_DASH_NO_BOARD)

    # ============================================================ 未了事项
    def test_open_loop_window_uses_30_days(self):
        """**变异 B**：30 天窗口；31 天前那条不计，5 天前那条计 1。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", title="下一步：写判据", recorded_at=_z(5))
        self._memory("P1-261026-02", "P1", title="待办：很久以前", recorded_at=_z(31))
        row = self._rows()["rows"][0]
        self.assertEqual(row["open_loops"], 1, "窗口口径错了（31 天前的被计入 或 5 天前的没计）")
        self._render()
        self.assertIn("| 1 |", self._text())

    def test_open_loops_zero_renders_bare_dash(self):
        """未了事项为 0 → 裸 `—`（不是 0，也不是某一种带括号的 `—`）。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", title="普通记忆", recorded_at=_z(1))
        row = self._rows()["rows"][0]
        self.assertEqual(row["open_loops"], 0)
        self._render()
        line = [ln for ln in self._text().splitlines() if "`P1`" in ln][0]
        self.assertTrue(line.rstrip().endswith("| — |"), line)
        self.assertEqual(fish.PST_DASH_NONE, "—")

    # ============================================================ retired（Q7）
    def test_retired_folded_with_registry_count(self):
        """**变异 C**：retired 不进主表；折叠区计数 == registry 的 retired 行数。"""
        self._registry("P1", "项目一")
        self._registry("OLD1", "旧一", "retired")
        self._registry("OLD2", "旧二", "retired")
        res = self._rows()
        self.assertEqual([r["prefix"] for r in res["rows"]], ["P1"])
        self.assertEqual({r["prefix"] for r in res["retired"]}, {"OLD1", "OLD2"})
        self._render()
        text = self._text()
        self.assertIn("<details><summary>已退役（2）</summary>", text)
        self.assertIn("OLD1", text.split("<details>", 1)[1], "折叠区里应列出退役项目")
        self.assertNotIn("OLD1", text.split("<details>", 1)[0], "主表里不该有退役项目")

    def test_include_retired_puts_them_in_main_table(self):
        """裁决 Q7 的逃生口：`--include-retired` 把 retired 放回主表。"""
        self._registry("P1", "项目一")
        self._registry("OLD1", "旧一", "retired")
        res = self._rows(include_retired=True)
        self.assertEqual({r["prefix"] for r in res["rows"]}, {"P1", "OLD1"})
        self.assertEqual(res["retired"], [], "已放进主表时 retired 列表应为空")
        self._render(include_retired=True)
        self.assertNotIn("<details>", self._text())

    # ============================================================ 降级（HOME-9 同构）
    def test_degraded_column_is_explicit_not_zero(self):
        """某张表坏了 → 那一格明说「数据源不可用」，其余格照常有值。"""
        self._registry("P1", "项目一")
        self._stint(CID_A, "P1")
        self.conn.execute("DROP TABLE code_index")
        self.conn.commit()
        res = self._rows()
        row = res["rows"][0]
        self.assertEqual(row["last_active"], fish.PST_DASH_UNAVAILABLE)
        self.assertEqual(row["open_loops"], fish.PST_DASH_UNAVAILABLE)
        self.assertEqual(row["owners"], "小甲", "坏一张表不该把整行都变成不可用")
        self.assertTrue(res["degraded"], "降级清单不能为空")
        self._render()
        text = self._text()
        self.assertIn("数据源不可用：", text)
        self.assertIn(fish.PST_DASH_UNAVAILABLE, text)

    def test_non_prefix_body_dirs_are_counted_not_listed(self):
        """Q6/Q18：正文目录里非前缀目录只计数（脚注），不进主表。"""
        self._registry("P1", "项目一")
        (self.root / "projects" / "P1").mkdir(parents=True, exist_ok=True)
        (self.root / "projects" / "_retired-ANYI-261001-01").mkdir(
            parents=True, exist_ok=True)
        res = self._rows()
        self.assertEqual(res["non_prefix_dirs"]["count"], 1)
        self.assertEqual([r["prefix"] for r in res["rows"]], ["P1"])
        footnote = " ".join(res["footnotes"])
        self.assertIn("不属于任何前缀的目录：1 个", footnote)
        self.assertIn("_retired-ANYI-261001-01", footnote)

    # ============================================================ 幂等 / 渲染
    def test_idempotent_same_as_of(self):
        """PST-4：同一 as-of 两次渲染，逐字节全等（除 as-of 行外 diff=0）。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1", title="下一步：甲")
        self._stint(CID_A, "P1")
        fish.board_ensure(self.conn, "project", "P1", period_key="261026")
        self._render()
        first = (self.home / fish.LAYOUT_STATUS_TABLE).read_bytes()
        self._render()
        second = (self.home / fish.LAYOUT_STATUS_TABLE).read_bytes()
        self.assertEqual(first, second, "同一 as-of 两次渲染字节不同")
        # 去掉 as-of 行再比一次：证明"全等"不是靠 as-of 行恰好相同
        strip = lambda b: b"\n".join(ln for ln in b.split(b"\n")
                                     if b"as-of" not in ln)
        self.assertEqual(strip(first), strip(second))

    def test_render_reports_path_bytes_sha256(self):
        """回执形状（K1）：path/bytes/sha256/rows/retired/degraded，且落点在 fish-home。"""
        self._registry("P1", "项目一")
        out = self._render()
        self.assertEqual(out["path"], fish.LAYOUT_STATUS_TABLE)
        target = self.home / out["path"]
        self.assertTrue(target.exists())
        self.assertEqual(out["bytes"], target.stat().st_size)
        self.assertEqual(out["sha256"], fish.kb_sha256_file(target))
        self.assertEqual(out["rows"], 1)
        self.assertEqual(out["retired"], 0)
        self.assertFalse((self.root / out["path"]).exists(), "状态表不许写进 data_root")

    def test_layout_render_uses_the_real_status_table(self):
        """B1 的 `layout render --all` 现在渲染真表（不再是占位页）。"""
        self._registry("P1", "项目一")
        self._stint(CID_A, "P1")
        res = fish.render_layout(self.conn, self.root, as_of=AS_OF)
        paths = {it["path"] for it in res["written"]}
        self.assertIn(fish.LAYOUT_STATUS_TABLE, paths)
        text = (self.home / fish.LAYOUT_STATUS_TABLE).read_text(encoding="utf-8")
        self.assertIn("| 项目 | 状态 | 最近活跃 | 看板入口 | 主责 AI | 未了事项 |", text)
        self.assertNotIn("占位", text, "占位页必须已被真表替换")

    # ============================================================ CLI
    def test_cli_json_is_parsable_and_writes_nothing(self):
        """PST-9：`--json` 输出 `{as_of, rows[{...,sources}]}`，且**不写文件**。"""
        self._registry("P1", "项目一")
        self._memory("P1-261026-01", "P1")
        code, out, err = self._run_main(
            ["--root", str(self.root), "project", "status",
             "--as-of", "2026-10-26T00:00:00+08:00", "--json"])
        self.assertIsNone(code, f"json 形态应 exit 0；stderr={err}")
        data = json.loads(out)
        self.assertEqual(data["as_of"], "2026-10-26T00:00:00+08:00")
        self.assertTrue(data["rows"])
        self.assertTrue(all("sources" in r for r in data["rows"]))
        self.assertEqual(data["columns"], list(fish.PST_COLUMNS))
        self.assertFalse((self.home / fish.LAYOUT_STATUS_TABLE).exists(),
                         "--json 不该写文件")

    def test_cli_render_prints_path_and_sha(self):
        code, out, err = self._run_main(
            ["--root", str(self.root), "project", "status",
             "--as-of", "2026-10-26T00:00:00+08:00"])
        self.assertIsNone(code, f"渲染应 exit 0；stderr={err}")
        self.assertIn(fish.LAYOUT_STATUS_TABLE, out)
        self.assertIn("主表", out)
        self.assertIn("重建命令", out)
        self.assertTrue((self.home / fish.LAYOUT_STATUS_TABLE).exists())

    def test_cli_bad_as_of_is_usage_error(self):
        """`--as-of` 解析失败 → exit 2（用法错误，不是域错误）。"""
        code, _out, err = self._run_main(
            ["--root", str(self.root), "project", "status", "--as-of", "昨天"])
        self.assertEqual(code, 2, f"应 exit 2；stderr={err}")
        self.assertIn("--as-of", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
