---
description: "Add a new provider checker plugin to final-checker"
---

# /new-plugin — 创建新的 API 检查插件

用户会指定一个新的 API 供应商名称（如 "vertex", "together", "fireworks"），你需要生成对应的 checker 插件。

## 步骤

### 1. 创建插件文件

在 `app/plugins/` 下创建 `{provider_name}.py`，实现 `CheckerPlugin` 基类：

```python
from app.plugins.base import CheckContext, CheckerPlugin
from app.models import KeyResult, KeyStatus

class MyPlugin(CheckerPlugin):
    name = "my_provider"

    def matches(self, key: str) -> bool:
        """纯离线模式匹配：前缀/格式/正则"""

    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """最便宜的一次调用证明 key 能用"""

    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """完整探测：模型列表、TPM/RPM、等级判定"""

PLUGIN = MyPlugin()  # 必须！registry 靠这个自动发现
```

### 2. 关键约定

- **matches()** 必须是纯离线的快速检查（正则/前缀），不能发网络请求
- **health_check()** 用 `result.status = KeyStatus.ALIVE / DEAD` 报告结果
- **grade_check()** 用 `result.tier`, `result.remarks`, `result.details` 报告等级
- 通过 `await ctx.progress(0.0~1.0, "标签")` 报告进度
- HTTP 请求用 `from app.http_util import timed_request`，不要自己创建 client
- 密钥脱敏用 `from app.redact import redact`
- 错误不要 raise，写入 `result.error` 和 `result.status = KeyStatus.ERROR`
- 大报告写入 `result.download_text` + `result.download_filename`

### 3. 如果需要新的凭证格式

编辑 `app/parsing.py` 的 `_preprocess_raw()` 函数，添加多行凭证拼接逻辑。
编辑 `app/plugins/base.py` 的 `mask_key()` 函数，添加脱敏显示。

### 4. 如果需要配置项

在 `app/config.py` 添加 `{Provider}Config(BaseModel)` 并加入 `Settings`。

### 5. 参考现有插件

| 插件 | 特点 | 参考场景 |
|------|------|----------|
| `openai.py` | x-ratelimit 响应头读 TPM/RPM | 标准 Bearer token API |
| `anthropic.py` | 模型枚举 + 逐模型探测 | 需要枚举模型 |
| `gcp.py` | JWT 签名换 token、多区域扫描 | Service Account JSON |
| `azure.py` | 多路径探测（Responses/chat/deployment） | URL+KEY 格式、Foundry 端点 |
| `aws_bedrock.py` | SigV4 签名、跨区域、inference profile | IAM 凭证、纯 Python 签名 |

### 6. 测试

```bash
# 写个快速测试
echo "YOUR_KEY_HERE" | uv run final-checker - --mode grade

# 或者启动 Web UI
uv run uvicorn app.main:app --reload
```

不需要手动注册 — `PLUGIN = MyPlugin()` 写在模块末尾，`registry.py` 会通过 `pkgutil.iter_modules` 自动发现。

## 输入

$ARGUMENTS - 供应商名称和凭证格式描述
