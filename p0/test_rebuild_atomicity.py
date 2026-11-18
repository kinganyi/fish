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
        why = [w for _seq, w in ctx.exception.detail.get("bad_events", [])]
        self.assertTrue(any("不是对象" in w for w in why),
                        f"诊断里没说明原因：{why}")
        self.assertEqual(self._snapshot(), before)

    def test_missing_code_also_aborts(self):
        """payload 是对象但缺 `code` ——重放会 KeyError，必须提前拦住。"""
        before = self._snapshot()
        self._inject_bad_event(payload=json.dumps({"title": "没编码"}))
        with self.assertRaises(fish_rebuild.RebuildError) as ctx:
            fish_rebuild.rebuild_projection(self.conn, self.data)
        why = [w for _seq, w in ctx.exception.detail.get("bad_events", [])]
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
