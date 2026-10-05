# AstrBot 历史消息与图片缓存

这个插件由原来的“QQ 引用消息缓存”改造而来。AstrBot 已经修好引用消息后，旧补丁终于可以光荣退休了；现在插件按会话保存 AstrBot 实际收到的消息，并把历史搜索能力注册成 LLM 工具。v1.3.0 起合并了原 [astrbot_plugin_image_cache](https://github.com/yun474/astrbot_plugin_image_cache)，图片也能先缓存、需要时再看。

仓库名和数据目录暂时保留为 `astrbot_plugin_quote_cache`，这样从旧版本升级时可以直接继续读取原来的 SQLite 数据库，不用折腾迁移。

## 功能

- 自动缓存 AstrBot 收到的消息。QQ 官方群聊、单聊按发送成功回执缓存机器人回复，包含分段发送、主动推送和单聊流式输出；其他平台仍在回复装饰阶段缓存正文。
- 保存原始消息 ID 与 QQ 引用索引 `idx`，复用现有 SQLite 表和会话隔离索引，方便其他插件按引用索引查询原始消息 ID。
- 群聊、私聊和不同平台实例分别保存；搜索工具默认只读取当前事件所在会话，也可由管理员改为全局范围。
- `search_chat_history` 支持正文子串、多关键词、发送者筛选、最近天数筛选和错别字近似匹配。
- `get_chat_history_context` 可根据搜索结果的 `record_id` 读取前后消息，避免第一次搜索就塞入一大坨上下文。
- 搜索工具可配置为“分群”或“全局”；全局模式会跨所有缓存会话搜索，并返回每条命中的会话来源。
- 适配器提供群名时会一并缓存，并在全局搜索结果中返回；取不到时保持为空，不会拿群号伪装群名。
- 历史正文里图片只写成 `[图片]`，不保存 URL；图片本身由下文的图片缓存单独保存。语音、视频、文件只保留占位符或文件名。
- 搜索和存储使用 Python 自带的 SQLite；图片缓存另需 AstrBot 自带的 `aiohttp`、`Pillow`、`mcp`。
- 管理员可查看状态、清理当前会话、清理全部或只清理过期记录。
- QQ 官方机器人收到的图片可单独缓存，模型按需查看，并能分清引用里的图是不是机器人自己发的，详见“图片缓存”。

## LLM 工具

插件会注册两个工具：

### `search_chat_history`

参数：

- `query`：关键词或短句；用空格分隔多个关键词时要求正文同时包含这些词。
- `limit`：返回条数。
- `sender`：可选，按昵称或用户 ID 片段筛选。
- `days_ago`：可选，只查最近若干天；`0` 表示全部有效缓存。

搜索优先执行完整缓存范围内的子串和多关键词匹配。如果结果不足，再对最近的候选消息做近似匹配。近似匹配候选数由 `fuzzy_candidate_limit` 控制，避免每次因为一个错别字就把几十万条消息全拉进 Python 算相似度。

### `get_chat_history_context`

参数：

- `record_id`：第一次搜索返回的记录 ID。
- `before`：读取目标记录之前多少条，最多 20。
- `after`：读取目标记录之后多少条，最多 20。

两种工具返回的聊天正文都会明确标注为“不可信资料”，提醒模型不要把历史消息中的文字当成系统指令执行。全局模式下，`get_chat_history_context` 只读取命中消息所属会话的相邻记录，不会把不同群聊混在一起。

## 图片缓存

只适配 QQ 官方机器人（WebSocket / Webhook）：收到的图片都会被收录，不需要 @，也不会回复或调用模型。平台范围跟随 `platform_allowlist`，与 `enabled`（历史消息缓存开关）互不影响，由 `image_cache.enabled` 单独控制。

1. **收图**：读取附件、引用链和原始 `msg_elements` 里的附件与正文图片链接（只认 `qpic.cn`、`qlogo.cn`、`multimedia.nt.qq.com(.cn)`）。框架已下载的图直接读本地文件，其余链接由插件下载，验证图片字节后按 SHA-256 去重落盘。QQ 没有表情包标记，表情包也按图片收录。
2. **提示**：每轮对话附上最近几张图的元数据（编号、类型、发送者、来源、时间），只有文字。当前消息的图已作为原生图片输入时，会注明无需再查看，避免重复计费。
3. **查看**：模型用 `image_cache_list(limit, sender_id, message_id, before)` 查询，用 `image_cache_view(image_ids)` 看图，一次最多 3 张、长边不超过 2048px，动图只看首帧。当前模型未启用图片模态时直接报错，不让模型瞎编。
4. **清理**：本轮 Agent 结束后，从历史中移除插件回传的预览图和索引，只留编号与回答。

### 引用了机器人自己的消息

AstrBot 的引用块只写昵称，QQ 推送里也没有被引用消息的作者，模型分不清引用里的图是不是自己发的。插件用上文“消息 ID 与引用索引”里缓存的发送回执，按收到消息的 `ref_msg_idx` 查找对应记录，命中机器人回复时在索引里注明“引用中的图片是你发的”，这些图片带 `sender_is_bot: true`，纯文字引用也会提示。

需要开启 `cache_bot_responses`，AstrBot `v4.26.8` 起才保留 `message_scene`；缓存之前发出的消息无法识别。

### 隔离与资源

- 群聊按群隔离，开启 `unique_session` 后同群成员仍共享图片；私聊按会话隔离；不同机器人实例互不可见。
- 数据位于 `data/plugin_data/astrbot_plugin_quote_cache/image_cache/`：`index.sqlite3` 存元数据，`images/` 存原图，每 15 分钟维护一次。
- 远程下载最多 4 个并发、全插件最多 64 个待处理候选，单个来源 25 秒、每条消息最多等 40 秒。提问时会等同会话仍在下载的图收尾。
- 只下载公共 HTTP(S) 地址，检查实际 DNS 解析和重定向，拒绝内网、组播、压缩包体和网络共享路径；不读系统代理，日志不输出带签名的 URL。
- 查看工具本身不发图。框架会提示模型可以用 `send_message_to_user` 发送预览，是否发送由模型判断。

### 边界

- 只能保存机器人实际收到的消息；过期或需要鉴权的腾讯 URL 会缓存失败，需要重新发图。
- AstrBot `v4.24.1` 及更早的 QQ 官方适配器会丢弃 `raw_data`、`msg_elements` 和 `message_scene`，收不到只存在于这些字段里的图片，也无法识别引用了机器人。
- 历史清理只针对 AstrBot 内置 Agent，Dify、Coze 等第三方 Agent 未验证。

## 安装与升级

可在 AstrBot 插件管理页面使用仓库地址安装：

```text
https://github.com/yun474/astrbot_plugin_quote_cache
```

如果从 v0.x 升级，插件会继续使用：

```text
data/plugin_data/astrbot_plugin_quote_cache/messages.sqlite3
```

数据库表结构仍然是原来的 `messages + aliases`。旧版本已经过期或已经被清理的记录无法恢复；旧库里残留的附件元数据仍能被读取，但 v1.0.0 不会把它们返回给 LLM，也不会再下载新附件。

升级后旧版的引用注入、QQ 原始 payload 旁路和附件落盘配置全部失效，可以在面板中删除那些旧配置。

**如果装过 `astrbot_plugin_image_cache`，请先卸载它**：两个插件会注册同名的 `image_cache_list` / `image_cache_view` 工具。图片缓存只保留 72 小时，旧图片目录不迁移，可手动删除 `data/plugin_data/astrbot_plugin_image_cache/`。使用前在人格中启用这两个工具，模型需要支持工具调用和图片输入。

## 消息 ID 与引用索引

QQ 的消息 ID 和引用索引是两种标识，按官方原值保存，不互相转换：

| 来源 | `original_message_id` | `ref_index` |
|---|---|---|
| 收到的 QQ 消息 | 原始事件 `id` | `message_scene.ext` 中的 `msg_idx` |
| QQ 机器人发送成功的回执 | 响应 `id` | 响应 `ext_info.ref_idx` |

收到的 `ref_msg_idx` 指向被引用的另一条消息，单独保存在 `raw_meta.ref_msg_idx`，不会注册成当前消息的别名。解析 `key=value` 时只切分第一个等号，保留 `REFIDX_...==` 的完整值。不会保存同一数组里的 `auth_token`。

原始 ID 和本条消息的引用索引都写入 `aliases`，可使用现有接口读取：

```python
# scope_key 必须来自当前平台实例和会话，不能跨会话猜测。
message = store.find(scope_key, [quoted_ref_index])
if message is not None:
    native_message_id = message.original_message_id
    own_ref_index = message.ref_index
```

这里的 `store` 是缓存插件的 `MessageStore` 实例。群聊 scope 为 `平台实例ID|group:群openid`，单聊为 `平台实例ID|private:用户openid`。这一版只保存和查询标识，不提供撤回命令。

实现依据：

- [QQ 群消息事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html)和[单聊消息事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)定义 `message_scene.ext` 的索引。
- [发送群聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)和[发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)定义回执 `id`、`ext_info.ref_idx`。
- 对照 [AstrBot QQ 入站适配器](https://github.com/AstrBotDevs/AstrBot/blob/6ddcb2fc088c67b302fb73f45b36d21f16b9fafd/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py)读取 `raw_message.raw_data`；不再添加接收端补丁。
- 对照 [AstrBot QQ 发送实现](https://github.com/AstrBotDevs/AstrBot/blob/6ddcb2fc088c67b302fb73f45b36d21f16b9fafd/astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py)，在 `botpy.http.BotHttp.request` 返回成功后记录 QQ v2 消息回执。私聊流式直接调用这个入口，主动推送也经过它；普通发送钩子无法统一拿到所有回执。

发送监听只记录属于当前 AstrBot QQ 平台实例的两个发消息接口，支持 WebSocket 和 Webhook；不修改请求与响应。发送失败不入库，缓存失败只记录日志，不触发消息重发；卸载插件时撤销监听。流式正文按分片 `index` 去重追加，`reset` 时覆盖，同一消息 ID 保留一条缓存。富媒体回执没有附件类型信息，因此发出的富媒体正文只记为 `[富媒体]`。

兼容性：合并图片缓存后，最低版本提高到 `astrbot_version >=4.23.2`（依赖 `on_agent_done` 等接口）；发送监听使用现有 QQ SDK。此次对照的适配器源码为 AstrBot 4.28.1（提交见上方链接），验证包含本地模拟事件、发送回执和 SQLite 测试，并使用 `qq-botpy 1.2.1` 与当前适配器的入站保留字段代码、私聊发送方法完成模拟网络回执验证；未连接真实 QQ 账号。旧适配器若未保留 `raw_data.message_scene`，只能缓存收到消息的 ID，无法补出它的 `msg_idx`；要完整保存入站索引，需要使用保留该字段的适配器。源码核对确认 4.26.8 已保留这个字段，但这里不将它冒充最早支持版本。

历史记录没有保存的真实 ID 或索引无法回填。QQ 未返回索引时保持为空，不按正文猜测匹配；缓存仍遵循保留天数和数量上限。频道消息与 QQ 群聊、单聊接口不同，暂时沿用原有缓存方式。

## 主要配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `enabled` | `true` | 启用消息缓存 |
| `enable_llm_search` | `true` | 向 LLM 提供两个历史工具 |
| `search_scope` | `group` | `group` 仅搜索当前群聊或私聊；`global` 跨所有缓存会话搜索 |
| `platform_allowlist` | 空 | 留空支持所有平台；可按适配器名或实例 ID 限制 |
| `retention_days` | `90` | 保留天数；`0` 表示不按时间过期 |
| `max_entries` | `200000` | 全库消息数上限；`0` 表示不限制 |
| `max_entries_per_group` | `0` | 单群消息数上限；`0` 表示不限制，不影响私聊 |
| `cache_bot_responses` | `true` | 缓存 AstrBot 发出的最终回复 |
| `max_message_chars` | `6000` | 单条正文缓存长度上限 |
| `default_result_limit` | `8` | 默认搜索结果数 |
| `max_result_limit` | `20` | 单次最多返回多少条 |
| `fuzzy_threshold` | `0.58` | 近似匹配阈值，越低越宽松 |
| `fuzzy_candidate_limit` | `3000` | 错别字近似匹配最多检查多少条最近消息 |
| `auto_cleanup_enabled` | `true` | 周期清理过期消息 |
| `cleanup_interval_minutes` | `60` | 清理周期 |
| `image_cache.enabled` | `true` | 启用图片缓存 |
| `image_cache.retention_hours` | `72` | 图片保留小时数，按最近一次收录计算 |
| `image_cache.max_cache_mb` | `512` | 全部会话共享的原图容量上限，MiB |
| `image_cache.max_image_mb` | `20` | 单张图片最大原始大小，MiB，最多 50 |
| `image_cache.context_count` | `5` | 每轮附带的最近图片条数，0–20 |
| `image_cache.max_images_per_message` | `20` | 每条消息最多缓存图片数，1–50 |

## 管理命令

以下命令仅限 AstrBot 管理员或 `admin_user_ids` 中列出的用户：

- `/历史消息状态`
- `/历史消息清理`：只清理过期记录。
- `/历史消息清理 当前会话`
- `/历史消息清理 全部`

清理命令只作用于历史消息；图片缓存按 `image_cache.retention_hours` 和容量上限自动清理。

## 边界和隐私

插件只能缓存平台适配器实际交给 AstrBot 的消息。某些平台不会把未提及机器人的普通群消息推给机器人，这种情况下插件不可能凭空得到完整群历史，别拿它当 QQ 聊天记录导出器使唤，真没有。

群名同样取决于平台适配器。AstrBot 标准消息对象包含群名字段，OneBot 等适配器通常可以提供；QQ 官方群机器人事件目前只包含不透明的 `group_openid`，官方 SDK 没有提供群资料查询接口，因此这类消息的群名会留空。QQ 频道与普通 QQ 群不是同一种接口，别被名字绕晕了。

数据库包含真实聊天正文、发送者 ID 和昵称。备份、迁移或分享 AstrBot 数据目录前，请先确认隐私范围。LLM 工具虽然做了当前会话隔离，但最终仍会把命中的正文交给当前使用的模型服务商。

主动调用工具还要求当前模型支持 function calling，并且该工具没有在 AstrBot 的工具管理页面被停用。模型是否会在合适的时候主动搜索，也会受到人格提示词和模型本身工具调用能力影响。
