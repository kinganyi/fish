# 修复 BUG-1：supersede 回显改读库内实际替代者（不再假成功）

> 编码：大肥鱼生命系统-261022-39 ｜ 类型：lesson ｜ 记录于 2026-10-21T23:32:01Z

根因：supersede_memory() 在「已是 superseded」分支回显的是入参 new_code，不是库内 superseded_by。
后果：后到会话看到「被 <我的入参> 替代过」，误以为替代生效，得到假成功（真实案例 anyi-studio-261022-13）。
改法：该分支改为读 code_index.superseded_by（o[2]），并对齐同文件 tool_resolve() 已有的读库写法。
返回文案明确区分「本次未生效」，并给下一步（改对实际替代者再调 supersede，形成链）。
新增 2 条测试：回显=库内实际 + 空操作不追加事件；另 1 条判据变异自证。
验证：test_p0 5 + test_p1a 16 + test_mcp 14 = 35 全绿；树外变异（换回旧版 fish.py）仅新判据变红。
零新增：无新字段/新工具/新事件类型。

