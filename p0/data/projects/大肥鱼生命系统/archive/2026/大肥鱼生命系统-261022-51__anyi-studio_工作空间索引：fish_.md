# anyi-studio 工作空间索引：fish 作入口，正文留在 artifacts/notes

> 编码：大肥鱼生命系统-261022-51 ｜ 类型：rule ｜ 记录于 2026-10-22T04:57:55Z

# anyi-studio 工作空间索引（fish 作为入口，正文不搬家）

> 迁移策略：首周只建「索引 + 结论」，不搬长文。fish 的注入包有 1500 token 硬预算，把 10KB 的 MEMORY.md 塞进来会直接挤掉别的东西。

## 一、正文仓库（权威源，仍在 workspace）

| 位置 | 内容 | 什么时候读 |
|---|---|---|
| `.workbuddy/memory/MEMORY.md` | 指针版长记忆：ER-1~31 铁律、协作约定 CR-1~15、方向状态 | 开工必读 |
| `.workbuddy/memory/notes/error-ledger.md` | PIT-001~102 错题本 | 接手/踩坑必读 |
| `.workbuddy/memory/notes/collab-rules.md` | 协作铁律权威源 | 多人协作前 |
| `.workbuddy/memory/notes/编号注册表.md` / `经验值清单.md` / `模板集.md` | 编号/经验/模板 | 按需 |
| `artifacts/` | 报告与产物（按编号目录：01 工程、05 交接、10 合规…） | 按需 |
| `.workbuddy/memory/YYYY-MM-DD.md` | 每日流水 | 新会话开工 |

## 二、fish 里存什么 / 不存什么

**存**：可检索的结论、规则、教训、报告指针（artifact 带附件）、跨会话需要“被想起来”的事实。
**不存**：长文正文、代码、每日流水逐条、临时排查过程。理由：inject 硬预算 1500 token，且 recall 是标题/关键词匹配。

## 三、检索约定（实测）

- `recall` 是**标题/关键词**匹配，不是语义检索：查“海报文案规律”不中，查“文案”才中 → **标题里必须写可查词**。
- `recall` 不返回正文，要看全文用 `read_body`。
- `recall` 默认不返回已 superseded 的，查历史加 `include_superseded=true`。

## 四、会话坐标

- 宿主 workbuddy｜身份 workbuddy-main（role，公民 17926159596950B9E67A9298E4）｜会话名 **anyi-studio-审查AI**｜主人 安逸
- 同项目其他会话：开发AI / 开发AI-2 / 前端AI / 主AI（各自 register 自己的会话名）

## 五、首周（2026-10-22 ~ 10-29）迁移节奏

1. 先建索引与规则（本条）；2. 再把 PIT 错题本按主题合并成 lesson（带 PIT 编号索引）；3. 最后才考虑把高频报告转 artifact 带附件。
**未全量迁入前，以 workspace 正文为准；fish 里的结论若与正文冲突，以正文为准。**

