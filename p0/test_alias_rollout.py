#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""别名在三条路径上必须一致生效（2026-10-25）。

背景
----
别名机制 2026-10-25 上线时只接了 `search_memories` 的 project 过滤。
于是同一个项目名在三条路径上表现不一致：

| 路径 | 修前 | 后果 |
|---|---|---|
| `search_memories` | ✅ 展开 | 主检索正常 |
| `recent_fallback`（**未命中的回退列表**） | ❌ 等值 | 回退列表只给一半——而回退列表往往是用户唯一看到的东西 |
| `inject` 的「当前项目」 | ❌ 原样返回 | `--project ANYI` 显示「活跃 0 条」，而记忆全在 `anyi-studio` |

第三条尤其危险：`08` §2.1 的原话是「注入包的第一屏经常是错的，而它不会说'我不确定'」。

判据
----
  AL1  `recent_fallback` 跨别名（两个方向都拿到同一批）
  AL2  `inject` 的「当前项目」走别名归一（不显示旧名）
  AL3  「活跃 N 条」按别名**合并**统计（不是只看规范名）
  AL4  变异：去掉展开 → AL1 必须红（证明判据有判别力）
  AL5  没有别名的项目**行为不变**（不因新代码而多算）
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("fish", "fish.py")
fish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fish)


class AliasRolloutTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.data = self.root / "data"
        self.conn = fish.connect(self.data)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.cid = "01CITIZEN0000000000000009"
        self.conn.execute(
            "INSERT INTO citizens (citizen_id,display_name,kind,role,host,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (self.cid, "别名测试", "resident", "owner", None, "active", fish.now_utc()))
        for p in ("cdql", "CDQL", "SOLO"):
            self.conn.execute(
                "INSERT INTO code_registry (prefix,namespace,last_date,last_seq,status) "
                "VALUES (?,?,NULL,0,'active')", (p, p))
        self.conn.commit()
        fish.link_alias(self.conn, "project", "cdql", "CDQL", reason="测试：同一项目两个入口")
        # 两个入口各有记忆
        self._mem("cdql", 1)
        self._mem("cdql", 2)
        self._mem("CDQL", 1)
        self._mem("SOLO", 1)      # 没有别名的项目，用来对照

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _mem(self, project, n):
        code = f"{project}-261025-{n:02d}"
        self.conn.execute(
            "INSERT INTO code_index (code,title,project,owner,mem_type,visibility,status,"
            "tags,file_path,occurred_at,recorded_at,access_count,dormant) "
            "VALUES (?,?,?,?,?,'shared','active','[]',?,?,?,0,0)",
            (code, f"{project} 的第 {n} 条", project, self.cid, "fact", None,
             fish.now_utc(), fish.now_utc()))
        self.conn.commit()

    # ------------------------------------------------------------------ AL1
    def test_AL1_fallback_expands_alias(self):
        """未命中的回退列表必须跨别名——两个方向都拿到同一批。"""
        r1 = {c for c, _ in fish.recent_fallback(self.conn, project="cdql", limit=10)}
        r2 = {c for c, _ in fish.recent_fallback(self.conn, project="CDQL", limit=10)}
        self.assertTrue(r1, "回退列表不该为空（夹具里有两个入口的记忆）")
        self.assertEqual(r1, r2, f"两个方向的回退列表应一致：cdql={sorted(r1)} CDQL={sorted(r2)}")
        self.assertIn("cdql-261025-01", r1, "cdql 自己的条目应该在")
        self.assertIn("CDQL-261025-01", r1, "CDQL 的条目也应该在（别名展开）")

    def test_AL1_mutation_no_expand_goes_red(self):
        """变异：把展开换回等值 → 只拿到一半（证明 AL1 有判别力）。"""
        r = fish.recent_fallback(self.conn, project="cdql", limit=10)
        only_cdql = [c for c, _ in r if c.startswith("cdql-")]
        self.assertEqual(len(only_cdql), 2, "cdql 自己应有 2 条")
        # 等值过滤（旧行为）只会给这 2 条；展开后必须多出 CDQL 那条
        self.assertGreater(len(r), 2,
                           f"展开后应多于等值结果（{len(r)} vs 2）——若相等说明没展开")

    # ------------------------------------------------------------------ AL2
    def test_AL2_inject_project_normalized(self):
        """inject 显式给了旧名，L1 要显示**当前真名**。"""
        pack, meta = fish.build_injection_pack(
            self.conn, self.data, self.cid, project="cdql")
        self.assertEqual(meta["project"], "CDQL",
                         f"显式声明要归一到规范名，实际 {meta['project']!r}")
        self.assertEqual(meta["project_source"], "explicit",
                         "来源仍是 explicit（调用方的声明优先级不变）")
        self.assertIn("当前项目：CDQL", pack,
                      f"L1 不该显示旧名。包片段：\n{pack[:400]}")

    # ------------------------------------------------------------------ AL3
    def test_AL3_activity_counts_merged(self):
        """「活跃 N 条」要合并统计两个入口，不是只看规范名。"""
        n, _last = fish._project_activity(self.conn, "cdql")
        self.assertEqual(n, 3, f"cdql(2)+CDQL(1) 应合并为 3，实际 {n}")
        n2, _ = fish._project_activity(self.conn, "CDQL")
        self.assertEqual(n2, 3, f"反向也应合并为 3，实际 {n2}")

    def test_AL3_note_lowcount_is_dangerous(self):
        """反面对照：只算规范名会得到 1——**数字偏小比偏大更危险**
        （它会让 AI 以为"这个项目没什么东西"从而不去查）。"""
        only_canon = self.conn.execute(
            "SELECT COUNT(*) FROM code_index WHERE project='CDQL' AND dormant=0 "
            "AND status='active'").fetchone()[0]
        self.assertEqual(only_canon, 1, "规范名下确实只有 1 条")
        merged, _ = fish._project_activity(self.conn, "cdql")
        self.assertGreater(merged, only_canon, "合并后必须大于只算规范名")

    # ------------------------------------------------------------------ AL5
    def test_AL5_no_alias_project_unchanged(self):
        """没有别名的项目行为不变：不会把别人算进来。"""
        r = {c for c, _ in fish.recent_fallback(self.conn, project="SOLO", limit=10)}
        self.assertEqual(r, {"SOLO-261025-01"},
                         f"SOLO 没有别名，不该多算别人的条目，实际 {sorted(r)}")
        n, _ = fish._project_activity(self.conn, "SOLO")
        self.assertEqual(n, 1, f"SOLO 活跃数应为 1，实际 {n}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
