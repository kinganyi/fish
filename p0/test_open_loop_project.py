#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`_open_loop_rows(..., project=None)` 判据（B2 的"仅关键字"改动 · 裁决 Q15）。

被改的函数是注入包 A 类信号（未闭合事项）的取数口，B2 状态表的「未了事项」列复用它。
这次改动**只加一个仅关键字参数**，本文件要同时钉住三件事：

  A  默认（不传 `project`）= **旧行为**：跨项目、词表/窗口/排除/上限一字不变；
  B  传 `project=` 时只取该项目（B2 列的语义）；
  C  只用**关键字**传：位置传参必须 `TypeError`（K10 的机械证明）。

## 变异自证（X-1）

变异 —— 对应 `test_project_filter_is_exclusive`：
  把 `sql += "AND project = ? "` 删掉 → 传 `project='P2'` 仍返回 P1 的记忆 →
  "只返回 P2" 的断言红。（本项目历史上真正的踩坑形态是"忘了加条件但参数照收"，
  那种 bug 不会报错，只会让两个项目的未了事项搅在一起。）

纪律（K12）：`tempfile.mkdtemp()` 隔离；不指向 `p0/data`。
"""
import datetime
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
AS_OF = datetime.datetime(2026, 10, 26, 0, 0, 0, tzinfo=TZ8)


def _z(days_ago=0):
    dt = AS_OF - datetime.timedelta(days=days_ago)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpenLoopProjectTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-openloop-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mem(self, code, project, *, title, recorded_at=None, status="active",
             dormant=0):
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,status,"
            "file_path,recorded_at,dormant) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, title, project, "01OWNER000000000000000001", "fact", status,
             f"projects/{project}/{code}__x.md", recorded_at or _z(1), dormant))
        self.conn.commit()

    def _since(self, days=30):
        return _z(days)

    # ---------------------------------------------------------------- A 旧行为
    def test_default_stays_cross_project(self):
        """默认 None：跨项目（注入包的老语义），一个字节都不变。"""
        self._mem("P1-261026-01", "P1", title="未闭合：甲")
        self._mem("P2-261026-01", "P2", title="下一步：乙")
        codes = [c for c, _t in fish._open_loop_rows(self.conn, self._since(), 10)]
        self.assertEqual(sorted(codes), ["P1-261026-01", "P2-261026-01"])

    def test_default_keeps_window_and_keyword_filter(self):
        """默认口径不变：窗口内 + 命中 8 词词表 + active + 未休眠。"""
        self._mem("P1-261026-01", "P1", title="待办：窗口内", recorded_at=_z(5))
        self._mem("P1-261026-02", "P1", title="待办：窗口外", recorded_at=_z(31))
        self._mem("P1-261026-03", "P1", title="普通记忆", recorded_at=_z(1))
        self._mem("P1-261026-04", "P1", title="待办：已归档", recorded_at=_z(1),
                  status="superseded")
        self._mem("P1-261026-05", "P1", title="待办：已休眠", recorded_at=_z(1),
                  dormant=1)
        codes = [c for c, _t in fish._open_loop_rows(self.conn, self._since(), 10)]
        self.assertEqual(codes, ["P1-261026-01"])

    def test_default_respects_limit_and_exclude(self):
        for i in range(1, 6):
            self._mem(f"P1-261026-0{i}", "P1", title=f"待办：{i}",
                      recorded_at=_z(i))
        got = fish._open_loop_rows(self.conn, self._since(), 2)
        self.assertEqual(len(got), 2, "limit 没生效")
        first = got[0][0]
        kept = fish._open_loop_rows(self.conn, self._since(), 2, exclude=(first,))
        self.assertNotIn(first, [c for c, _t in kept], "exclude 没生效")
        self.assertEqual(len(kept), 2, "exclude 掉一条后仍要凑满 limit（不是少一条）")

    # ---------------------------------------------------------------- B 新参数
    def test_project_filter_is_exclusive(self):
        """**变异证明点**：传 project 只返回该项目的记忆，另一个项目一条不混。"""
        self._mem("P1-261026-01", "P1", title="未闭合：甲")
        self._mem("P2-261026-01", "P2", title="下一步：乙")
        got = fish._open_loop_rows(self.conn, self._since(), 10, project="P2")
        self.assertEqual([c for c, _t in got], ["P2-261026-01"],
                         "project= 过滤没生效（变异：删掉 AND project = ? 后会红）")

    def test_project_filter_no_rows_is_empty(self):
        """项目存在但窗口内没有未闭合事项 → 空列表（状态表据此显示裸 `—`）。"""
        self._mem("P2-261026-01", "P2", title="下一步：乙")
        self.assertEqual(fish._open_loop_rows(self.conn, self._since(), 10,
                                              project="P3"), [])

    def test_project_filter_respects_limit(self):
        for i in range(1, 4):
            self._mem(f"P2-261026-0{i}", "P2", title=f"待办：{i}", recorded_at=_z(i))
        got = fish._open_loop_rows(self.conn, self._since(), 1, project="P2")
        self.assertEqual(len(got), 1)

    # ---------------------------------------------------------------- C 仅关键字
    def test_project_must_be_keyword_only(self):
        """K10 的机械证明：第 5 个位置参数必须不被接受。"""
        with self.assertRaises(TypeError):
            fish._open_loop_rows(self.conn, self._since(), 10, (), "P1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
