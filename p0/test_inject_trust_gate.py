#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-1（外部审查 2026-11-25）判据：注入包的**作者信任门**。

## 修的是什么（报告 §P0-1，阻断级）

进包侧（`fish_inject`）原来只按 `mem_type` / `status` / `project` 过滤，**不看作者**：

  · MCP 的 `session_start` 对任何调用方无门槛签发 visitor 公民 + token（自助身份）；
  · `lesson` 按 2026-11-11 的决策**免凭证可写**（本轮**不动**写入侧）。

两条放一起：任何本地 MCP 客户端写一条 `lesson`，它进的是**所有会话**的错题本段；
历史 visitor / `owner='unknown'` 作者的 `rule` 更是不用新写就已经在「规矩」段里
（生产库 2026-11-25 实测：rule 97 条里 11 条 visitor、14 条 unknown；lesson 159 条里
68 条 visitor）。「规矩」与「错题本」都进系统提示——这不是展示问题，是**提示注入 /
行为约束劫持**。

## 判定（唯一实现：`fish_inject._owner_trust_sql`）

    可信   = owner 非空、不是 unattributed/unknown、且不是 kind='visitor' 的公民
    不可信 = owner 为空 / unattributed / unknown / visitor

四个取数函数（`_rule_rows` / `count_rule_rows` / `_lesson_rows` / `count_lesson_rows`）
共用同一条 SQL；`build_short_rules_block` 的两栏正是走前两个函数，所以它没有第二份判定。

## 这批判据为什么能失败

每条主判据都造**两个作者**（resident / visitor）指向同一段文本，断言包里只有
resident 那条——把 `_owner_trust_sql` 的过滤删掉（变异 M-TRUST1），它们同时变红；
把开关支路写成"连 visitor 一起放行"（M-TRUST2），开关那条变红。
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish  # noqa: E402

RESIDENT = "C-RESIDENT-甲乙"
VISITOR = "C-VISITOR-自助"
PROJECT = "P"


