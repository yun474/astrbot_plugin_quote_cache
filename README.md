# AstrBot 历史消息搜索

这个插件由原来的“QQ 引用消息缓存”改造而来。AstrBot 已经修好引用消息后，旧补丁终于可以光荣退休了；现在插件只做一件更实用的事：按会话保存 AstrBot 实际收到的消息，并把历史搜索能力注册成 LLM 工具。

仓库名和数据目录暂时保留为 `astrbot_plugin_quote_cache`，这样从旧版本升级时可以直接继续读取原来的 SQLite 数据库，不用折腾迁移。

## 功能

- 自动缓存 AstrBot 收到的消息，以及 AstrBot 最终发出的回复。
- 群聊、私聊和不同平台实例严格隔离；LLM 只能搜索当前事件所在的会话。
- `search_chat_history` 支持正文子串、多关键词、发送者筛选、最近天数筛选和错别字近似匹配。
- `get_chat_history_context` 可根据搜索结果的 `record_id` 读取前后消息，避免第一次搜索就塞入一大坨上下文。
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

两种工具返回的聊天正文都会明确标注为“不可信资料”，提醒模型不要把历史消息中的文字当成系统指令执行。

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

## 主要配置

| 配置项 | 默认值 | 说明 |
|---|---:|---|
| `enabled` | `true` | 启用消息缓存 |
| `enable_llm_search` | `true` | 向 LLM 提供两个历史工具 |
| `platform_allowlist` | 空 | 留空支持所有平台；可按适配器名或实例 ID 限制 |
| `retention_days` | `90` | 保留天数；`0` 表示不按时间过期 |
| `max_entries` | `200000` | 全库消息数上限 |
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

数据库包含真实聊天正文、发送者 ID 和昵称。备份、迁移或分享 AstrBot 数据目录前，请先确认隐私范围。LLM 工具虽然做了当前会话隔离，但最终仍会把命中的正文交给当前使用的模型服务商。

主动调用工具还要求当前模型支持 function calling，并且该工具没有在 AstrBot 的工具管理页面被停用。模型是否会在合适的时候主动搜索，也会受到人格提示词和模型本身工具调用能力影响。
