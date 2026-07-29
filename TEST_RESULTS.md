# 测试结果

**最近更新：2026-07-29**

## `all_combos.json` 实测

输入文件（未提交 Git）：

```text
<凭证包路径>/all_combos.json
```

源数据共 1,045 行：

| 类型 | 原始条数 | 唯一且可诊断 |
|------|---------:|-------------:|
| AWS IAM | 888 | 167 个 `AKIA` |
| Azure | 66 | 42 个合法 Azure AI HTTPS 端点 |
| GCP | 91 | 72 个 service-account 形状对象 |
| **总计** | **1,045** | **281** |

说明：

- 664 个 `ASIA` 临时凭证没有 session token；当前 Bedrock 插件仅支持 `AKIA`，因此安全跳过。
- 聚合解析器稳定去重。
- 非 Azure AI 域名、HTTP、userinfo、非默认 HTTPS 端口均不会接收或转发 Azure API key。
- GCP 聚合条目的 OAuth endpoint 固定为 `https://oauth2.googleapis.com/token`，不信任导入的 `token_uri`。

真实 CLI 命令：

```bash
uv run python -m app.cli \
  "<凭证包路径>/all_combos.json" \
  --mode health --concurrency 20
```

结果：

| Provider | Alive | Dead | Error |
|----------|------:|-----:|------:|
| AWS Bedrock | 92 | 75 | 0 |
| Azure | 15 | 0 | 27 |
| GCP | 0 | 72 | 0 |
| **总计** | **107** | **147** | **27** |

```text
107/281 keys checked OK
```

安全验证：

- CLI 输出中匹配到的原始 secret 数：**0**
- 临时日志权限：**0600**
- `all_combos*.json`、Azure/GCP/Bedrock 报告和凭证目录均在 `.gitignore` 中

> `health` 的含义是凭证/资源端点测活；AWS STS 存活不等于每个 Bedrock 模型都已授权。模型权限、TPM/RPM 使用 `grade` 模式进一步探测。

## 自动化回归

聚合解析、GCP、插件路由、Azure 安全边界、长期监控状态映射和 API 输入边界：

```text
91 passed
```

同时通过：

- `node --check app/static/app.js`
- `git diff --check`
- 对真实聚合包的解析 smoke test（281 条）

## 已知测试套件问题

完整 `pytest` 仍包含两组旧测试设计问题，不能据此宣称全套绿色：

1. `tests/test_api_client.py` 的长期监控用假 key，但生产逻辑现在要求添加前做真实 health check；未 mock 网络的新增/生命周期断言会失败。
2. `tests/test_service_e2e.py` 的 uvicorn task 与 pytest fixture 使用不同 event loop，导致 server 启动/销毁失败。

已修复生产代码中真实存在的旧 API 回归（并新增 `tests/test_long_term_monitor.py` 锁定）：

- `plugin.check()` → `plugin.health_check()`
- `KeyStatus.SUCCESS` → `KeyStatus.ALIVE / GRADED`

这些旧测试需要单独重构为 mock health-check 和同一 event-loop 的 E2E fixture。当前不再做“全部测试通过”或“100% coverage”的不实声明。
