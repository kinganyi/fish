#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FB-14（批 D·D-1）：项目板「在动但未签到」——会话级**推断**节的判据（2026-10-26）。

> 2026-10-26（批 G · 评审 ⚪15）：节标题改成「在动但**未在本板登记**」。
> 旧措辞错一档：生产库实测 29 个会话都在「未分类」板自动签到过——它们**签过到**，
> 只是没签在这块板上。判据里的标题字符串随之更新（断言强度不变：仍是"标题必须出现 /
> 空看板必须不出现"），并由 `test_feedback_261026_g` 的 G15 钉住新措辞与那句说明。

## 实证背景（生产库只读）

会话 `dsh:b92c164c…` 有 64 条 `tool_activity`（一直在写 `fish-devA/p0/fish.py`），
但它的 presence 落在「未分类」板，项目板上**没有它的行**。旧渲染只读 presence
→ 项目板显示"没人"；用户因此以为"AI 明明在开发，看板却说离线"。

## 判据（三条，对应交付说明的 D1–D3）

  D1  造 project=P 的板 + 2 条窗口内 activity（会话 S / 公民 C）→ 渲染出现该节，
      含 S / C / 目录与次数；`presence` 表**不新增任何行**（推断不许变成事实）
  D2  再给 C 签一条 presence → 该节不再列 S（已在场表里）；另一路按
      `agent_id=会话键` 登记（自动签到的真实形态）同样要排除掉
  D3  超窗（40 分钟前）的 activity 不出现；别的 project 的 activity 不出现；
      空看板（无 presence 无 activity）**不新增空节**

## 为什么只对渲染产物断言

渲染的唯一入口是 `_render_one`：`board show`（CLI 与 MCP 的 `board` 工具）与
`board render` 都经过它。判据盯这一处，就同时钉住了两条读路径——
分别断言两处会漂（本项目反复付学费的那类问题）。

变异自证（交付说明第 3 条，人工跑，不复跑在 CI 里）：
  · 注释掉 `board_unchecked_sessions` 里的排除 → D2 必红；
  · 去掉 `fish_activity.activity_sessions` 的窗口条件 → D3 必红。
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

import fish_naming    # noqa: E402 —— 会话短标识（人类面唯一取法，F2 契约）
import fish_activity  # noqa: E402

# 真实的形态：公民是 26 位定长大写串，会话键是 `dsh:<session_id>`。
CID = "17929164334384A67C1B1F95C4"
OTHER_CID = "1792ABCDE21384A67C1B1F95C4"
SESSION = "dsh:b92c164c7a3e4a0b9f1c0000d1"
OTHER_SESSION = "dsh:0f0f0f0f7a3e4a0b9f1c0000d1"
NAME = "fish-开发AI"