class InjectTrustGateTest(unittest.TestCase):

    def setUp(self):
        # mkdtemp 而不是 TemporaryDirectory：Win 上 SQLite 的 -wal/-shm 句柄会让
        # GC 终结器的 rmtree 抛 PermissionError，那是**清理失败**不是判据失败。
        self._tmpdir = tempfile.mkdtemp(prefix="fish-trustgate-")
        self.data = Path(self._tmpdir) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for cid, kind, name in ((RESIDENT, "resident", "被授予者"),
                                (VISITOR, "visitor", "自助访客")):
            self.conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,host,status,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (cid, name, kind, "cli", "active", fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq)"
            " VALUES (?,'探针',NULL,0)", (PROJECT,))
        self.conn.commit()      # next_code 用 BEGIN IMMEDIATE，必须没有未结束的事务

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _lesson(self, title, owner):
        """走**真实写入路径**（`record_memory`）写一条 lesson。"""
        code, *_ = fish.record_memory(
            self.conn, self.data, project=PROJECT, title=title, body="# 探针\n",
            mem_type="lesson", owner=owner)
        self.conn.commit()
        return code

    def _rule(self, title, owner, *, project=PROJECT):
        """直接插投影——模拟**信任门之前**写下的历史行（旁路 record_memory 的凭证门）。"""
        ts = fish.now_utc()
        code = fish.next_code(self.conn, PROJECT)
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active','[]',NULL,?,?,0,0)",
            (code, title, project, owner, "rule", ts, ts))
        self.conn.commit()
        return code

    def _lesson_text(self):
        return fish.build_lesson_block(self.conn, project=PROJECT)["text"]

    def _rules(self):
        return fish.build_short_rules_block(self.conn, project=PROJECT)

    def _set_cfg(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO config (key,value,type,updated_at) VALUES (?,?,?,?)",
            (key, str(value), "int", "x"))
        self.conn.commit()

    # ------------------------------------------------------------ lesson 门
    def test_visitor_lesson_is_excluded_resident_kept(self):
        """**核心**：同标题两条 lesson，只有 resident 作者那条能进错题本段。"""
        self._lesson("被授予者写的教训", RESIDENT)
        self._lesson("自助访客写的教训", VISITOR)
        text = self._lesson_text()
        self.assertIn("被授予者写的教训", text, f"resident 的 lesson 没进包：{text!r}")
        self.assertNotIn("自助访客写的教训", text,
                         f"**visitor 作者的 lesson 进了注入包** —— 这就是 P0-1 的投毒面：\n{text!r}")

    def test_unattributed_lesson_is_excluded(self):
        """无主（unattributed）照旧不进包（F-P0-3 的口径不许被这次改动放宽）。"""
        self._lesson("无主教训", "unattributed")
        self._lesson("被授予者写的教训", RESIDENT)
        text = self._lesson_text()
        self.assertIn("被授予者写的教训", text)
        self.assertNotIn("无主教训", text, f"未归属 lesson 又进包了：{text!r}")

    def test_lesson_header_count_uses_the_same_gate(self):
        """段头 M 与列表同口径：两条 lesson 只一条可信 → 显示 1/1。"""
        self._lesson("被授予者写的教训", RESIDENT)
        self._lesson("自助访客写的教训", VISITOR)
        text = self._lesson_text()
        self.assertIn("1/1", text, f"段头计数没跟上信任门（应只有 1 条）：{text!r}")
        self.assertEqual(fish.count_lesson_rows(self.conn, "2000-01-01T00:00:00Z"), 1,
                         "count_lesson_rows 与列表口径不一致")

    # ------------------------------------------------------------ rule 门
    def test_visitor_rule_is_excluded_resident_kept(self):
        """**核心**：历史 visitor 作者的 rule 不许进「规矩」段（两栏都要判）。"""
        self._rule("被授予者写的本项目规矩", RESIDENT)
        self._rule("访客写进规矩的探针", VISITOR)
        self._rule("被授予者写的全局规矩", RESIDENT, project=None)
        self._rule("访客写进全局栏的探针", VISITOR, project=None)
        blk = self._rules()
        text = blk["text"]
        self.assertIn("被授予者写的本项目规矩", text, f"resident 的项目规矩没进包：{text!r}")
        self.assertIn("被授予者写的全局规矩", text, f"resident 的全局规矩没进包：{text!r}")
        self.assertNotIn("访客写进规矩的探针", text, f"visitor 的项目规矩进包了：{text!r}")
        self.assertNotIn("访客写进全局栏的探针", text, f"visitor 的全局规矩进包了：{text!r}")
        self.assertEqual(blk["total"], 2, f"段头总数把不可信作者算进去了：{blk!r}")

    def test_unknown_and_null_owner_rule_excluded(self):
        """`owner='unknown'` 与 owner 为空的历史 rule 都不进包（生产 14 条就是这一类）。"""
        self._rule("unknown 作者的规矩", "unknown")
        self._rule("没有作者列的规矩", None)
        self._rule("被授予者写的本项目规矩", RESIDENT)
        blk = self._rules()
        text = blk["text"]
        self.assertIn("被授予者写的本项目规矩", text, text)
        self.assertNotIn("unknown 作者的规矩", text, f"unknown 作者进包了：{text!r}")
        self.assertNotIn("没有作者列的规矩", text, f"空 owner 进包了：{text!r}")
        self.assertEqual(fish.count_rule_rows(self.conn, project=PROJECT), 1,
                         "count_rule_rows 没有按信任门过滤")

    # ------------------------------------------------------------ 放行开关
    def test_switch_releases_only_unowned_not_visitor(self):
        """开关打开 → 只多出「无主」，visitor **仍然不进**（自助身份不能靠开关翻白）。"""
        self._lesson("被授予者写的教训", RESIDENT)
        self._lesson("无主教训", "unattributed")
        self._lesson("自助访客写的教训", VISITOR)
        self._rule("unknown 作者的规矩", "unknown")
        self._rule("访客写进规矩的探针", VISITOR)
        self._set_cfg("inject.allow_untrusted", 1)
        text = self._lesson_text()
        self.assertIn("无主教训", text, f"开关打开后无主 lesson 仍没进包：{text!r}")
        self.assertNotIn("自助访客写的教训", text,
                         f"开关把 **visitor** 也放进来了——门等于没关：\n{text!r}")
        rules = self._rules()["text"]
        self.assertIn("unknown 作者的规矩", rules, f"开关打开后 unknown 规矩仍没进包：{rules!r}")
        self.assertNotIn("访客写进规矩的探针", rules, f"开关把 visitor 规矩放进来了：{rules!r}")

    def test_legacy_switch_key_still_works(self):
        """旧键 `inject.allow_untrusted_lessons` 是同一件事的历史名——升级后不许静默变关。"""
        self._lesson("无主教训", "unattributed")
        self._set_cfg("inject.allow_untrusted_lessons", 1)
        self.assertIn("无主教训", self._lesson_text(),
                      "旧键失效：已经把它设成 1 的库会在升级后静默变回关闭")

    # ------------------------------------------------------------ 写入侧不变
    def test_write_side_is_not_tightened(self):
        """**写入侧不许被这次改动收紧**（用户决策：记忆比归属重要）。"""
        code = self._lesson("访客照写不误", VISITOR)
        row = self.conn.execute(
            "SELECT owner FROM code_index WHERE code=?", (code,)).fetchone()
        self.assertEqual(row[0], VISITOR, "visitor 作者的 lesson 被拦了——违反『写入侧不动』")
        self.assertNotIn("访客照写不误", self._lesson_text(), "写进去了却照样进包")


if __name__ == "__main__":
    unittest.main(verbosity=2)
