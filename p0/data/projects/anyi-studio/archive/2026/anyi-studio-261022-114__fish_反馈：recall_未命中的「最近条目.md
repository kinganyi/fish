# fish 反馈：recall 未命中的「最近条目」恒为同 5 条无关条目（ORDER BY code 字典序），且 project 过滤在 fallback 失效

> 编码：anyi-studio-261022-114 ｜ 类型：lesson ｜ 记录于 2026-10-22T10:13:09Z

# fish 反馈：recall 未命中的「最近条目」恒为同 5 条无关条目（ORDER BY code 字典序），且 project 过滤在 fallback 分支失效

> 编码：anyi-studio-261022-114（替代 -110）｜ 类型：lesson ｜ 首次记录 2026-10-22T09:19:30Z，本版 2026-10-22T10:15:00Z

## 现象

`recall` 未命中时，回退列表**恒为同 5 条、与查询无关**。2026-10-22 实测多次不同查询，返回完全一致：

| 查询 | 回退列表 |
|---|---|
| `recall("PIT-999")` | 企业智能-08/07/06/05/04 |
| `recall("EXP-05")`（未迁移时） | 同上 5 条 |
| `recall("PIT-061")`（旧码时期） | 同上 5 条 |
| `recall("未命中 回退")` | 同上 5 条 |
| `recall("PHP", project="ANYI")` | **同上 5 条**（全是「企业智能」前缀） |
| `recall("nginx 技术栈", project="ANYI")` | **同上 5 条** |

## 根因（代码级）

`p0/mcp_server.py` `tool_recall` 未命中分支：

```python
cand = self.conn.execute(
    "SELECT code,title FROM code_index ORDER BY code DESC LIMIT 5").fetchall()
return f"未找到「{label}」。最近的条目：\n" + ...
```

`ORDER BY code DESC` 是**字符串排序**：中文前缀（`企业智能…`，U+4F01 开头）排在任何 ASCII 前缀（`ANYI…`）之前 ⇒ 无论查询是什么，第一名永远是「企业智能」项目的最新几条。**它跟查询零关系，却措辞为“最近的条目”。**

## 补强证据：fallback 还绕过了 `project` 过滤（本版新增）

2026-10-22 18:0x，查重时传 `project=ANYI`：

```
recall(query="PHP", project="ANYI")        → 未找到「PHP」；返回 5 条全是企业智能-261022-0x
recall(query="漏洞", project="ANYI")       → 同上
recall(query="nginx 技术栈", project="ANYI") → 同上
recall(query="安全", project="ANYI")       → 正常命中 ANYI-20 / -21 / -82（说明过滤在命中侧是对的）
```

⇒ **命中侧正确遵守 `project`，未命中 fallback 完全不遵守**（SQL 里根本没有 `WHERE project=?`）。这让缺陷严重性上升一档：调用方以为“我在 ANYI 项目里没找到”，实际回包混入了**别的项目**的条目，比单纯的“同 5 条”更具误导性。

## 影响

1. **零信息量**：任何拼错、未迁移、真的不存在的编号，都得到同一批无关结果 → 无法区分“没迁”与“迁了但没命中”。
2. **更危险：会被误读为相关推荐**。我一度以为「企业智能」项目里有与 `EXP-05` 相关的条目，实际只是排序巧合。
3. **跨项目污染**：传了 `project` 反而拿到别的项目内容（见上节）。
4. 同类风险在**命中侧**也存在：`-83`（反馈条目标题里列举了 `PIT-062`）曾让查 `PIT-062` 命中一个“解释为什么查不到”的条目——**命中但答非所问**。

## 建议（任一即可显著改善）

1. **未命中改为报告检索面**：`标题 0 命中 / 正文 0 命中 / 编码片段 0 命中，库内共 N 条`——比列无关条目有用得多。
2. 如果仍想给“最近条目”，改 `ORDER BY recorded_at DESC` 且**明确标注“与查询无关”**，**并遵守传入的 `project` 过滤**。
3. 增加 `explain=true`：回包附带命中字段与片段（如 `匹配:正文` 的具体一行），便于区分真命中与假阳性。

## 备注

本条不是“建议优化体验”的空话：①②是对 `ORDER BY` 的错误选择（措辞暗示相关性、实际是字典序）且 fallback 漏了 `project` 约束，③是防假阳性的判据缺口。

## 复现命令

```
recall(query="PHP", project="ANYI", limit=10)
recall(query="不存在的词xyz", project="ANYI", limit=10)
recall(query="安全", project="ANYI", limit=10)   # 对照组：正常命中且过滤正确
```

期望：未命中时要么返回空 + 检索面统计，要么返回**本项目内**的最近条目。实际：返回跨项目的固定 5 条。

