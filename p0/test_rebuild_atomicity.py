#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查 F-P0-2 判据：`fish rebuild` 必须**先校验再动投影**。

## 审查方实测的问题（已复核成立）

旧实现：

    conn.execute("DELETE FROM code_index")
    conn.commit()                    # ← 投影已清空
    for ... in events:               # ← 才开始逐条重放
        p = json.loads(payload)      # ← 畸形事件在这里抛

一条畸形事件 → **投影从 1 变 0**，而快照只在内存里。恢复手段本身变成了破坏手段。

## 这批判据为什么能失败

关键是**真的塞一条畸形事件进去**，然后断言投影**逐行不变**。
如果实现退回"先删再重放"，第二条断言立刻红。

对照组（无畸形事件）必须绿——否则"全红"可能只是判据读空。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fish          # noqa: E402
import fish_rebuild  # noqa: E402


class RebuildAtomicityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        self.conn.execute(
            "INSERT INTO code_registry (prefix, namespace, last_date, last_seq)"
            " VALUES ('P','P','2026-11-18',0)")
        self.conn.commit()
        # 写两条正常记忆当基线
        for t in ("甲", "乙"):
            fish.record_memory(self.conn, self.data, project="P", title=t,
                               body=f"# {t}\n", mem_type="fact")
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _snapshot(self):
        return sorted(self.conn.execute(
            "SELECT code,title,owner,status FROM code_index").fetchall())

    def _inject_bad_event(self, payload="这不是 JSON"):
        self.conn.execute(
            "INSERT INTO events (event_id,ts,type,actor,subject,payload,cause,schema)"
            " VALUES ('EV-BAD','2026-11-18T00:00:00Z','memory.recorded',"
            "'{\"citizen_id\":\"x\"}','{}',?,'%s','evt@1')" % "test", (payload,))
        self.conn.commit()

    # ------------------------------------------------------------- 主判据
    def test_malformed_event_leaves_projection_untouched(self):
        """**F-P0-2 的直接探针**：畸形事件必须让 rebuild 中止，且投影一行不变。"""
        before = self._snapshot()
        self.assertEqual(len(before), 2, "基线不对")
        self._inject_bad_event()
        with self.assertRaises(fish_rebuild.RebuildError) as ctx:
            fish_rebuild.rebuild_projection(self.conn, self.data)
        self.assertIn("未被修改", str(ctx.exception))
        self.assertTrue(ctx.exception.detail.get("bad_events"),
                        "异常里没有带坏事件的诊断信息")
        after = self._snapshot()
        self.assertEqual(after, before,
                         f"重建中止后投影被改了：\n前 {before}\n后 {after}")

    def test_payload_not_object_also_aborts(self):
        """payload 是合法 JSON 但不是对象（例如数组）——同样必须中止。"""
        before = self._snapshot()
        self._inject_bad_event(payload="[1,2,3]")
        with self.assertRaises(fish_rebuild.RebuildError) as ctx:
            fish_rebuild.rebuild_projection(self.conn, self.data)
        # F-P1-X（评审 🟠-1）：bad_events 元素改成了 3 元组 (seq, 事件类型, 原因)——
        # 预检现在覆盖五类事件，报错必须能说清是**哪一类**坏了。
        why = [b[2] for b in ctx.exception.detail.get("bad_events", [])]
        self.assertTrue(any("不是对象" in w for w in why),
                        f"诊断里没说明原因：{why}")
        self.assertEqual(self._snapshot(), before)

    def test_missing_code_also_aborts(self):
        """payload 是对象但缺 `code` ——重放会 KeyError，必须提前拦住。"""
        before = self._snapshot()
        self._inject_bad_event(payload=json.dumps({"title": "没编码"}))
        with self.assertRaises(fish_rebuild.RebuildError) as ctx:
            fish_rebuild.rebuild_projection(self.conn, self.data)
        # F-P1-X（评审 🟠-1）：bad_events 元素改成了 3 元组 (seq, 事件类型, 原因)——
        # 预检现在覆盖五类事件，报错必须能说清是**哪一类**坏了。
        why = [b[2] for b in ctx.exception.detail.get("bad_events", [])]
        self.assertTrue(any("缺 code" in w for w in why),
                        f"诊断里没说明原因：{why}")
        self.assertEqual(self._snapshot(), before)

    # ------------------------------------------------------------- 对照组
    def test_clean_rebuild_still_works(self):
        """**未变异的对照组必须绿**：没有坏事件时重建照常成功。"""
        before = self._snapshot()
        rep = fish_rebuild.rebuild_projection(self.conn, self.data)
        after = self._snapshot()
        self.assertEqual(after, before,
                         f"干净重建改变了投影：\n前 {before}\n后 {after}")
        self.assertTrue(rep.get("precheck_ok"), "预检标记没写进报告")


    # ----------------------------------------- 🔴 评审 🟠-1：预检必须覆盖五类
    def test_malformed_other_event_types_also_abort(self):
        """**核心补充**：畸形事件不止 memory.recorded 一种。

        预检只扫 memory.recorded 时，下面这三类坏事件会在 `DELETE FROM code_index`
        **之后**才炸——`JSONDecodeError` 发生在投影已被清空的那一刻。
        函数级"投影未被修改"在那条路径上是假的。

        用 setUp 的库：每一轮都只往**同一个**库里加一条坏事件，
        投影必须始终不变（坏事件会累积，但预检会一次性全报出来）。
        """
        before = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        for ev_type in ("memory.superseded", "memory.retired", "memory.accessed"):
            with self.subTest(event_type=ev_type):
                # 事件表是 append-only（UPDATE 会被触发器拒绝），所以坏事件只能
                # **直接 INSERT** 进去模拟"历史里本来就是坏的"。
                # 列：event_id/ts/type/actor/subject/payload/cause/refs/schema，
                # schema 是 NOT NULL，append_event 写的值是 "evt@1"。
                self.conn.execute(
                    "INSERT INTO events (event_id,ts,type,actor,subject,payload,"
                    "cause,refs,schema) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"bad-{ev_type}", fish.now_utc(), ev_type, "{}", "mem:P-1",
                     "{不是合法 JSON", None, None, "evt@1"))
                self.conn.commit()
                with self.assertRaises(fish_rebuild.RebuildError) as ctx:
                    fish_rebuild.rebuild_projection(self.conn, self.data)
                detail = ctx.exception.detail or {}
                types = {b[1] for b in detail.get("bad_events", [])}
                self.assertIn(ev_type, types,
                              f"{ev_type} 的坏事件没被预检抓到：{detail}")
                after = self.conn.execute(
                    "SELECT COUNT(*) FROM code_index").fetchone()[0]
                self.assertEqual(after, before,
                                 f"{ev_type} 畸形时投影被改了：{before}→{after}")

    def test_precheck_types_cover_every_replayed_type(self):
        """**判据要和实现一样长**：预检清单必须覆盖重放解析的每一类事件。

        逐个扫源码里 `json.loads` 前面出现的事件类型，断言都在 PRECHECK_TYPES 里。
        谁哪天新增一类重放却忘了加进预检，这条就红。
        """
        import re as _re
        src = (HERE / "fish_rebuild.py").read_text(encoding="utf-8")
        # 重放阶段按类型查询的地方
        used = set(_re.findall(r"type=\"(memory\.[a-z_]+)\"", src))
        used |= set(_re.findall(r"type IN \(([^)]*)\)", src) and
                    [t for grp in _re.findall(r"type IN \(([^)]*)\)", src)
                     for t in _re.findall(r"'(memory\.[a-z_]+)'", grp)])
        # owner_fixed 自带 try/except，不强制进预检
        used.discard("memory.owner_fixed")
        declared = set(fish_rebuild._PRECHECK_TYPES)
        missing = sorted(used - declared)
        self.assertEqual(missing, [],
                         f"这些事件类型被重放解析但不在预检清单里：{missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
