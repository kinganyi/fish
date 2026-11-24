#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""112 · W-B（词表与比率单源）判据：A1 + N-3 + N-5。

| 判据 | 变异（打在语义开关上） |
|---|---|
| B1 通道词表只有一份，声明 == 库实际 == 写端接受 == 读端接受 | 往单源加 `hook` → B1 红 |
| B2 采纳率一个算法：`/api/stats` 与 `/api/push/overview` 同分母同值 | 一处分母改成 total → B2 红 |
| B3 同一会话键的正常路径与兜底路径给**同一个**短标签 | 兜底改回取末 4 位 → B3 红 |

## 通道词表的最终集合（W-B 的选择，写在这里防漂）

`push_log.channel` 的**实际值只有三个**：`inject` / `prompt` / `tool_after`
（生产 6,326 行实测；写端四个调用点也只写这三个）。旧词表多出的两个值各自来自
**另一个维度**：`hook` 是 `session_context.channel`（cli/hook/parent）的值，
`situational` 是候选 **kind** 的值。所以：

  · **UI 词表**（显示顺序）= 实际三词；
  · **写端兼容** = 三词 + 两个历史词（`LEGACY_PUSH_CHANNELS`），否则库里/外部
    调用方写过的历史行会被新校验挡回去；
  · **读端筛选**也必须接受这两个历史词——旧故障正是"写进去了却筛不出来"
    （`check_channel` 只认三词、写端接受五词）。聚合侧把历史词追加成额外行并
    标 `legacy=True`，让人看得见它是旧数据，而不是静默消失。

## 为什么 B1 要带"对照组"

`声明集合 == 库实际集合` 如果两边读的是同一个常量，永远相等——那是假绿。
所以本文件**在同一条用例里**先绿（三个通道写进去 → 相等），再删掉一个通道的行
（同一比较必须不等）。"能红"写进判据自身，而不是只写在变异脚本里。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

P0 = Path(__file__).resolve().parent
sys.path.insert(0, str(P0))

import fish  # noqa: E402
import fish_push_audit  # noqa: E402
import fish_push_vocab  # noqa: E402
import fish_ui  # noqa: E402
import fish_workbench_api  # noqa: E402

#: 生产库实际出现的通道（112 §三 A1：6,326 行 / 3 个值）。
CHANNEL_ACTUAL = ("inject", "prompt", "tool_after")
#: 两个历史词各自来自**另一个维度**，只保留写入兼容，不进 UI 词表。
CHANNEL_LEGACY = ("hook", "situational")


