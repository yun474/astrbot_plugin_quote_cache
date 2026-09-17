# AstrBot 历史消息搜索

这个插件由原来的“QQ 引用消息缓存”改造而来。AstrBot 已经修好引用消息后，旧补丁终于可以光荣退休了；现在插件只做一件更实用的事：按会话保存 AstrBot 实际收到的消息，并把历史搜索能力注册成 LLM 工具。

仓库名和数据目录暂时保留为 `astrbot_plugin_quote_cache`，这样从旧版本升级时可以直接继续读取原来的 SQLite 数据库，不用折腾迁移。

## 功能

- 自动缓存 AstrBot 收到的消息。QQ 官方群聊、单聊按发送成功回执缓存机器人回复，包含分段发送、主动推送和单聊流式输出；其他平台仍在回复装饰阶段缓存正文。
- 保存原始消息 ID 与 QQ 引用索引 `idx`，复用现有 SQLite 表和会话隔离索引，方便其他插件按引用索引查询原始消息 ID。
- 群聊、私聊和不同平台实例分别保存；搜索工具默认只读取当前事件所在会话，也可由管理员改为全局范围。
- `search_chat_history` 支持正文子串、多关键词、发送者筛选、最近天数筛选和错别字近似匹配。
- `get_chat_history_context` 可根据搜索结果的 `record_id` 读取前后消息，避免第一次搜索就塞入一大坨上下文。
- 搜索工具可配置为“分群”或“全局”；全局模式会跨所有缓存会话搜索，并返回每条命中的会话来源。
- 适配器提供群名时会一并缓存，并在全局搜索结果中返回；取不到时保持为空，不会拿群号伪装群名。
- 图片不下载、不保存 URL，只写成 `[图片]`；语音、视频、文件也只保留占位符或文件名。
- 无第三方运行依赖，搜索和存储都使用 Python 自带的 SQLite。
- 管理员可查看状态、清理当前会话、清理全部或只清理过期记录。

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

兼容性：插件保留原有 `astrbot_version >=3.4.21` 声明，新增发送监听使用现有 QQ SDK，没有引入新的 AstrBot 事件 API。此次对照的适配器源码为 AstrBot 4.28.1（提交见上方链接），验证包含本地模拟事件、发送回执和 SQLite 测试，并使用 `qq-botpy 1.2.1` 与当前适配器的入站保留字段代码、私聊发送方法完成模拟网络回执验证；未连接真实 QQ 账号。旧适配器若未保留 `raw_data.message_scene`，只能缓存收到消息的 ID，无法补出它的 `msg_idx`；要完整保存入站索引，需要使用保留该字段的适配器。源码核对确认 4.26.8 已保留这个字段，但这里不将它冒充最早支持版本。

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

## 管理命令

以下命令仅限 AstrBot 管理员或 `admin_user_ids` 中列出的用户：

- `/历史消息状态`
- `/历史消息清理`：只清理过期记录。
- `/历史消息清理 当前会话`
- `/历史消息清理 全部`

## 边界和隐私

插件只能缓存平台适配器实际交给 AstrBot 的消息。某些平台不会把未提及机器人的普通群消息推给机器人，这种情况下插件不可能凭空得到完整群历史，别拿它当 QQ 聊天记录导出器使唤，真没有。

群名同样取决于平台适配器。AstrBot 标准消息对象包含群名字段，OneBot 等适配器通常可以提供；QQ 官方群机器人事件目前只包含不透明的 `group_openid`，官方 SDK 没有提供群资料查询接口，因此这类消息的群名会留空。QQ 频道与普通 QQ 群不是同一种接口，别被名字绕晕了。

数据库包含真实聊天正文、发送者 ID 和昵称。备份、迁移或分享 AstrBot 数据目录前，请先确认隐私范围。LLM 工具虽然做了当前会话隔离，但最终仍会把命中的正文交给当前使用的模型服务商。

主动调用工具还要求当前模型支持 function calling，并且该工具没有在 AstrBot 的工具管理页面被停用。模型是否会在合适的时候主动搜索，也会受到人格提示词和模型本身工具调用能力影响。
