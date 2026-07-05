# AstrBot QQ 引用消息缓存

这个插件用于弥补 QQ 官方适配器无法稳定向普通 Star 插件提供引用内容的问题。它优先使用 AstrBot 已分发的消息 ID 建库，同时保留 QQ 原始消息 ID、`msg_idx`、`ref_msg_idx`、发送回包 ID 等别名。用户引用历史消息时，插件会在同一机器人实例、同一群或同一私聊作用域内查库，并在调用 LLM 前注入引用内容。

## 能做什么

- 缓存收到的文本、图片、语音、视频、文件及常见消息段结构。
- 一条消息可同时使用 AstrBot 消息 ID、QQ 原始 ID、REFIDX 等多个索引查询。
- 优先解析 AstrBot `Reply.id`，并兼容 `message_scene.ext`、`message_type == 103` 与 `msg_elements[0]`。
- 尝试读取 QQ 官方发送接口回包中的 `id/ref_idx/msg_idx`，把机器人回复也加入缓存。
- 图片加入 `ProviderRequest.image_urls`，语音加入 `audio_urls`；文件和视频以结构化说明注入，小型文本文件还会附带内容预览。
- 默认保留 48 小时，每小时自动清理一次；按机器人实例和群聊/私聊隔离索引。

## 安装

将整个 `astrbot_plugin_quote_cache` 目录放入 AstrBot 插件目录，或在管理面板上传本目录的 zip 包，然后重载插件。

也可以直接使用仓库地址安装：

```text
https://github.com/yun474/astrbot_plugin_quote_cache
```

默认只处理 `qq_official` 和 `qq_official_webhook`。如果你的平台实例 ID 或适配器名称不同，在插件配置的 `platform_allowlist` 中追加实际名称；留空则处理所有平台。

## 自动清理配置

管理面板中可以直接调整以下三项：

| 配置项 | 默认值 | 作用 |
|---|---:|---|
| `auto_cleanup_enabled` | `true` | 是否运行后台周期清理任务；关闭后手动清理指令仍可用 |
| `ttl_hours` | `48` | 单条消息及附件保留多久，默认两天 |
| `cleanup_interval_minutes` | `60` | 每隔多久扫描并删除过期缓存，最小 5 分钟 |

插件每次启动时都会先清理一次已经过期的缓存。后台任务只删除超过 `ttl_hours` 的数据，不会因为扫描周期到达就清空仍在有效期内的消息。

## 缓存位置

插件数据位于 AstrBot 数据目录下：

```text
data/plugin_data/astrbot_plugin_quote_cache/
├── messages.sqlite3       # 消息、发送者、各类消息 ID、附件元数据
├── messages.sqlite3-wal   # SQLite 运行时文件，存在时不要单独删除
├── messages.sqlite3-shm   # SQLite 运行时文件，存在时不要单独删除
└── media/                 # 两天内引用可能使用的图片、语音、视频和文件
```

缓存中可能包含聊天内容和附件。备份、迁移或向他人发送 AstrBot 数据目录前，请自行确认隐私范围。

## 指令

以下指令仅限 AstrBot 管理员，或配置项 `admin_user_ids` 中列出的用户：

- `/引用缓存状态`：显示当前会话和全库条目数、TTL 与实际缓存路径。
- `/引用缓存清理`：立即删除所有已经过期的消息和媒体。
- `/引用缓存清理 当前会话`：清空当前群或当前私聊缓存。
- `/引用缓存清理 全部`：清空整个插件缓存。
- `/引用缓存调试`：建议“回复一条消息”后执行；显示当前事件的 raw 类型、可见字段、当前消息索引、引用目标索引、103 内嵌引用和缓存命中情况。

## 查询与注入顺序

1. 从 AstrBot 消息链的 `Reply.id` 查引用目标。
2. 从 `raw_message` 和 `event extra` 的 `message_scene.ext/ref_msg_idx` 等字段查目标。
3. 在当前作用域的 SQLite 别名表中查询。
4. 缓存未命中且 `message_type == 103` 时，解析 `msg_elements[0]` 并补写缓存。
5. 将引用文本作为额外用户内容块注入；旧版 AstrBot 没有该接口时退回到当前 prompt 前缀。

插件不会修改 `event.message_str` 或 AstrBot 的原始消息链，因此不会影响指令匹配。新版 AstrBot 若已经注入了 `<Quoted Message>`，本插件不会重复添加文本，但仍会补充本地缓存命中的图片和语音输入。

## 机器人回复缓存的边界

普通 Star 插件通常看不到 QQ 发送接口的返回值。本插件会对 QQ 官方事件实例的 `_post_send` 做一次局部兼容包装，在不修改 AstrBot 源码的前提下读取回包 ID。该接口是 AstrBot 的内部实现：

- 当前版本存在 `_post_send` 且 QQ 回包含 `id/ref_idx/msg_idx` 时，可以缓存机器人回复。
- 旧版或魔改版没有该方法、流式回包没有可用索引时会自动跳过，不影响正常发送。
- 即使机器人回复索引没有捕获，`message_type == 103` 的 `msg_elements[0]` 仍可作为最后兜底。

先开启 `debug_log` 并观察 `[quote-cache] outbound cached`，即可确认你的版本有没有拿到机器人回复索引。

## 附件安全与限制

- 本地附件会复制到插件媒体目录，避免 AstrBot 清理临时文件后引用失效。
- HTTP(S) 附件下载前会解析 DNS，并拒绝回环、内网、链路本地、保留地址和带用户名密码的 URL；重定向目标也会重新检查。
- 单个附件默认最大 50 MB，下载超时默认 20 秒，均可在配置中调整。
- 文件/视频是否能被模型直接理解取决于 LLM 提供商。本插件保证缓存并注入元数据；常见纯文本文件会额外注入预览。
- 语音能否被模型听懂取决于提供商是否支持 `audio_urls`。若 QQ payload 带 `asr_refer_text`，该转写也会进入引用说明。

## 首次验证建议

1. 在群里连续发送一条文字和一张图片，再引用它们并 @机器人。
2. 回复原消息执行 `/引用缓存调试`，确认“引用目标索引”与“缓存命中”为正常值。
3. 查看日志是否出现 `[quote-cache] quote hit`。
4. 引用机器人回复再测试一次；若只在这一步失败，重点看日志中是否出现 `outgoing response had no usable ID`。

如果调试结果里既没有 `Reply.id`，也没有 `message_scene/msg_elements/ref_msg_idx`，那就是适配器进入插件层前已丢弃引用关系。普通 Star 插件无法凭空恢复目标 ID，此时只能升级/修补 QQ 官方适配器，或改用保留完整 payload 的平台适配器插件。
