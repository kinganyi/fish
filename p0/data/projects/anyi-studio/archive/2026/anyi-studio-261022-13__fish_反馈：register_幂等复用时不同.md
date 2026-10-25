# fish 反馈：register 幂等复用时不同显 session_name，写入前无法确认归属

> 编码：anyi-studio-261022-13 ｜ 类型：lesson ｜ 记录于 2026-10-21T22:24:49Z

**场景**：本会话按指引调 `register(display_name="workbuddy-main", kind="role", host="workbuddy", session_name="anyi-studio-主AI")`。

**卡在哪**：返回只有两句——「已存在，直接使用：17926159596950B9E67A9298E4」「workbuddy-main / role」。因为同名会幂等复用，我**无法从返回值判断 `session_name` 到底生效了没有**：是同一条公民换了会话，还是仍绑在旧会话上？

**后果**：下一条 `remember` 我是在“可能写错会话”的惄惄不安中发的；直到它的回执尾部出现「会话 anyi-studio-主AI」才确认。也就是说，**写入前拿不到确认，只能靠写入后的事后回执反推**。

**期望**：register 的回执把实际绑定的 `session_name` 一并回显（例：“已存在，复用公民 …；本会话：anyi-studio-主AI”），或者明确说“未传 session_name，沿用旧的”。一行字就能消除这个不确定。

