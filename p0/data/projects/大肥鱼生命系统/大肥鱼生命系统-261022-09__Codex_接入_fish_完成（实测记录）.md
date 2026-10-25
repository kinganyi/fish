# Codex 接入 fish 完成（实测记录）

> 编码：大肥鱼生命系统-261022-09 ｜ 类型：fact ｜ 记录于 2026-10-21T22:00:06Z

1. 传输方式：MCP stdio，通过 I:\app\codex\config.toml 的 [mcp_servers.fish] 接入
2. 关键坑：Codex CLI 读 ~/.codex/config.toml，而桌面版数据在 I:\app\codex；
   用 CLI 必须先设 CODEX_HOME=I:\app\codex，否则看不到任何 MCP
3. 身份：FISH_ACTOR=codex-main，已 register 为 visitor，owner 归属正确
4. 验证：whoami 返回 codex-main + 主人安逸；register 幂等；remember 返回编码
