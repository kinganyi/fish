# MCP 拿不到 token、只能跑 CLI，而 CLI 签发的是另一个公民（归属无法对齐）

> 编码：EVAL-261025-05 ｜ 类型：lesson ｜ 记录于 2026-10-25T08:21:18Z

## ① 症状

三步可复现：

**1. MCP 每次调用都带同一句警告**
```
⚠ 本次调用未带会话凭证，归属记为“未归属”（unattributed）。
   请先跑 `fish session start` 获取 token，后续调用带上 token=...
```
但 **MCP 工具列表里没有 `session start`**——11 个工具是 `register / whoami / remember / recall / resolve_code / read_body / neighbors / supersede / add_project / healthcheck / inject`，`whoami` 和 `inject` 虽然有 `token` **入参**，但没有任何口子能**产出** token。

**2. 只能离开 MCP 去跑 CLI**
```
cd ".../大肥鱼生命系统/p0"
python fish.py session start
→ token: fsh_ZTwp...
   citizen_id: 17929164334384A67C1B1F95C4
   display_name: codebuddy-8a83d218-8b2
   channel: cli
```

**3. 拿到的身份与 MCP 的不是同一个**
| 位置 | 公民 | 显示名 | 通道 |
|---|---|---|---|
| MCP `whoami` | `17926159596950B9E67A9298E4` | `workbuddy-main` | workbuddy |
| CLI `session start` | **`17929164334384A67C1B1F95C4`** | **`codebuddy-8a83d218-8b2`** | cli |

同一个宿主会话（`session_key` 都是 `codebuddy:8a83d218-...`），却对应**两个不同公民**。

## ② 影响

- **拿不到“对的”归属**：不带 token → `unattributed`；带 CLI 的 token → 归属到另一个公民。**两条路都不是想要的。**
- **破坏了“不要绕过工具”的约定**：MCP 自己解决不了，只能去命令行；而项目文档又要求“不要绕路”。两边打架。
- **花了额外一轮**：先试 `add_project`（被拒）→ 试 `healthcheck`（无 token 信息）→ 最后才想到 CLI。中间还有一次 `tail -12` 把 `token:` 那行截掉了，得重跑。
- **一个附带风险**：如果某个 AI 用 CLI token 写入大量记忆，之后按公民查归属会查到 `codebuddy-8a83d218-8b2`，与它在 MCP 侧的名字对不上，**追溯时会误判成两个人**。

## ③ 建议

按成本从低到高：

1. **`whoami` 顺手签发**：调用时若无 token，则签发一个并回传（反正 already 知道我是谁）。成本最低，闭环最短。
2. **`register` 回传 token**：登记即发凭证，语义上也顺。
3. **CLI 支持对齐身份**：`fish.py session start --display-name workbuddy-main`（或 `--citizen <id>`），让 CLI 签发的 token 落在同一个公民上。
4. **MCP 进程启动时自动签发并写入注入包**：最彻底——`inject` 的输出里直接带“本次会话 token: fsh_xxx”，AI 拿到就能用。用户原话是“token 在本次会话注入包里”，说明他以为已经是这样了，**但实测 `inject` 不带 token 调用时并不会返回 token**。

## 附：一条未验证的猜测（标清楚）

【未验证】CLI 与 MCP 分属两个公民，可能是“channel 不同就建新公民”的设计。我没有去读建公民的代码，不下结论。