class _DbCase(unittest.TestCase):
    """临时库夹具：schema 现建、不碰生产库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-wb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "index.sqlite"
        self.conn = fish.connect(self.data)
        self.addCleanup(self._close)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.commit()

    def _close(self):
        try:
            self.conn.close()
        except Exception:      # noqa: BLE001 —— 夹具清理
            pass

    def _push(self, code, channel, reaction=None, session="dsh:wb-s1"):
        """走**生产写口**写一行推送（pushed_at 用当前时刻：窗口判据才不会随时间腐坏）。"""
        n = fish.append_push_log(self.conn, [code], channel=channel,
                                 session_key=session, pushed_at=fish.now_utc())
        if reaction:
            self.conn.execute("UPDATE push_log SET reaction=? WHERE code=? AND session_key=?",
                              (reaction, code, session))
        self.conn.commit()
        return n


class ChannelVocabularyTest(_DbCase):
    """B1（A1）：通道词表单源。"""

    def test_single_source_is_the_actual_three(self):
        self.assertIs(fish.PUSH_CHANNELS, fish_push_vocab.PUSH_CHANNELS,
                      "`fish.PUSH_CHANNELS` 必须是单源的**同一个对象**（别名，不是副本）")
        self.assertIs(fish_push_audit.CHANNELS, fish_push_vocab.PUSH_CHANNELS,
                      "审计读端的词表必须来自同一单源")
        self.assertEqual(tuple(fish_push_vocab.PUSH_CHANNELS), CHANNEL_ACTUAL,
                         "单源词表必须等于生产库实际出现的三个值")

    def test_declared_equals_db_actual_with_a_negative_control(self):
        for channel, reaction in zip(CHANNEL_ACTUAL, ("accepted", "ignored", None)):
            self.assertEqual(self._push("WBT-261124-01", channel, reaction), 1)
        actual = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT channel FROM push_log")}
        declared = set(fish_push_vocab.PUSH_CHANNELS)
        self.assertEqual(actual, declared,
                         "声明集合 != 库实际集合（112 C-B1）")
        self.assertEqual(actual, set(CHANNEL_ACTUAL))
        # 对照组：删掉一个通道的行，同一比较必须**不等**——否则判据读的是同一个常量
        self.conn.execute("DELETE FROM push_log WHERE channel='prompt'")
        actual_after = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT channel FROM push_log")}
        self.assertNotEqual(actual_after, declared,
                            "判据没有判别力：删掉一个通道后两边仍然相等")

    def test_write_side_keeps_legacy_values_writable(self):
        for channel in CHANNEL_LEGACY:
            self.assertEqual(self._push("WBT-261124-02", channel), 1,
                             "历史词 %r 的写入兼容被删了（外部调用方/历史行会被挡回去）"
                             % channel)
        with self.assertRaises(ValueError):
            fish.append_push_log(self.conn, ["WBT-261124-03"], channel="bogus")

    def test_read_filter_accepts_whatever_the_write_side_accepts(self):
        """旧故障的守门人：写端接受的值，读端必须筛得出来。"""
        for channel in CHANNEL_ACTUAL + CHANNEL_LEGACY:
            self.assertEqual(fish_push_audit.check_channel(channel), channel,
                             "写端能写进 %r，读端却判它非法——旧故障复发" % channel)
        with self.assertRaises(ValueError):
            fish_push_audit.check_channel("bogus")

    def test_aggregation_marks_legacy_rows_explicitly(self):
        """读端聚合：实际三词按 UI 顺序在前，历史词追加在后并标 `legacy=True`。"""
        for channel in CHANNEL_ACTUAL + CHANNEL_LEGACY:
            self._push("WBT-261124-04", channel)
        payload = fish_push_audit.overview(str(self.db))
        rows = {row["channel"]: row for row in payload["channels"]}
        self.assertEqual([row["channel"] for row in payload["channels"][:3]],
                         list(CHANNEL_ACTUAL),
                         "UI 词表的显示顺序变了（顺序是对外契约）")
        for channel in CHANNEL_ACTUAL:
            self.assertFalse(rows[channel].get("legacy"),
                             "实际通道被误标成 legacy")
        for channel in CHANNEL_LEGACY:
            self.assertTrue(rows[channel].get("legacy"),
                            "历史通道 %r 没有被显式标注（会看起来像当前值）" % channel)


class AcceptRateSingleSourceTest(_DbCase):
    """B2（N-3）：采纳率一个算法，三处调用同一个纯函数。"""

    def _seed(self):
        # accepted=2 / ignored=1 / pending=1 → 期望 2/3
        self._push("WBT-261124-11", "inject", "accepted", session="dsh:a")
        self._push("WBT-261124-12", "prompt", "accepted", session="dsh:b")
        self._push("WBT-261124-13", "tool_after", "ignored", session="dsh:a")
        self._push("WBT-261124-14", "inject", None, session="dsh:c")

    def test_formula_and_cross_endpoint_agreement(self):
        self._seed()
        stats = fish.push_stats(self.conn)
        self.assertAlmostEqual(stats["accept_rate"], 2 / 3, places=6,
                               msg="分母必须是 accepted+ignored（口径不许变）")
        audit = fish_push_audit.overview(str(self.db))
        self.assertEqual(audit["totals"]["acceptRate"], stats["accept_rate"],
                         "/api/push/overview 的 totals.acceptRate 与 /api/stats 不一致")
        # 逐通道行：同一 JSON 里两个 acceptRate 必须由同一个函数算出来
        acc = sum(row["accepted"] for row in audit["channels"])
        ign = sum(row["ignored"] for row in audit["channels"])
        self.assertEqual(audit["totals"]["acceptRate"],
                         fish_push_vocab.accept_rate(acc, ign),
                         "同一份响应里两个 acceptRate 出自不同公式（112 §四 N-3）")
        for row in audit["channels"]:
            self.assertEqual(row["acceptRate"],
                             fish_push_vocab.accept_rate(row["accepted"], row["ignored"]),
                             "通道行 %r 的 acceptRate 不是单源函数的输出" % row["channel"])

    def test_workbench_stats_uses_the_same_rate(self):
        self._seed()
        ro = fish_ui.open_readonly(str(self.db))
        self.addCleanup(ro.close)
        push = fish_workbench_api.collect_stats(ro, days=30, top=10)["data"]["push"]
        self.assertAlmostEqual(push["acceptRate"], 2 / 3, places=6,
                               msg="/api/stats 的 acceptRate 分母变了")
        for row in push["top"]:
            self.assertEqual(row["rate"],
                             fish_push_vocab.accept_rate(row["accepted"], row["ignored"]),
                             "推送 Top 行的 rate 不是单源函数的输出")

    def test_all_three_read_paths_call_the_one_function(self):
        """机制面：三处读口必须**调用**同一个函数——用"哨兵返回值"证明，而不是数次数。

        若某处又写了一份等价公式，它会算出真实比率而不是哨兵值 → 红。
        （数调用次数抓不住"内联公式"：内联也能算对，只是不再是单源。）
        """
        self._seed()
        ro = fish_ui.open_readonly(str(self.db))
        self.addCleanup(ro.close)
        with mock.patch.object(fish_push_vocab, "accept_rate", return_value=0.4242):
            stats = fish.push_stats(self.conn)
            audit = fish_push_audit.overview(str(self.db))
            push = fish_workbench_api.collect_stats(ro, days=30, top=10)["data"]["push"]
        self.assertEqual(stats["accept_rate"], 0.4242, "push_stats 没走单源函数")
        self.assertEqual(audit["totals"]["acceptRate"], 0.4242,
                         "overview 的 totals 没走单源函数")
        for row in audit["channels"]:
            self.assertEqual(row["acceptRate"], 0.4242,
                             "通道行 %r 的 acceptRate 没走单源函数" % row["channel"])
        self.assertEqual(push["acceptRate"], 0.4242, "/api/stats 没走单源函数")
        for row in push["top"]:
            self.assertEqual(row["rate"], 0.4242,
                             "推送 Top 行 %r 的 rate 没走单源函数" % row["code"])

    def test_no_decided_returns_none_not_zero(self):
        self._push("WBT-261124-21", "inject", None)
        stats = fish.push_stats(self.conn)
        self.assertIsNone(stats["accept_rate"],
                          "没有已判数据时必须 None（0 会被读成「推了没人理」）")
        audit = fish_push_audit.overview(str(self.db))
        self.assertIsNone(audit["totals"]["acceptRate"])
        for row in audit["channels"]:
            self.assertIsNone(row["acceptRate"])


class SessionShortLabelTest(unittest.TestCase):
    """B3（N-5）：短标签只取 id 的**一端**，两条路径同一个答案。"""

    IDS = (
        "dsh:session-88f9b6a5-7077-4c2e-9f1a",   # 正常形态：`session-` 前缀 + UUID
        "dsh:88f9",
        "self:企业微信-主AI",                    # 非 ASCII：按字符切，不切字节
        "codebuddy:abc",
    )

    def test_ui_tag_equals_naming_short(self):
        import fish_naming
        for key in self.IDS:
            want = fish_naming.session_short(key)
            self.assertTrue(want, "夹具无效：%r 在正常路径上取不到短标签" % key)
            self.assertEqual(fish_ui.session_tag(key), want,
                             "兜底路径与正常路径取的**不是同一端**（%r）" % key)
            self.assertEqual(fish_push_audit._session_tag(key), want)
            self.assertEqual(fish_workbench_api.session_tag(key), want)

    def test_tail_of_id_is_never_used(self):
        # 只挑「首尾不同」的 id：短 id（`dsh:88f9`）取哪端都一样，测不出方向
        for key in (self.IDS[0], self.IDS[2]):
            self.assertNotIn(key[-4:], fish_ui.session_tag(key),
                             "兜底路径又去取 id 末 4 位了（两端并存的老问题）")

    def test_empty_label_is_owned_by_the_call_site(self):
        self.assertEqual(fish_push_audit._session_tag(""), "(未归属)",
                         "审计侧的空标签契约不许变")
        self.assertEqual(fish_ui.session_tag(""), "")
        self.assertEqual(fish_workbench_api.session_tag(""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
