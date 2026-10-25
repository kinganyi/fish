# 飞书跳组织文档读写：第二应用 bot 身份（免扫码）

> 编码：anyi-studio-261022-28 ｜ 类型：rule ｜ 记录于 2026-10-21T22:36:48Z

## 场景
主应用对目标 wiki 树无权限（131006 permission_denied / 3380004），user 与 bot 身份均被拒。

## 解法
第二应用 App ID `cli_aad8938290381bb6`，直接指定：

`lark-cli <命令> --as bot --profile cli_aad8938290381bb6`

- **不需要扫码登录**（profile add 会报 app-id already used，直接指 --profile 即可）
- `--as user` 对该 wiki 树无效

## 关键文档坐标
- 「电商 AI 图应用服务内部文档」wiki `OPWPwHK04i40zjkkOG9cUlEAnEh` → docx `V0EzdMu08oKco1x2rgYcBnY9nNd`
- 父节点「AI赋能业务」`L3vfwiIWiifhTLkX8hBcj5Jcn5e`｜space `7660826901723925491`

## 常用命令
- 读大纲：`docs +fetch --doc <id> --doc-format markdown --scope outline`
- 读全文：去掉 `--scope`
- 追加写入：`docs +update --doc <id> --command append --content @file.xml`
- 取 block id：`--doc-format xml --detail with-ids`
- 查字段类型：`base +field-list --base-token <t> --table-id <t>`
- 拉记录：`base +record-list --field-id <名称> --limit 200 --offset N`

## 坑
- `--detail with-ids` 只能配 `--doc-format xml`，配 markdown 会警告失效
- 写入前先 `--scope outline` 取结构，避免重复追加

