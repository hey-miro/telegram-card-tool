# Telegram 个人账号批量发送名片：官方与 Telethon 调研

> 调研日期：2026-08-19。本文只使用 Telegram 官方文档/错误库/条款，以及 Telethon 官方文档和上游 GitHub 信息。

## 结论

没有找到能让 **Telegram 个人账号**稳定突破发送限制、同时保证不被限制的官方 API、Telethon 选项或 GitHub 补丁。Telegram 不公布各类请求的固定阈值，Telethon 也明确说阈值受很多因素影响。

可持续的解法是：正确区分服务端信号，严格遵守等待时间，减少非发送类 API，避免并发突发，对已授权的群组发送，并根据账号的真实反馈做自适应调度。这能提高长期成功量，但不是绕过风控。

> 实施状态（2026-08-19）：项目已按本调研落地默认直接发送、`resolvePhone` 慢速预处理、群慢速独立调度、RPC 限流事件记录、FloodWait 冷却和账号级持久化熔断。下文“建议”保留为方案与实现对照。

## 四类信号不能混为一谈

| 信号 | 官方含义 | 正确处理 |
| --- | --- | --- |
| `FLOOD_WAIT_X` | 调用某方法及参数的尝试过多，必须等待 X 秒 | 完整等待服务端给出的时间；发送链路暂停，不立即重试 |
| `SLOWMODE_WAIT_X` | 某一个聊天开启慢速模式，向该聊天再发送前需等待 X 秒 | 只暂停该目标群，不应阻塞其他群 |
| `PEER_FLOOD` | Telegram 当前错误库的描述是账号被标记/举报为 spam，并指示查询 `@SpamBot` | 立即停止该账号的自动发送，不做自动重试，由用户通过 `@SpamBot` 核对 |
| 举报型账号限制 | 私聊接收者和群管理员都可举报；审核后可临时限制，重复违规会延长甚至永久限制 | 这不是休眠几秒可解决的速率问题；只向明确预期接收内容的群发送 |

