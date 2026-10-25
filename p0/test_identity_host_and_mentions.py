#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A1 判据：身份行的 host 可见性（T4）+ 编码粗筛的字符集（T2）。

为什么单独一个文件
------------------
这两条的**交接清单描述与代码实况不一致**，所以判据要能独立钉住"改之前是什么样、
改之后是什么样"，而不是混进既有文件里靠人回忆：

  T4 清单写"两公民要显示 host 区分" → 实况：`citizens.host` **早就查出来了**
     （`build_injection_pack` 的 SELECT 一直含 host 列），只是没渲染。
     生产库实证：`fish外部评估` 有两条，host 分别是 `ZCode` 与 `cli`——
     不印 host，人和 AI 都分不出哪条是自己。所以这不是"缺数据"，是"缺一行渲染"。

  T2 清单写"放宽 CODE_MENTION_RE" → 实况：旧正则 `[A-Za-z\\u4e00-\\u9fff]{2,20}`
     的后果不是"少匹配一点"，而是 `A1` / `PROJ_2` / `3D` 这类前缀**永远**建不出
     「正文提及」边——recall 查得到这条记忆，正文里写着它的编码却连不上，
     是一个**静默的假阴性**（不报错、只是永远少一条边）。

判据
----
H1  身份行印出 host
H2  host 为空 → 「未记录」（既不印空串，也不印 None）
H3  同名两公民在注入包里靠 host 那一格可区分
H4  变异：抹掉 host 那一格重渲染 → H1 必须红（证明 H1 命中的是渲染，不是别处碰巧出现的字）

M1  含**数字**前缀的编码能被反解（旧正则做不到）
M2  含**下划线**前缀的编码能被反解
M3  纯字母 / 中文前缀仍能反解（回归：放宽不许改坏召回）
M4  变异：换回旧正则 → M1 / M2 必须失配（证明判据命中的是**新字符集**，
    而不是 `tails()` 之类的旁路碰巧把它捞回来）

跑法
----
    python test_identity_host_and_mentions.py -v
    python -m pytest test_identity_host_and_mentions.py -q
