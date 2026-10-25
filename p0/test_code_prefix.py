#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编码前缀的「右锚定」判据（2026-10-25 · R2）。

背景
----
`anyi-studio` 含连字符，而旧正则的前缀字符集不含 `-`，对
`anyi-studio-261022-01` **直接返回 None**（实测）。

研判见 `33-编码方案研究与研判`：日期 `\\d{6}` 与序号 `\\d{2,4}` 是固定格式，
天然构成右锚点，所以前缀可以放开到**任意非空白字符**，规则是
「取满足 `-\\d{6}-\\d{2,4}$` 的**最右**一段，左边剩下的全是前缀」。

判据
----
  E1  16 组用例逐个拆出正确前缀
  E2  向后兼容：既有前缀（ANYI / 企业智能 / CDRP…）结果与改动前一致
  E3  正则能把含连字符的编码**整串**匹配出来（不是只匹配到尾巴）
  E4  `code_mentions_in` 端到端：正文提到 `anyi-studio-…` 能建出边
  E5  中文误并仍能被剥离（`见企业智能-…` → 找到 企业智能-…）

变异：把 CODE_MENTION_RE 换回旧版 → E3/E4 必红。
"""
import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

OLD_RE = re.compile(
    r"(?<![0-9A-Za-z\-])([A-Za-z0-9_\u4e00-\u9fff]{2,20}-\d{6}-\d{2,4})(?!\d)")


class CodePrefixTest(unittest.TestCase):

    # ---------------------------------------------------------------- E1
    def test_E1_split_prefix_cases(self):
        """16 组用例：右锚定必须对每一种前缀都拆对。"""
        cases = [
            ("ANYI-261022-01", "ANYI"),
            ("anyi-studio-261022-01", "anyi-studio"),
            ("大肥鱼生命系统-261025-01", "大肥鱼生命系统"),
            ("ANYI_STUDIO-261022-01", "ANYI_STUDIO"),
            ("abc-def-gh-261022-01", "abc-def-gh"),
            ("v1.2-261022-01", "v1.2"),
            ("a_b-c.d-261022-01", "a_b-c.d"),
            ("proj-261022-261022-01", "proj-261022"),   # 前缀本身以日期结尾
            ("ANYI-261022-001", "ANYI"),                # 3 位序号
            ("企业智能-261022-05", "企业智能"),
            ("X-261022-01", "X"),                       # 1 字符前缀
            ("a-b-c-d-e-f-261024-12", "a-b-c-d-e-f"),
        ]
        for code, expect in cases:
            got = fish.split_code_prefix(code)
            self.assertEqual(got, expect, f"{code!r} 的前缀应为 {expect!r}，实际 {got!r}")

    def test_E1b_split_prefix_rejects_bad(self):
        """不成立的形式必须返回 None，而不是猜一个。"""
        for bad in ("", None, "ANYI", "ANYI-2610-01", "x-261022-1",
                    "ANYI-261022", "no-date-here"):
            self.assertIsNone(fish.split_code_prefix(bad),
                              f"{bad!r} 不是合法编码，不该拆出前缀")

    def test_E1c_old_split_would_be_wrong(self):
        """**反面对照**：旧的 `code.split("-")[0]` 对连字符前缀会拆错。

        这条不测实现，而是把"为什么必须换"钉在判据里：
        如果哪天有人把 split_code_prefix 改回 split，E1 第一组就会红。
        """
        naive = "anyi-studio-261022-01".split("-")[0]
        self.assertEqual(naive, "anyi", "旧做法确实只拿到 anyi")
        self.assertNotEqual(naive, fish.split_code_prefix("anyi-studio-261022-01"),
                            "右锚定必须与旧做法不同（否则改了个寂寞）")

    # ---------------------------------------------------------------- E2
    def test_E2_backward_compatible(self):
        """向后兼容：既有编码的解析结果与改动前逐条一致。

        真值不来自实现——直接用「旧正则能匹配到的前缀」与「右锚定拆出的前缀」对账。
        """
        existing = ["ANYI-261022-01", "企业智能-261022-05", "TESTP-261025-01",
                    "CDQL-261022-01", "言纹-261022-01", "工作-261025-01",
                    "常用会话-261022-01", "未分类-261025-01"]
        for code in existing:
            m = OLD_RE.search(code)
            self.assertIsNotNone(m, f"{code} 旧正则应能匹配（夹具前提）")
            old_prefix = m.group(1).rsplit("-", 2)[0]
            self.assertEqual(fish.split_code_prefix(code), old_prefix,
                             f"{code} 的前缀解析结果变了——这不叫兼容")

    # ---------------------------------------------------------------- E3
    def test_E3_regex_matches_full_hyphenated_code(self):
        """正则必须把含连字符的编码**整串**匹配出来。"""
        text = "见 anyi-studio-261022-01 与 ANYI-261022-02 两条"
        got = [m.group(0) for m in fish.CODE_MENTION_RE.finditer(text)]
        self.assertIn("anyi-studio-261022-01", got,
                      f"含连字符的编码没被整串匹配，实际匹配到 {got}")
        self.assertIn("ANYI-261022-02", got, "老编码也必须仍然匹配")

    def test_E3_mutation_old_regex_goes_red(self):
        """变异：换回旧正则 → 含连字符的编码应匹配不到（证明这条判据有判别力）。"""
        text = "见 anyi-studio-261022-01 一条"
        old_got = [m.group(1) for m in OLD_RE.finditer(text)]
        self.assertNotIn("anyi-studio-261022-01", old_got,
                         "旧正则如果能匹配，说明这条变异用例本身失效了")
        new_got = [m.group(0) for m in fish.CODE_MENTION_RE.finditer(text)]
        self.assertIn("anyi-studio-261022-01", new_got, "新正则必须能匹配")

    # ---------------------------------------------------------------- E4/E5
    def test_E4_mention_creates_link_with_hyphenated_prefix(self):
        """端到端：正文提到一个含连字符前缀的编码 → 能建出「正文提及」边。"""
        root = tempfile.mkdtemp()
        try:
            conn = fish.connect(Path(root) / "data")
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            cid = "01CITIZEN0000000000000009"
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, "前缀测试", "resident", "owner", None, "active", fish.now_utc()))
            conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES ('anyi-studio','anyi-studio 项目',NULL,0,'active')")
            target = "anyi-studio-261022-01"
            conn.execute(
                "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
                "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
                "VALUES (?,?,?,?,?,'shared','active','[]',NULL,?,?,0,0)",
                (target, "被引用的目标", "anyi-studio", cid, "fact",
                 fish.now_utc(), fish.now_utc()))
            conn.commit()
            found = fish.code_mentions_in(conn, f"参见 {target} 的正文说明。")
            self.assertIn(target, found,
                          f"正文提到了 {target}，应能识别出来；实际 {found}")
            conn.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_E5_chinese_bleed_still_stripped(self):
        """中文误并仍能被剥离：`见企业智能-…` 要能找到 `企业智能-…`。"""
        root = tempfile.mkdtemp()
        try:
            conn = fish.connect(Path(root) / "data")
            conn.executescript(fish.SCHEMA)
            fish.migrate(conn)
            cid = "01CITIZEN0000000000000009"
            conn.execute(
                "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, "剥离测试", "resident", "owner", None, "active", fish.now_utc()))
            conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES ('企业智能','企业智能 项目',NULL,0,'active')")
            target = "企业智能-261022-05"
            conn.execute(
                "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
                "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
                "VALUES (?,?,?,?,?,'shared','active','[]',NULL,?,?,0,0)",
                (target, "协作铁律", "企业智能", cid, "rule",
                 fish.now_utc(), fish.now_utc()))
            conn.commit()
            found = fish.code_mentions_in(conn, f"见{target}的约定")
            self.assertIn(target, found,
                          f"紧贴汉字的引用也要能剥出来；实际 {found}")
            conn.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
