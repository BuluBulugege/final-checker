---
description: "Run API key checks via CLI or Web UI"
---

# /check — 运行 API Key 检查

快速检查一批 API key 的存活状态和等级。

## 步骤

### 判断输入来源

用户可能提供：
1. 直接粘贴的 key 文本
2. 文件路径
3. 要求启动 Web UI

### CLI 检查

```bash
# 从文件检查（测等级）
uv run final-checker keys.txt --mode grade

# all_combos.json 聚合凭证包（自动展开、去重；推荐先测活）
uv run final-checker all_combos.json --mode health --concurrency 20

# 从 stdin
echo "sk-xxx" | uv run final-checker - --mode health

# 指定并发
uv run final-checker keys.txt --mode grade --concurrency 10

# 省钱模式（不做突发压测）
uv run final-checker keys.txt --mode grade --no-full-load
```

### Web UI 检查

```bash
uv run uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000
```

### 支持的格式

| 供应商 | 格式 |
|--------|------|
| Gemini | `AIza...` |
| OpenAI | `sk-...` / `sk-proj-...` |
| Anthropic | `sk-ant-...` |
| GCP | `{"type":"service_account",...}` JSON 块 |
| Azure | `URL\|KEY` 或分行粘贴 URL + key |
| AWS Bedrock | `AKIA...:SECRET` 或 `AWS_ACCESS_KEY_ID=...` 格式 |
| 聚合凭证包 | `all_combos.json`：`aws_iam_pairs` / `azure_openai_pairs` / `gcp_service_accounts` |

聚合包会稳定去重；当前 Bedrock 插件只支持 `AKIA`，所有 `ASIA` 临时凭证会安全跳过。真实凭证文件禁止加入 Git；只提交代码、测试和 skill。

### 模式说明

- **health** — 最便宜的一次调用，只判断 key 能不能用
- **grade** — 完整探测等级、限额、模型列表（会产生少量 API 费用）

## 输入

$ARGUMENTS - key 文本、文件路径、或 "web" 启动 Web UI
