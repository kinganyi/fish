#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB-15（批 E）：distill 提炼建议层——**不自动入库**的判据（2026-10-26）。

## 本轮的边界（三条硬约束，每条都有判据盯着）

  · **不自动入库** → E4 断言写建议前后 `code_index` 全量行 + `events` 行数**完全不变**；
  · **保留溯源**   → E4 断言建议文件里三个 source episode 的 code 全在；
  · **原 episode 不删** → E4 同一条断言（行数 / status 不变；本轮也不标 `distilled_into`）。

## 判据（对应交付说明的 E1–E5）

  E1  3 条同 tag 的 episode（窗口内）→ 恰好一个 cluster，含 3 个 code（组内按 code 升序）
  E2  另 1 条不同 tag（组内 1 条，min=2）→ 不出现
  E3  superseded / withdrawn 的 episode 不计入；超窗的不计入
  E4  `--write` 生成建议文件：含全部 3 个 code 与"建议、不是记忆"声明；
      `code_index` 行数 / status / `events` 行数三者前后完全不变
  E5  无 cluster → 提示文案、rc=0、不落文件

## 为什么判据盯函数 + CLI 两条入口

`distill_clusters` / `write_distill_suggestion` 是能力面，`cmd_distill`（经 `main()`
的真实 parser）是入口面。只测函数会漏掉"参数没接上"，只测 CLI 会把失败原因藏在
文本文案里。两边各测一半，出问题能直接指到哪一层。

## 变异自证（交付说明第 3 条，人工各跑一次，不复跑在 CI 里）

  ① 删掉 `distill_clusters` 里的 `if len(eps) < min_group` 判断 → E2 必红；
  ② `write_distill_suggestion` 里顺手 `INSERT` 一条记忆 → E4 必红。
