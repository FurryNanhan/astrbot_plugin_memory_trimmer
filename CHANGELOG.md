# 更新日志 (CHANGELOG)

本项目遵循 [语义化版本 (SemVer)](https://semver.org/lang/zh-CN/)。

## [2.3.0] - 2026-11-07

### Added
- 新增多模态图片内容的字符长度估算机制。

### Fixed
- 修复了在截断上下文时，可能切断 Tool Calls (工具调用) 链路导致 API 报 400 错误的问题。现在会自动清理孤立的 `tool` 节点或悬空的 `assistant` 请求。

### Changed
- 项目正式重命名为 `memory_trimmer`（记忆修剪器）。

## [2.1.0] - 2026-10-31

### Added
- 新增 `length` 策略，支持基于上下文字符长度触发裁剪。
- 提供更平滑的渐进式清理逻辑 `progressive`。

## [1.0.0] - 2026-10-01

### Added
- 初始版本发布，支持固定人设和传统的硬裁剪 `hard` 模式。