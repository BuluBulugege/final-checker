# final-checker 项目约定

## 架构

插拔式 API Key 检查器。FastAPI 后端 + Neo-Brutalism Web UI。

- 插件在 `app/plugins/` 下，实现 `CheckerPlugin` 基类，模块末尾 `PLUGIN = XxxPlugin()` 自动注册
- 配置在 `app/config.py`，所有阈值/速率/模型ID 集中管理，可被 `FC_` 前缀环境变量覆盖
- HTTP 请求统一用 `app/http_util.py` 的 `timed_request()`
- 密钥脱敏用 `app/redact.py`
- 输入解析在 `app/parsing.py`，支持 JSON 块 + 多行凭证自动拼接

## 6 个插件

gemini / openai / anthropic / gcp / azure / aws_bedrock

## 命令

- `/new-plugin` — 创建新检查插件
- `/check` — 运行 key 检查
- `/plugins` — 查看已注册插件
