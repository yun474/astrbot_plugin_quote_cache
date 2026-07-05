# Changelog

## v0.2.1

- 修复插件停用后 SQLite 已关闭、同一实例再次启用时无法初始化的问题。
- 初始化时自动重建已关闭的消息库，清理任务与 botpy payload 旁路支持重复启停。
- `terminate()`、SQLite `close()` 和 payload bridge `install()` 改为幂等操作。

## v0.2.0

- 修复未解析 botpy `message_reference.message_id`，导致普通引用无法命中缓存的问题。
- 新增 botpy 原始 payload 旁路，在 `message_scene/message_type/msg_elements` 被 `__slots__` 丢弃前暂存。
- WebSocket 与 Webhook 共用同一捕获逻辑，缓存事件处理完成后立即释放旁路数据。
- 自动补建 AstrBot `Reply` 消息段，修复“只引用并 @机器人”被 Agent 当作空消息跳过的问题。
- `/引用缓存调试` 增加原始 payload 捕获状态、字段列表及 `message_reference` ID。

## v0.1.1

- 增加 `auto_cleanup_enabled` 周期清理总开关。
- 明确 `ttl_hours` 缓存保留时间与 `cleanup_interval_minutes` 扫描周期。
- README 增加自动清理配置表、仓库安装地址与缓存位置说明。

## v0.1.0

- 首次发布：SQLite 多索引引用缓存、REFIDX/103 消息解析、富媒体上下文注入、机器人发送回包捕获与缓存管理指令。
