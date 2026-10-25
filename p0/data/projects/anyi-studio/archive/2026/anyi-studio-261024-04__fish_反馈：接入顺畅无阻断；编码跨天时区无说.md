# fish 反馈：接入顺畅无阻断；编码跨天时区无说明、recall 缺会话标注两点可改进

> 编码：anyi-studio-261024-04 ｜ 类型：lesson ｜ 记录于 2026-10-23T16:32:50Z

## 现象
本会话（anyi-studio-审查B，zcode 宿主）完整用了五个工具：whoami / register / remember ×3 / recall，四步接入自检一次全过，整体体验顺畅，与文档声明一致。两个可改进点：

1. **编码日期跨天无时区口径说明**：我 2026-10-23 深夜连续写三条，自检条目分到 anyi-studio-261023-05，十几分钟后两条审查结论分到 anyi-studio-261024-01/02——编码日期在会话中途跳天（本地 23:5x → 00:0x）。功能无碍，但「按本地日期推码」会错，回包与文档都没说明编码用哪个时区。
2. **recall 结果不带会话/宿主标注**：recall(query="接入自检") 命中审查A、审查B 两个会话的同标题条目（anyi-studio-261023-04/05），回包只有编码+类型+标题+匹配来源，无法直接分辨哪条是哪个会话写的，需要逐条 read_body 或凭编码时间推断。

## 复现
1. 调 whoami() → 得「登记身份：未登记（未配置 FISH_ACTOR；请调用 register 登记）；主人：安逸」——初始态符合设计。
2. 调 register(display_name="zcode-main", kind="role", role="代码审查", host="zcode", session_name="anyi-studio-审查B") → 得「已存在，直接使用：17927685072419E5219887E1C4 / zcode-main / role，会话 anyi-studio-审查B」——幂等复用+会话名一次绑成功，无需按预案二次调用。
3. 当晚约 23:05 调 remember(project="ANYI", title="接入自检：zcode-main", mem_type="episode") → 得 anyi-studio-261023-05；约 00:0x 再写两条 → 得 anyi-studio-261024-01/02。
4. 调 recall(query="接入自检") → 命中两条同标题条目（04=审查A会话、05=本会话），回包无会话名字段。

## 期望
1. remember 回包或使用文档注明编码日期的时区口径（如「编码取 UTC 日期」），避免跨天写入时按本地日期推错码。
2. recall 回包在条目行附带写入时的 session_name（或 host），多会话同名/同标题条目可直接区分，省去逐条 read_body。
其余保持现状即可：register 幂等语义、编码系统分配、禁删+supersede、关键词匹配的规则都清晰好用，工具描述里内嵌规则的做法对新会话很友好。

