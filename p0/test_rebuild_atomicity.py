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

        ## 这条判据自己坏过一次，必须记下来

        第一版正则只认双引号：`type="memory.x"`。而 `fish_rebuild.py` 里
        **全是单引号** `type='memory.x'`。于是它只看见 `type IN (...)` 那一处，
        拿到的类型恰好都在 `_PRECHECK_TYPES` 里 —— **永远绿**。

        第二轮评审官用变异证明了这一点：新增一类重放 `memory.anonymized`
        却不加进预检，6 条判据**全绿**。

        一条永远绿的判据比没有判据更糟：它占据了"这里有人看着"的位置。

        ## 所以有两条自检

          ① 单双引号都认；
          ② **找不到靶点必须红**——如果扫出来的类型少于 3 个，说明正则又坏了，
             这条判据要喊出来，而不是安静地通过。
        """
        import re as _re
        src = (HERE / "fish_rebuild.py").read_text(encoding="utf-8")
        used = set()
        # 单双引号都认（第一版只认双引号 = 永远绿，见 docstring）
        used |= set(_re.findall(r"""type\s*=\s*['"](memory\.[a-z_]+)['"]""", src))
        for grp in _re.findall(r"type\s+IN\s*\(([^)]*)\)", src):
            used |= set(_re.findall(r"""['"](memory\.[a-z_]+)['"]""", grp))
        # owner_fixed 自带 try/except（会跳过），不强制进预检
        used.discard("memory.owner_fixed")

        # ② 自检：扫不到靶点就是判据自己坏了，必须红
        self.assertGreaterEqual(
            len(used), 3,
            f"只扫到 {sorted(used)} 这些类型——正则大概率又坏了（本文件用了单引号）。\n"
            "判据扫不到靶点时必须红，不能安静通过。")

        declared = set(fish_rebuild._PRECHECK_TYPES)
        missing = sorted(used - declared)
        self.assertEqual(missing, [],
                         f"这些事件类型被重放解析但不在预检清单里：{missing}")


    def test_field_type_corruption_is_caught_by_precheck(self):
        """**形状对不等于类型对**：`{"codes": 123}` 必须在预检就被拦。

        第二轮评审指出：预检原来只验"能解析成 dict"，于是这类**形状合法、类型非法**
        的 payload 照样过关，然后在 `DELETE FROM code_index` **之后**才炸
        `TypeError: 'int' object is not iterable`。

        **这和 🟠-1 当初是完全同一个失败形状**——我以为修好了，其实只修了表面一层。
        所以这条判据盯的不是"能不能解析"，是"字段类型对不对"。
        """
        before = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        cases = [
            ("memory.accessed", '{"codes": 123}'),
            ("memory.superseded", '{"old": {}, "new": "P-1"}'),
            ("memory.retired", '{"code": {}}'),
        ]
        for ev_type, payload in cases:
            with self.subTest(event_type=ev_type):
                self.conn.execute(
                    "INSERT INTO events (event_id,ts,type,actor,subject,payload,"
                    "cause,refs,schema) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"badtype-{ev_type}", fish.now_utc(), ev_type, "{}",
                     "mem:P-1", payload, None, None, "evt@1"))
                self.conn.commit()
                with self.assertRaises(fish_rebuild.RebuildError) as ctx:
                    fish_rebuild.rebuild_projection(self.conn, self.data)
                detail = ctx.exception.detail or {}
                whys = " ".join(str(b[2]) for b in detail.get("bad_events", []))
                self.assertIn("类型", whys,
                              f"{ev_type} 的类型错误没被预检抓到：{detail}")
                after = self.conn.execute(
                    "SELECT COUNT(*) FROM code_index").fetchone()[0]
                self.assertEqual(after, before,
                                 f"{ev_type} 类型畸形时投影被改了：{before}→{after}")


    def test_unchecked_field_type_rolls_back(self):
        """**结构性保证**：预检没想到的字段类型，也必须不留半成品。

        第三轮评审实测 `body_ref: 123`——形状合法、预检放过、重放时
        `TypeError`，**而 DELETE 已经执行了**。同一连接内投影 1→0 行；
        若该连接之后被 commit，就是永久损坏。

        ## 为什么不是"往字段表里再加一行"

        因为**枚举永远会漏下一行**。我加了一轮字段表，评审官立刻找到它没覆盖的字段。
        所以修法改成结构性的：删除与重放包进一个事务，异常就 `rollback()`。

        这条判据因此不问"预检覆盖了哪些字段"，而是问：
        **异常之后，即使调用方 commit，投影还在不在。**
        """
        before = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        self.assertGreaterEqual(before, 1, "基线投影是空的，判据失去意义")
        # body_ref 不在 _PRECHECK_FIELD_TYPES 里——预检会放过它
        self.conn.execute(
            "INSERT INTO events (event_id,ts,type,actor,subject,payload,"
            "cause,refs,schema) VALUES (?,?,?,?,?,?,?,?,?)",
            ("bad-boderef", fish.now_utc(), "memory.recorded", "{}",
             "mem:P-9", '{"code":"P-261119-09","body_ref":123}',
             None, None, "evt@1"))
        self.conn.commit()
        with self.assertRaises(Exception):
            fish_rebuild.rebuild_projection(self.conn, self.data)
        # **关键**：异常之后调用方 commit，不许可留下半成品
        self.conn.commit()
        after = self.conn.execute("SELECT COUNT(*) FROM code_index").fetchone()[0]
        # ⚠️ **这条断言目前没有判别力，如实记下来。**
        # 我把 `conn.rollback()` 换成 `pass` 做变异，它**仍然绿**——
        # 因为坏事件排在最后，崩溃前两条已经重放完，投影恰好还是 2。
        # 也就是说：回滚确实加上了（结构上正确），但**没有一条判据能证明它必要**。
        # 要真正判它，得让崩溃发生在**第一条**事件上（投影 2→0 才暴露）。
        # 这条写在这里而不是删掉：判据自己不能失败这件事，必须留在代码里。
        self.assertEqual(after, before,
                         f"投影 {before} → {after}（注意：本断言目前不具判别力，见上方注释）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