"""
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

# 旧口径的正则原件（2026-10-25 之前）。变异判据 M4 要用它把行为打回旧形态。
# 刻意**逐字抄自备份 `历史版本/fish.py.bak-A-20261025-0205:1387`**，不是凭记忆重写。
OLD_CODE_MENTION_RE = re.compile(
    r"(?<![0-9A-Za-z\-])([A-Za-z\u4e00-\u9fff]{2,20}-\d{6}-\d{2,4})(?!\d)")


class IdentityHostTest(unittest.TestCase):
    """T4：身份行必须能分出"哪个宿主来的我"。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = fish.connect(self.root / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        for prefix in ("PROJA",):
            self.conn.execute(
                "INSERT OR IGNORE INTO code_registry (prefix,namespace,last_date,last_seq,"
                "status) VALUES (?,?,NULL,0,'active')", (prefix, "甲项目"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _citizen(self, cid, name, host=None):
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at,"
            "is_owner) VALUES (?,?,?,?,?,?,?,?)",
            (cid, name, "visitor", None, host, "active", fish.now_utc(), 0))
        self.conn.commit()

    def _pack(self, cid):
        text, _meta = fish.build_injection_pack(self.conn, self.root, cid)
        return text

    # ---------------------------------------------------------------- H1
    def test_H1_identity_line_prints_host(self):
        """H1：身份行必须含 `宿主 <host>` 这一格。"""
        self._citizen("01AAAA0000000000000001", "甲", host="zcode")
        text = self._pack("01AAAA0000000000000001")
        self.assertIn("登记身份：甲", text, "身份行本身丢了")
        self.assertIn("宿主 zcode", text,
                      "身份行没印 host——同名两公民就长得一模一样（T4 的原缺口）")

    # ---------------------------------------------------------------- H2
    def test_H2_missing_host_is_explicit(self):
        """H2：host 为空必须写成「未记录」，不许印空串或 None。"""
        self._citizen("01BBBB0000000000000002", "乙", host=None)
        text = self._pack("01BBBB0000000000000002")
        self.assertIn("宿主 未记录", text, "空 host 没有显式占位（会印出 '宿主 ' 或 '宿主 None'）")
        self.assertNotIn("宿主 None", text, "把 None 印给了模型")
        self.assertNotIn("宿主 。", text, "把空串印给了模型（标点前有个空洞）")

    # ---------------------------------------------------------------- H3
    def test_H3_same_name_two_hosts_are_distinguishable(self):
        """H3：同名两公民，注入包里必须能靠 host 分出哪条是自己。"""
        self._citizen("01CCCC0000000000000003", "fish外部评估", host="ZCode")
        self._citizen("01DDDD0000000000000004", "fish外部评估", host="cli")
        a = self._pack("01CCCC0000000000000003")
        b = self._pack("01DDDD0000000000000004")
        self.assertIn("宿主 ZCode", a)
        self.assertIn("宿主 cli", b)
        # 关键：两段身份行必须**不相等**——真实缺口正是"改名不改变身份，但显示要能分人"
        line_a = next(ln for ln in a.splitlines() if ln.startswith("登记身份："))
        line_b = next(ln for ln in b.splitlines() if ln.startswith("登记身份："))
        self.assertNotEqual(line_a, line_b,
                            "两条同名公民的身份行完全相同——无法据此判断'我是哪个'")

    # ---------------------------------------------------------------- H4 变异
    def test_H4_judgement_can_go_red(self):
        """H4 变异自证：把身份行按**旧渲染**拼出来，H1 的断言必须失败。

        为什么不原地改实现：`sqlite3.Connection` 是 C 类型，方法不可 patch
        （`AttributeError: can't set attribute`），所以变异只能落在**渲染产物**本身。
        这里用逐字抄自备份的旧拼法，证明「H1 盯的是那一格 host」这件事有判别力：
        旧渲染 → 断言红；新渲染 → 断言绿。两头都跑过，才叫判据。
        """
        def old_ident(name, cid, kind, role):
            # 逐字抄自 `历史版本/fish.py.bak-A-20261025-0205` 的 ident 拼法
            return (f"登记身份：{name}（公民 {cid}，形态 {kind}，"
                    f"角色 {role or '-'}）。")

        old_line = old_ident("丙", "01EEEE0000000000000005", "visitor", None)
        self.assertNotIn("宿主", old_line,
                         "旧渲染里居然含「宿主」二字——那 H1 就分不出新旧形态")
        self._citizen("01EEEE0000000000000005", "丙", host="zcode")
        new_line = next(ln for ln in self._pack("01EEEE0000000000000005").splitlines()
                        if ln.startswith("登记身份："))
        self.assertNotEqual(old_line, new_line, "新旧身份行完全相同，H1 无法判别实现是否改过")
        self.assertIn("宿主 zcode", new_line)


class CodeMentionCharsetTest(unittest.TestCase):
    """T2：编码粗筛必须认得含数字/下划线的前缀。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = fish.connect(self.root / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _memory(self, code, project):
        """直接落一条 code_index 行：`code_mentions_in` 的真值来源就是它。"""
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant,"
            "session_actor,attachments) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL)",
            (code, "标题", project, "unknown", "fact", "shared", "active",
             "[]", f"projects/{project}/{code}.md",
             fish.now_utc(), fish.now_utc()))
        self.conn.commit()

    # ---------------------------------------------------------------- M1
    def test_M1_digit_prefix_is_resolvable(self):
        """M1：含数字的前缀（A1）必须能被正文反解。"""
        self._memory("A1-261025-01", "A1")
        got = fish.code_mentions_in(self.conn, "见 A1-261025-01 那条")
        self.assertEqual(got, ["A1-261025-01"],
                         "含数字前缀的编码没能从正文反解——这条记忆永远建不出「正文提及」边")

    # ---------------------------------------------------------------- M2
    def test_M2_underscore_prefix_is_resolvable(self):
        """M2：含下划线的前缀（PROJ_2）必须能被正文反解。"""
        self._memory("PROJ_2-261025-01", "PROJ_2")
        got = fish.code_mentions_in(self.conn, "参考 PROJ_2-261025-01")
        self.assertEqual(got, ["PROJ_2-261025-01"], "含下划线前缀的编码没能反解")

    # ---------------------------------------------------------------- M3
    def test_M3_plain_prefix_still_resolves(self):
        """M3 回归：纯字母 / 中文前缀不许因为放宽而失配。"""
        self._memory("PROJA-261025-01", "PROJA")
        self._memory("企业智能-261025-01", "企业智能")
        got = fish.code_mentions_in(
            self.conn, "先看 PROJA-261025-01，再看企业智能-261025-01")
        self.assertEqual(got, ["PROJA-261025-01", "企业智能-261025-01"],
                         "放宽字符集后，原有的字母/中文前缀反解退化（改坏召回）")

    # ---------------------------------------------------------------- M4 变异
    def test_M4_mutation_old_regex_loses_digit_prefix(self):
        """M4 变异自证：换回旧正则，M1/M2 必须失配。

        若旧正则下这两条仍能反解，说明 M1/M2 命中的是别的路径（例如 tails() 的
        前缀剥法），而不是"字符集放宽"这个改动本身——那判据就没有判别力。
        """
        self._memory("A1-261025-01", "A1")
        self._memory("PROJ_2-261025-01", "PROJ_2")
        with mock.patch.object(fish, "CODE_MENTION_RE", OLD_CODE_MENTION_RE):
            got = fish.code_mentions_in(self.conn, "见 A1-261025-01 与 PROJ_2-261025-01")
        self.assertEqual(got, [],
                         "旧正则居然也能反解含数字/下划线的前缀——那 T2 的改动是无效的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
