#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入包的状态过滤判据（2026-10-24 补）。

背景
----
`build_injection_pack` 的 L1/L2 四处 SQL 原本都没筛 `status`，导致：
  ①「活跃 N 条」把 superseded/withdrawn 也算进去（实测 183 vs 真实 149）；
  ②「最近三件事」可能推出已替代/已作废的条目（实测 TESTP/VIDEO 各中 1 条）；
  ③「今日编码」可能列出已作废的编码（实测 ANYI-261024-04 被列出）；
  ④「休眠 N 条」把已作废且休眠的也算进去。

注入包是**每个会话开场**都用的路径，错误信息的杠杆最大，所以这四处必须有判据钉住。

判据与变异
----------
A  活跃数只算 active        | 变异：去掉 status 过滤 → 数字变大 → 红
B  最近三件事不含非 active   | 变异：去掉 status 过滤 → 作废条目冒进 → 红
C  今日编码不含非 active     | 变异：去掉 status 过滤 → 作废编码冒进 → 红
D  休眠数只算 active         | 变异：去掉 status 过滤 → 数字变大 → 红
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class InjectionStatusFilterTest(unittest.TestCase):
    """注入包必须只展示 active 记忆。"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.conn = fish.connect(Path(self.root) / "data")
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        # 建一个公民当 actor
        self.cid = "01CITIZEN0000000000000009"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "过滤测试居民", "resident", "owner", None, "active", fish.now_utc()))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _mem(self, code, title, project="TESTP", mem_type="fact", status="active"):
        """直接插投影（不经过 record_memory，避免正文文件与事件开销）。"""
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared',?,'[]',NULL,?,?,0,0)",
            (code, title, project, self.cid, mem_type, status,
             fish.now_utc(), fish.now_utc()))
        self.conn.commit()

    def _pack(self):
        return fish.build_injection_pack(self.conn, Path(self.root) / "data", self.cid)

    # ---------------------------------------------------------------- A
    def test_A_active_count_excludes_non_active(self):
        """「活跃 N 条」只算 status='active'。"""
        self._mem("TESTP-261024-01", "真活跃一", status="active")
        self._mem("TESTP-261024-02", "真活跃二", status="active")
        self._mem("TESTP-261024-03", "已替代", status="superseded")
        self._mem("TESTP-261024-04", "已作废", status="withdrawn")

        pack, _ = self._pack()
        self.assertIn("活跃 2 条", pack,
                      f"应只算 2 条 active；实际包里没有「活跃 2 条」。包片段：\n{pack[:400]}")
        self.assertNotIn("活跃 4 条", pack, "把非 active 算进活跃数是本次修复的 bug")

    # ---------------------------------------------------------------- B
    def test_B_recent_excludes_non_active(self):
        """「最近三件事」不能推已替代/已作废的条目（即使它编码更新）。"""
        self._mem("TESTP-261024-01", "正常的旧条目", status="active")
        self._mem("TESTP-261024-02", "正常的次新条目", status="active")
        # 编码更大、但已作废 —— 旧实现会把它当"最近"
        self._mem("TESTP-261024-99", "已作废的最新条目", status="withdrawn")
        self._mem("TESTP-261024-98", "已替代的次新条目", status="superseded")

        pack, _ = self._pack()
        self.assertNotIn("已作废的最新条目", pack, "作废条目不该出现在「最近三件事」")
        self.assertNotIn("已替代的次新条目", pack, "已替代条目不该出现在「最近三件事」")
        self.assertIn("正常的次新条目", pack, "active 条目应正常展示")

    # ---------------------------------------------------------------- C
    def test_C_index_lists_only_projects_with_active_memories(self):
        """L2「可查项目」只列有 active 记忆的项目；已作废编码不许出现在任何段。

        2026-10-25 · P1-a：**原判据测「今日编码」**（L2 曾列当日全部编码）。该段已按
        研判删除（编码是坐标不是信息，见 `31` §2），判据对象随之迁移到新 L2 的主体
        「可查项目」——**意图不变：状态过滤不能漏**。

        为什么要一起测「0 条项目不许列」：那是同一类错误的反面——
        原实现把 14 个项目全列了，其中 3 个一条记忆都没有，等于给了个找不到东西的地址。

        变异：JOIN 不筛 c.status='active' → 作废编码冒进 → 红；
              改回 LEFT JOIN 且不过滤 n>0 → EMPTYNS 冒进 → 红。
        """
        d = fish.local_date()
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('TESTP','测试项目',NULL,0,'active')")
        self._mem(f"TESTP-{d}-01", "今天的活跃", status="active")
        self._mem(f"TESTP-{d}-99", "今天的作废", status="withdrawn")
        # 一个"注册了但一条记忆都没有"的项目：不该出现在「可查项目」里
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('EMPTYNS','空壳用于判据',NULL,0,'active')")
        # 一个已退役的项目：即使有记忆也不该出现
        self.conn.execute(
            "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
            "VALUES ('RETIREDNS','退役用于判据',NULL,0,'retired')")
        self.conn.commit()

        pack, _meta = self._pack()
        self.assertNotIn(f"TESTP-{d}-99", pack, "已作废的编码不该出现在任何段")
        self.assertIn("可查项目：", pack,
                      f"L2 应渲染「可查项目」行。包片段：\n{pack[-500:]}")
        self.assertIn("TESTP(1)", pack, "有 1 条 active 记忆的项目应被列出且计数为 1")
        self.assertNotIn("EMPTYNS", pack,
                         "0 条记忆的项目不该列出（等于给了个找不到东西的地址）")
        self.assertNotIn("RETIREDNS", pack, "已退役项目不该列出")

    # ---------------------------------------------------------------- D
    def test_D_dormant_count_excludes_non_active(self):
        """「休眠 N 条」只算 active 且 dormant 的。"""
        self._mem("TESTP-261024-01", "活跃条目", status="active")
        # 一条 active 休眠 + 一条 withdrawn 休眠 → 计数应为 1
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES ('TESTP-261024-02','活跃休眠','TESTP',?,'fact','shared','active',"
            "'[]',NULL,?,?,0,1)", (self.cid, fish.now_utc(), fish.now_utc()))
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES ('TESTP-261024-03','作废休眠','TESTP',?,'fact','shared','withdrawn',"
            "'[]',NULL,?,?,0,1)", (self.cid, fish.now_utc(), fish.now_utc()))
        self.conn.commit()

        pack, _ = self._pack()
        self.assertIn("另有 1 条已休眠", pack,
                      f"应只算 1 条 active 的休眠；实际包片段：\n{pack[:400]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