"""
import argparse
import contextlib
import datetime
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

PROJECT = "测试项目"
ALIAS = "别名项目"


def _z_ago(days: int, *, minutes: int = 0) -> str:
    """`days` 天（可再加 `minutes` 分钟）前的 UTC ISO，与库里的口径一致。"""
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=days, minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Feedback261026ETest(unittest.TestCase):
    """批 E：FB-15 提炼建议层（只读聚类 + 建议文件）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self._project()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:               # noqa: BLE001 —— 收尾不该掩盖断言失败
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _project(self, prefix=PROJECT):
        """登记项目前缀（`next_code` 要求前缀已在册，与 `project-add` 落的一样）。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
            "status) VALUES (?,?,NULL,0,'active')", (prefix, prefix))
        self.conn.commit()
        return prefix

    def _episode(self, title, tags, *, project=PROJECT):
        """走真实写入入口落一条 episode（不手写 INSERT）。"""
        code, _rel = fish.record_memory(
            self.conn, self.root, project=project, title=title, body="正文占位",
            mem_type="episode", tags=list(tags))
        return code

    def _age(self, code, days):
        """把 `recorded_at` 改到 `days` 天前（"超窗"夹具）。"""
        self.conn.execute("UPDATE code_index SET recorded_at=? WHERE code=?",
                          (_z_ago(days), code))
        self.conn.commit()

    def _three_same_tag(self):
        return sorted([self._episode("事件一", ["主题A"]),
                       self._episode("事件二", ["主题A"]),
                       self._episode("事件三", ["主题A"])])

    def _suggestion_dir(self, project=PROJECT):
        return self.root / "projects" / project / fish.DISTILL_DIRNAME

    def _snapshot(self):
        """真相快照：code_index 全量行 + events 行数（写建议必须一个字节都不动它）。"""
        rows = self.conn.execute("SELECT * FROM code_index ORDER BY code").fetchall()
        events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return rows, events

    def _run_cli(self, *argv):
        """走 `main()` 的真实 parser（这条断言的是"参数真的接上了"）。"""
        out, err = io.StringIO(), io.StringIO()
        old = sys.argv
        sys.argv = ["fish.py", "--root", str(self.root)] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                fish.main()
        finally:
            sys.argv = old
        return out.getvalue(), err.getvalue()

    # ------------------------------------------------------------- E1 同主题成组
    def test_e1_same_tag_makes_one_cluster(self):
        codes = self._three_same_tag()

        res = fish.distill_clusters(self.conn, project=PROJECT)

        self.assertEqual(res["checked"], 3, "窗口内 episode 总数应为 3")
        self.assertEqual(res["project"], PROJECT)
        self.assertEqual(len(res["clusters"]), 1, "只有一个主题，应当只有一组")
        cluster = res["clusters"][0]
        self.assertEqual(cluster["tag"], "主题A")
        self.assertEqual(cluster["count"], 3)
        self.assertEqual([ep["code"] for ep in cluster["episodes"]], codes,
                         "组内必须按 code 升序")
        self.assertEqual(cluster["episodes"][0]["title"], "事件一")
        self.assertTrue(cluster["episodes"][0]["recorded_at"])

    def test_e1b_project_alias_is_expanded(self):
        """项目口径与 `list_memories` 同一条：别名查询要看到规范名下的 episode。"""
        codes = self._three_same_tag()
        fish.link_alias(self.conn, "project", ALIAS, PROJECT, reason="E1b 测试")

        via_alias = fish.distill_clusters(self.conn, project=ALIAS)
        via_name = fish.distill_clusters(self.conn, project=PROJECT)

        self.assertEqual([ep["code"] for ep in via_alias["clusters"][0]["episodes"]],
                         codes)
        self.assertEqual(via_alias["clusters"], via_name["clusters"])

    # ------------------------------------------------------------- E2 min 阈值
    def test_e2_singleton_tag_is_dropped(self):
        self._three_same_tag()
        lonely = self._episode("孤立事件", ["主题B"])

        res = fish.distill_clusters(self.conn, project=PROJECT, min_group=2)

        tags = [c["tag"] for c in res["clusters"]]
        self.assertEqual(tags, ["主题A"], "组内只有 1 条的 tag 不该成组")
        self.assertNotIn(lonely,
                         [ep["code"] for c in res["clusters"] for ep in c["episodes"]])
        self.assertEqual(res["checked"], 4, "checked 统计的是窗口内全部 episode")

        # 阈值放低到 1 时它必须出现（证明是阈值在起作用，不是别处丢了数据）
        loose = fish.distill_clusters(self.conn, project=PROJECT, min_group=1)
        self.assertIn("主题B", [c["tag"] for c in loose["clusters"]])

    # ------------------------------------------------------- E3 状态与窗口过滤
    def test_e3_status_and_window_filters(self):
        keep = self._three_same_tag()

        # superseded：两条同 tag，其一被替代 → 只剩 1 条 active → 不成组
        b1 = self._episode("旧说法", ["主题B"])
        b2 = self._episode("新说法", ["主题B"])
        fish.supersede_memory(self.conn, b1, b2, reason="E3 测试")

        # withdrawn：两条同 tag，其一作废 → 只剩 1 条 active → 不成组
        d1 = self._episode("要作废", ["主题D"])
        self._episode("留着", ["主题D"])
        fish.withdraw_memory(self.conn, d1, reason="E3 测试")

        # 超窗：两条同 tag，但 recorded_at 在 40 天前（窗口 30 天）
        e1 = self._episode("很久以前一", ["主题E"])
        e2 = self._episode("很久以前二", ["主题E"])
        self._age(e1, 40)
        self._age(e2, 41)

        res = fish.distill_clusters(self.conn, project=PROJECT, days=30)

        tags = [c["tag"] for c in res["clusters"]]
        self.assertEqual(tags, ["主题A"], "superseded / withdrawn / 超窗都不许成组")
        self.assertEqual([ep["code"] for ep in res["clusters"][0]["episodes"]], keep)
        # 窗口内 active：3（主题A）+ b2 + "留着" = 5；b1/d1/两条超窗都不在其中
        self.assertEqual(res["checked"], 5)
        all_codes = [ep["code"] for c in res["clusters"] for ep in c["episodes"]]
        for gone in (b1, d1, e1, e2):
            self.assertNotIn(gone, all_codes)

    # --------------------------------------------------- E4 --write 不碰真相
    def test_e4_write_suggestion_leaves_truth_untouched(self):
        codes = self._three_same_tag()

        # ① 演练：打印聚类、不落任何文件
        out, err = self._run_cli("distill", PROJECT, "--days", "30", "--min", "2")
        self.assertEqual(err, "")
        self.assertIn("主题A", out)
        self.assertIn("3 条", out)
        self.assertIn(codes[0], out)
        self.assertIn("演练：未落文件", out)
        self.assertFalse(self._suggestion_dir().exists(), "演练不许建目录、不许落文件")

        before_rows, before_events = self._snapshot()

        # ② --write：生成建议文件
        out2, err2 = self._run_cli("distill", PROJECT, "--days", "30", "--min", "2",
                                   "--write")
        self.assertEqual(err2, "")
        self.assertIn("建议已写入", out2)
        files = sorted(self._suggestion_dir().glob("*.md"))
        self.assertEqual(len(files), 1, f"应恰好一个建议文件，实际 {files}")
        text = files[0].read_text(encoding="utf-8")

        # 保留溯源：三个 source episode 的 code 全在
        for code in codes:
            self.assertIn(code, text)
        self.assertIn("事件一", text)
        # 声明：这是建议、不是记忆
        self.assertIn("本文件是**建议**，不是记忆", text)
        self.assertIn("未入库", text)
        self.assertIn("不自动生效", text)
        self.assertIn("as-of ", text)
        self.assertIn("## 1. 主题A（3 条）", text)

        # 写建议不碰真相：code_index 全量行 + events 行数完全不变
        after_rows, after_events = self._snapshot()
        self.assertEqual(len(after_rows), len(before_rows))
        self.assertEqual(len(after_rows), 3)
        self.assertEqual(after_rows, before_rows,
                         "文案写建议不许改 code_index（status / 任何列都不许动）")
        self.assertEqual(after_events, before_events, "文案写建议不许写 events")
        self.assertEqual([r[6] for r in after_rows], ["active"] * 3)

    # --------------------------------------------------------- E5 无组 → 空跑
    def test_e5_no_cluster_prints_hint_and_writes_nothing(self):
        self._episode("独立一", ["独1"])
        self._episode("独立二", ["独2"])

        buf = io.StringIO()
        args = argparse.Namespace(root=str(self.root), project=PROJECT, days=30,
                                  min=2, max_groups=10, write=True)
        with contextlib.redirect_stdout(buf):
            rc = fish.cmd_distill(args)

        self.assertEqual(rc, 0, "没东西可提炼不是错误")
        out = buf.getvalue()
        self.assertIn("未找到可提炼的同主题 episode", out)
        self.assertIn("窗口 30 天", out)
        self.assertIn("≥2 条", out)
        self.assertFalse(self._suggestion_dir().exists(),
                         "无 cluster 时即使带 --write 也不许落文件")

    # ------------------------------------------------------------ 参数校验
    def test_bad_arguments_are_rejected(self):
        self._three_same_tag()
        with self.assertRaises(ValueError):
            fish.distill_clusters(self.conn, project="  ")
        with self.assertRaises(ValueError):
            fish.distill_clusters(self.conn, project=PROJECT, days=-1)
        with self.assertRaises(ValueError):
            fish.distill_clusters(self.conn, project=PROJECT, min_group=0)
        with self.assertRaises(ValueError):
            fish.distill_clusters(self.conn, project=PROJECT, now="不是时间")
        with self.assertRaises(ValueError):
            fish.write_distill_suggestion(self.root, "", {"clusters": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