def _ago(minutes: int) -> str:
    """`minutes` 分钟前的 UTC ISO（与库里的 `%Y-%m-%dT%H:%M:%SZ` 同口径）。"""
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Feedback261026DTest(unittest.TestCase):
    """批 D·D-1：看板补上「在动但未签到」这一屏（**推断**，只读）。"""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "data"
        self.conn = fish.connect(self.root)
        self.conn.executescript(fish.SCHEMA)
        fish.migrate(self.conn)
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, host, status, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (CID, NAME, "role", "dsh", "active", "2026-10-20T00:00:00Z"))
        self.conn.execute(
            "INSERT INTO citizens (citizen_id, display_name, kind, host, status, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (OTHER_CID, "别的会话", "role", "dsh", "active", "2026-10-20T00:00:00Z"))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:               # noqa: BLE001 —— 收尾不该掩盖断言失败
            pass
        shutil.rmtree(self.base, ignore_errors=True)

    # ------------------------------------------------------------------ 夹具
    def _record(self, *, project, minutes_ago=3, path_dir="p0",
                session_key=SESSION, citizen_id=CID, action="write"):
        """落一条 `tool_activity`（走采集层的真实入口，不手写 INSERT）。"""
        classification = {
            "project": project, "tool_name": "write", "action": action,
            "path": f"{path_dir}/fish.py", "path_dir": path_dir,
            "path_abs": f"I:/app/DSH-Home/workspace/learn/{project}/{path_dir}/fish.py",
            "args_digest": "0123456789abcdef", "ok": 1,
        }
        return fish_activity.record(
            self.conn, host="dsh", session_key=session_key,
            citizen_id=citizen_id, classification=classification,
            seen_at=_ago(minutes_ago))

    def _render(self, project="P"):
        """走**唯一渲染出口**：落文件再读回来（与 `board show` 同一份正文）。"""
        board = fish.board_ensure(self.conn, "project", project)
        fish.board_render(self.conn, self.root, board)
        path = fish.board_out_path(self.root, board)
        return path.read_text(encoding="utf-8")

    def _presence_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM presence").fetchone()[0]

    # ------------------------------------------------------------------ D1
    def test_D1_unchecked_session_surfaces_with_dirs_and_writes(self):
        """D1：有 activity、没签到 → 出现该节（含 S/C/目录/次数），且不写 presence。"""
        fish.board_ensure(self.conn, "project", "P")
        self._record(project="P", minutes_ago=4)
        self._record(project="P", minutes_ago=2)
        before = self._presence_count()
        self.assertEqual(before, 0, "起点没有 presence（否则这条判据测不到东西）")

        text = self._render("P")

        self.assertIn("### 在动但未在本板登记（推断：来自 tool_activity）", text,
                      "标题必须带「推断」——它不是签到")
        self.assertIn("窗口 30 分钟", text, "标题里要写清窗口（默认 30 分钟）")
        self.assertIn(fish_naming.session_short(SESSION), text,
                      "会话要能被认出来（人类面只出短标识，见 45 §4.1 S18 后半）")
        self.assertNotIn(SESSION, text, "人类面不许出现完整会话键（F2 契约）")
        self.assertIn(NAME, text, "公民要出**可读名**（不出 26 位 ID 是既有契约）")
        self.assertIn("p0（2 次）", text, "目录与写次数都要印（推断的原始证据）")
        self.assertIn("| 2 |", text, "写次数列 = 2")
        self.assertGreater(text.index("### 在动但未在本板登记"),
                           text.index("## 在场"),
                           "这一节必须在在场表**之后**（先声明，再推断）")

        self.assertEqual(self._presence_count(), before,
                         "这一节是推断：只读聚合，不许往 presence 写任何行")

        # 采集层的契约（渲染依赖它）：字段齐全 + 目录倒序 + 最多 3 个
        sessions = fish_activity.activity_sessions(self.conn, project="P")
        self.assertEqual(len(sessions), 1)
        row = sessions[0]
        self.assertEqual(set(row), {"session_key", "citizen_id", "display_name",
                                    "dirs", "writes", "last_seen"})
        self.assertEqual(row["session_key"], SESSION)
        self.assertEqual(row["citizen_id"], CID)
        self.assertEqual(row["display_name"], NAME)
        self.assertEqual(row["writes"], 2)
        self.assertEqual(row["dirs"][0]["dir"], "p0")
        self.assertEqual(row["dirs"][0]["count"], 2)

    # ------------------------------------------------------------------ D2
    def test_D2_checked_in_session_no_longer_listed(self):
        """D2：签过到的会话不再出现在这一节（两种登记形态都要排除）。"""
        fish.board_ensure(self.conn, "project", "P")
        self._record(project="P", minutes_ago=2)
        fish.board_checkin(self.conn, CID, doing="在改 fish.py",
                           scope="project", scope_key="P")

        text = self._render("P")
        self.assertIn("### 在动但未在本板登记（推断：来自 tool_activity）", text,
                      "在场表非空 → 这一节要在（哪怕结论是「无」）")
        self.assertNotIn(SESSION, text, "已签到的会话不许被重复列一遍")
        self.assertIn("_（无）_", text)
        self.assertIn("都已签过到", text, "空的原因要写清楚，不许只印一个「无」")

        # 第二种形态：`agent_id` 存会话键（`board_auto_checkin` 的真实写法）
        fish.board_ensure(self.conn, "project", "Q")
        self._record(project="Q", minutes_ago=2, session_key=OTHER_SESSION,
                     citizen_id=OTHER_CID)
        fish.board_checkin(self.conn, CID, doing="自动登记：会话开始",
                           scope="project", scope_key="Q", agent_id=OTHER_SESSION)
        text_q = self._render("Q")
        self.assertNotIn(OTHER_SESSION, text_q,
                         "agent_id 上记着会话键的签到行同样算「已在场」")

    # ------------------------------------------------------------------ D3
    def test_D3_window_and_project_isolation_and_empty_board(self):
        """D3：超窗不出现、别的项目不出现、空看板不新增空节。"""
        # ① 窗口外的写动作（40 分钟前 > 默认 30 分钟）
        fish.board_ensure(self.conn, "project", "P")
        self._record(project="P", minutes_ago=40, session_key="dsh:stale0000",
                     path_dir="old")
        # ② 别的项目的窗口内写动作
        self._record(project="OTHER", minutes_ago=1, session_key="dsh:other0000",
                     path_dir="otherp0")

        text = self._render("P")
        self.assertNotIn("dsh:stale0000", text, "超窗的会话不算「此刻在动」")
        self.assertNotIn("old（", text, "超窗的目录同样不许出现")
        self.assertNotIn("dsh:other0000", text, "别的项目的会话不许串到本板")
        self.assertNotIn("otherp0", text)
        self.assertNotIn("### 在动但未在本板登记", text,
                         "无 presence 且窗口内无活动的空看板：保持原样，不新增空节")

        # ③ 窗口参数真的生效：把窗口放大到 60 分钟，那条 40 分钟前的就该出现
        sessions = fish_activity.activity_sessions(self.conn, project="P",
                                                   window_min=60)
        self.assertEqual([s["session_key"] for s in sessions], ["dsh:stale0000"],
                         "window_min 必须真的过滤，而不是写死 30 分钟")

        # ④ 有 presence 但没有活动数据 → 出节并印 `_（无）_`（"看过、没有"也要说）
        fish.board_ensure(self.conn, "project", "NOACT")
        fish.board_checkin(self.conn, CID, doing="在写文档",
                           scope="project", scope_key="NOACT")
        text_noact = self._render("NOACT")
        self.assertIn("### 在动但未在本板登记（推断：来自 tool_activity）", text_noact)
        self.assertIn("_（无）_", text_noact)
        self.assertIn("窗口内没有观测到写动作", text_noact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
