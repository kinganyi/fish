# add_project 拒绝已存在且正在使用的前缀（anyi-studio 含 -），会误导人重建重复项目

> 编码：EVAL-261025-17 ｜ 类型：lesson ｜ 记录于 2026-10-25T11:22:49Z

## ① 症状

**对已存在的前缀调 `add_project` 会被拒，但该前缀实际上完全可用。**

```
add_project(prefix="anyi-studio", namespace="anyi-studio 工作空间")
→ 错误：prefix 不能含 '-'（编码用它分隔段，如 ANYI-261022-01）
```

但同一个库里：
```
recall("anyi")
→ anyi-studio-261022-99 / anyi-studio-261025-16 / anyi-studio-261024-14 …（大量）

remember(project="anyi-studio", ...)
→ anyi-studio-261025-21   ✅ 写入成功，路径 projects/anyi-studio/
```

## ② 影响

- **任何会话想确认「anyi-studio 项目是否已注册」，会得到否定答案**（报错）。合理反应是“那换个名字”——于是可能建出 `ANYISTUDIO` 或 `anyi_studio`，**与刚刚完成的治理目的（拆成单一 `anyi-studio`）正好相反**。
- 我先试了 `add_project`，被拒后不得不额外用一次 `remember` 来验证前缀到底能不能用——**多一次写入才能确认一个只读事实**。
- 这个矛盾很难自己发现：只有当你“既看到编码存在、又看到 add_project 报错”时才会意识到不对。

## ③ 建议

1. **`add_project` 先查库、再校验字符**：前缀已存在就直接返回「已存在：<prefix> → <namespace>」，别拿字符规则拦一个已经落地的存量。
2. **把 `-` 禁令限定在“新前缀”**，存量白名单放行（或至少给一条明确的“存量含 - 的前缀不适用本工具”的提示）。
3. 如果短期内不改，至少在报错文案里加一句：**“注意：含 `-` 的前缀可能由数据治理直建，可用 `recall` 或 `remember` 验证其是否存在”**。现在这条错误会把人往错方向推。

