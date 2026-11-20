#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集活性判据（2026-11-20 · DSH hook 断流事故的防线）。

这份文件只回答一个问题：**采集通道停下来的时候，有没有东西会说话**。

2026-11-09 起 DSH 侧 hook 断流 11 天没人发现，不是因为它难查，而是因为
**没有任何东西会因为它坏了而报错**：WorkBuddy 一直在采，总数看起来一直"有数据"。

所以这里锁三件事：

- **L1 每个 host 单独判**：一个通道正常不代表另一个正常。只看总量时，
  单通道断流会被另一通道的数据完全掩盖。
- **L2「没采过」与「很久没采」分开**：前者是没接过（配置可能没生效），
  后者才是断了。两者的处置不同，混成一条就分不清该查哪里。
- **L3 判据真的在读时间**：同一份夹具，阈值一紧一松必须给出不同结论。
  这是变异自证——证明这条判据有判别力，而不是恒真。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fish  # noqa: E402
import fish_activity  # noqa: E402


def _utc(*, minutes_ago=0):
    import datetime
    base = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


class LivenessTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-live-"))
        self.root = self.tmp / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    def _record(self, host, *, minutes_ago):
        self.conn.execute(
            "INSERT INTO tool_activity (host, session_key, tool_name, action, seen_at) "
            "VALUES (?,?,?,?,?)",
            (host, "sess-" + host, "Write", "write", _utc(minutes_ago=minutes_ago)))
        self.conn.commit()

    # ---------------------------------------------------------------- L1

    def test_L1_fresh_host_is_ok(self):
        """刚采过的通道不该被报成断流（否则报警天天响，人就会忽略它）。"""
        self._record("dsh", minutes_ago=5)
        res = fish_activity.collection_liveness(self.conn, hosts=("dsh",))
        self.assertEqual(res["stale_hosts"], [])
        self.assertTrue(res["ok"])

    def test_L1_one_host_stale_does_not_hide_behind_another(self):
        """一个通道停了、另一个还在采 —— 必须只报停的那个。

        这正是本次事故的形态：WorkBuddy 一直在采，DSH 早就停了，
        总数看起来完全正常。只看总量永远发现不了。
        """
        self._record("workbuddy", minutes_ago=5)
        self._record("dsh", minutes_ago=60 * 48)         # 两天前
        res = fish_activity.collection_liveness(self.conn, hosts=("dsh", "workbuddy"))
        self.assertEqual(res["stale_hosts"], ["dsh"])
        self.assertFalse(res["ok"])
        self.assertFalse(res["hosts"]["workbuddy"]["stale"])

    # ---------------------------------------------------------------- L2

    def test_L2_never_seen_is_not_the_same_as_stale(self):
        """声明过却一次都没采到 → 报 never_seen，不混进 stale。

        两种处置不同：never_seen 要去查"配置/插件到底有没有装配"，
        stale 要去查"它是什么时候停的"。混成一条就不知道该往哪查。
        """
        res = fish_activity.collection_liveness(self.conn, hosts=("dsh",))
        self.assertEqual(res["never_seen_hosts"], ["dsh"])
        self.assertEqual(res["stale_hosts"], [])
        self.assertIsNone(res["hosts"]["dsh"]["last_seen"])
        self.assertFalse(res["ok"])

    # ---------------------------------------------------------------- L3

    def test_L3_threshold_actually_matters(self):
        """**变异自证**：同一份夹具，阈值松→绿、紧→红。

        如果两个阈值给出同一个结论，说明这条判据根本没在读时间——
        那样的"绿"没有任何意义。
        """
        self._record("dsh", minutes_ago=60 * 48)         # 两天前
        lenient = fish_activity.collection_liveness(self.conn, stale_hours=24 * 30)
        strict = fish_activity.collection_liveness(self.conn, stale_hours=1)
        self.assertEqual(lenient["stale_hosts"], [],
                         "30 天阈值下，两天前的记录不算断流")
        self.assertEqual(strict["stale_hosts"], ["dsh"],
                         "1 小时阈值下，必须把它报出来")

    def test_L3_undeclared_host_still_listed(self):
        """没在 `hosts` 里声明的通道，出现过就要照实列——不能只报"关心的"那几个。"""
        self._record("somehost", minutes_ago=5)
        res = fish_activity.collection_liveness(self.conn, hosts=("dsh",))
        self.assertIn("somehost", res["hosts"])
        self.assertFalse(res["hosts"]["somehost"]["stale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