来源：[Telegram RPC 错误处理](https://core.telegram.org/api/errors)、[Telegram 官方错误库 JSON](https://core.telegram.org/api/errors.json)、[Telegram Spam FAQ](https://telegram.org/faq_spam?setln=en)。

注意：`FLOOD_WAIT` 是请求频率信号，`PEER_FLOOD` 是账号 spam 状态信号；后者不应当成普通限流继续重试。

## 上游真正提供的机制

### 1. `flood_sleep_threshold` 只负责“等待并重试”

Telethon 的 `flood_sleep_threshold` 会在 `FloodWaitError` 或 `SlowModeWaitError` 的等待值不大于阈值时自动 sleep，否则拋出异常。`request_retries` 则控制可重试请求的次数。这两个参数不会增加 Telegram 分配给账号的容量，也不会解除 `PEER_FLOOD`。

当前项目设置 `flood_sleep_threshold=0`，由任务层显示倒计时并支持停止。这个方向是合理的，比让 Telethon 在内部不可见地休眠更适合桌面工具。

来源：[Telethon RPC Errors](https://docs.telethon.dev/en/stable/concepts/errors.html)、[Telethon `TelegramClient` 参数](https://docs.telethon.dev/en/stable/modules/client.html)。

### 2. 联系人与 Peer 缓存值得保留

Telegram 明确要求客户端保存 peer 信息，避免不断请求未变更的数据。`contacts.importContacts` 也支持一次传入联系人向量，其 `retry_contacts` 是“因服务端系统限制未导入，稍后再试”的 client ID，不能当成“未注册”缓存。

当前项目已缓存已注册/未注册结果，也没有把 `retry_contacts` 写入未注册缓存，这部分与官方语义一致。

来源：[Telegram Peer database](https://core.telegram.org/api/peers)、[Telegram Contact list](https://core.telegram.org/api/contacts)、[`contacts.importContacts`](https://core.telegram.org/method/contacts.importContacts)。

### 3. 如果只需判断号码是否关联 Telegram，可用 `contacts.resolvePhone`

Telegram 提供 `contacts.resolvePhone`，可在不导入通讯录的情况下查询手机号。但官方明确要求客户端限速/防抖：**最多每 3 秒 1 次**。100 个缓存未命中的号码因此至少需要约 5 分钟预处理。

`PHONE_NOT_OCCUPIED` 也不能完全等同于“未注册”：当对方隐私设置禁止通过手机号查找时，官方说明也会返回同一错误。因此应将结果缓存为“未解析/不可见”，不宜显示为绝对未注册。

来源：[Telegram Contact list - Resolve a phone number](https://core.telegram.org/api/contacts)、[`contacts.resolvePhone`](https://core.telegram.org/method/contacts.resolvePhone)。

### 4. `random_id` 只避免技术性重发

`messages.sendMedia` 官方参数中的 `random_id` 用于避免同一条请求因网络重试被重复发送。这是传输层幂等，不应阻止用户在新任务或新轮次中主动重复发送相同名片。Telethon 的高层发送方法会为新请求生成随机 ID；若要做崩溃恢复，应保存当次发送的 request/random ID，而不是做跨任务内容去重。

来源：[`messages.sendMedia`](https://core.telegram.org/method/messages.sendMedia)。

## 没有找到的“捷径”

1. **没有个人账号的批量名片发送端点。** 真实名片通过 `inputMediaContact` + `messages.sendMedia` 发送。`messages.sendMultiMedia` 是相册/组合媒体方法，并非文档化的批量联系人通道。
2. **付费 flood-skip 不适用于个人账号。** `allow_paid_floodskip` 的官方说明是 bot-only；用户已明确不使用机器人。
3. **把多个 RPC 塞进一个 MTProto container 不是解限流。** 它只降低往返开销，每个请求仍由 Telegram 执行和计算，而更高并发会更早碰到限制。
4. **Takeout session 不适用于发送。** Telethon 将它定位为导出会话数据和批量下载媒体，不是消息发送通道。
5. **随机延迟、换代理、修改设备信息不会解除举报型限制。** 代理在 Telethon 文档中是连接问题的手段，不是发送容量手段。
6. **没有可信的“每天 N 条必安全”常数。** Telethon 官方文档明确说精确阈值未知且依赖很多因素。GitHub 上有用户报告约 50 次 DM 后出现 `PEER_FLOOD`，但这只是个案，不是官方阈值或可复用解法。

来源：[`InputMedia` 中的 `inputMediaContact`](https://core.telegram.org/type/InputMedia)、[`messages.sendMultiMedia`](https://core.telegram.org/method/messages.sendMultiMedia)、[Telethon takeout 文档](https://docs.telethon.dev/en/stable/modules/client.html)、[Telethon FAQ](https://docs.telethon.dev/en/stable/quick-references/faq.html)、[Telethon GitHub issue #397（仅个案）](https://github.com/LonamiWebs/Telethon/issues/397)。

Telethon 维护者也指出，`PEER_FLOOD` 是账号被认为可能在发送垃圾消息时由 Telegram 返回的限制，Telethon 无法修复：[Telethon GitHub issue #398](https://github.com/LonamiWebs/Telethon/issues/398)。

## 对当前代码的安全修改方案

### P0：本次已落地的五项

1. **将 `SlowModeWaitError` 与 `FloodWaitError` 拆开。** 每个 target 独立保存可再发时间，慢速群等待时继续处理其他群。
2. **对 `PEER_FLOOD` 做持久化硬熔断。** 状态写入 SQLite，重启后仍禁止自动发送，直到用户在 `@SpamBot` 确认状态并手动解锁。
3. **默认不导入通讯录，直接发名片。** `inputMediaContact` 只需手机号、姓名和 vCard；需要资料姓名或号码过滤时再显式启用预处理。
4. **需要检查号码时，使用 `resolvePhone` 慢速预处理。** 严格不快于 1 次/3 秒，优先读持久缓存。
5. **按 RPC 类型记录风控事件。** 持久化 `account_id / target_id / method / error_type / wait_seconds / occurred_at`，区分 `contacts.resolvePhone` 与 `messages.sendMedia` 产生的限流。

### P1：用数据提高长期吞吐

1. **自适应调度，不写死“安全条数”。** 在长时间无 `FloodWait` 时非常缓慢地缩短间隔；一旦出现 `FloodWait`，完整等待并大幅降低后续速率。这是工程上的拥塞控制策略，不是 Telegram 官方保证。
2. **发送前预处理，发送期不混入通讯录 RPC。** 将必须的 `importContacts`/姓名获取与正式发送分成两个任务，并优先用已有缓存。
3. **保留单账号单发送任务。** 不为了吞吐对同一账号并发 `sendMedia`；并发会压缩到达限制的时间，不会增加长期容量。
4. **仅保留任务内幂等。** 同一任务的网络重试复用 request/random ID；新任务和新轮次生成新 ID，允许用户主动重复发送。
5. **显示账号状态而不是伪造保证。** 前端分别显示“群慢速”、“方法限流”、“账号被 spam 限制”，并引导用户检查 `@SpamBot`。

## 运营侧的必要条件

即使完成以上代码改造，只要内容被群成员或管理员认为不受欢迎，账号仍可能被限制。因此需同时满足：

- 只向明确允许该内容的群组发送，并得到管理员同意；
- 发送内容是成员预期收到的，不是未经请求的广告或骚扰；
- 使用自己在 `my.telegram.org` 申请的 API ID，不发布共用 API ID；
- 受限时不通过重启、代理、多会话或重复请求绕过。

Telegram 官方明确表示会监控第三方 API 客户端，使用 API 进行 flooding/spamming 可被永久封禁：[Creating your Telegram Application](https://core.telegram.org/api/obtaining_api_id)。Telegram [Terms of Service](https://telegram.org/tos) 也禁止 spam/scam。

## 推荐实施顺序

1. 持久化记录实际出错 RPC，先确认目前的“100 次后限制”是 `SendMediaRequest` 的 `FLOOD_WAIT`，还是 `ImportContactsRequest` 的 `FLOOD_WAIT`，或者是 `PEER_FLOOD`。
2. 拆分每群 `SlowModeWait` 调度，在不超过单群限制的前提下提升多群吞吐。
3. 用小量、已授权的测试群验收“直接 `InputMediaContact`”的名片外观；通过后将通讯录导入改为可选预处理。
4. 若仍需过滤号码，加入持久化缓存的 `resolvePhone` 慢速预处理，严格遵守官方 3 秒间隔。
5. 持久化 `PEER_FLOOD` 熔断和手动解锁。
6. 最后再用至少一周的实际数据调整自适应间隔；随机抖动只用于平滑请求，不宣称能防封。
