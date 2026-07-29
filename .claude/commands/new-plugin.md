---
description: "Add a new provider checker plugin to final-checker"
---

# /new-plugin — 创建新的 API 检查插件

用户会指定一个新的 API 供应商名称（如 "vertex", "together", "fireworks"），你需要生成对应的 checker 插件。

## 步骤

### 1. 创建插件文件

在 `app/plugins/` 下创建 `{provider_name}.py`，实现 `CheckerPlugin` 基类并声明完整的 `PluginMeta`：

```python
from app.plugins.base import CheckContext, CheckerPlugin, PluginMeta
from app.models import KeyResult, KeyStatus

class MyPlugin(CheckerPlugin):
    meta = PluginMeta(
        name="my_provider",          # 插件名只来自 meta，不要再写 name = "..." 类属性
        version="1.0.0",
        description="一句话描述这个插件做什么",
        key_format_hint="my_provider key 格式说明（用于“不支持的 key”报错）",
        capabilities=["health", "grade"],
        priority=60,                 # 见下方“优先级”说明
    )

    def matches(self, key: str) -> bool:
        """纯离线模式匹配：前缀/格式/正则"""

    async def health_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """最便宜的一次调用证明 key 能用"""

    async def grade_check(self, key: str, result: KeyResult, ctx: CheckContext) -> None:
        """完整探测：模型列表、TPM/RPM、等级判定"""

PLUGIN = MyPlugin()  # 必须！registry 靠这个自动发现
```

`tests/test_plugin_arch.py::test_all_plugins_declare_complete_meta` 会强制
每个插件的 meta 字段齐全（version/description/key_format_hint 非空、
capabilities 为 `["health", "grade"]`），meta 不全套件会红。

### 2. 优先级（dispatch 顺序）

`dispatch()` 按 `(meta.priority, meta.name)` 升序逐个尝试 `matches()`，先中先得。
当前顺序：anthropic 10 → aws_bedrock 20 → azure 30 → gcp 40 → gemini 50 →
openai 90。**openai 必须保持最后**：它宽松的 `sk-` 匹配器会吞掉别的
`sk-` 前缀 key。新插件按匹配器的“特异性”插队：前缀越宽松、数字要越大；
与其它插件前缀有重叠时，务必加一条 dispatch 测试证明 routing 正确。

### 3. 关键约定

- **matches()** 必须是纯离线的快速检查（正则/前缀），不能发网络请求
- **health_check()** 用 `result.status = KeyStatus.ALIVE / DEAD` 报告结果
- **grade_check()** 用 `result.tier`, `result.remarks`, `result.details` 报告等级
- 通过 `await ctx.progress(0.0~1.0, "标签")` 报告进度
- HTTP 请求用 `from app.http_util import timed_request`，不要自己创建 client
- 密钥脱敏用 `from app.redact import redact`
- 错误不要 raise，写入 `result.error` 和 `result.status = KeyStatus.ERROR`
- 大报告写入 `result.download_text` + `result.download_filename`

### 4. 可选 hook —— 不要改核心代码

多行凭证拼接、自定义脱敏、聚合包展开都通过插件自身的 hook 实现，
**不需要也不应该**编辑 `app/parsing.py` 或 `app/plugins/base.py`：

- `stitch(lines, i) -> (credential | None, consumed) | None`
  多行格式（如 `URL 行 + key 行`、`AWS_ACCESS_KEY_ID=/AWS_SECRET_ACCESS_KEY=`
  环境变量对）拼成单行凭证；返回 `(None, set())` 可静默吞掉孤立续行。
  参考 `azure.py` / `aws_bedrock.py`。
- `mask(key) -> str | None`
  自定义脱敏显示（如 Azure 显示 host、AWS 只显示 access key id）；
  返回 None 则回退到通用前后缀掩码。
- `extract_candidates(text) -> list[str] | None`
  识别 all_combos 聚合包里的供应商数组并展开成凭证列表；返回 None 表示
  “不是我的格式”，返回空列表表示“是我的格式但没有有效行”。

### 5. 如果需要配置项

在 `app/config.py` 添加 `{Provider}Config(BaseModel)` 并加入 `Settings`，
运行时经 `ctx.settings.{provider}` 读取。

### 6. 参考现有插件

| 插件 | 特点 | 参考场景 |
|------|------|----------|
| `openai.py` | x-ratelimit 响应头读 TPM/RPM | 标准 Bearer token API |
| `anthropic.py` | 模型枚举 + 逐模型探测 | 需要枚举模型 |
| `gcp.py` | JWT 签名换 token、多区域扫描 | Service Account JSON |
| `azure.py` | 多路径探测（Responses/chat/deployment） | URL+KEY 格式、Foundry 端点 |
| `aws_bedrock.py` | SigV4 签名、跨区域、inference profile | IAM 凭证、纯 Python 签名 |

### 7. 测试

每个插件要有自己的 `tests/test_{provider}.py`（respx mock 全部 HTTP，
禁止真实网络），覆盖：matches 正/反例、stitch/extract_candidates hook、
health 的 ALIVE/DEAD/ERROR 路径、grade 的关键产物。参考
`tests/test_azure.py` / `tests/test_aws_bedrock.py` / `tests/test_gcp.py`。

```bash
# 跑新插件的测试 + 插件架构契约测试
uv run pytest tests/test_{provider}.py tests/test_plugin_arch.py -v

# 手动验证（真实 key 不要提交）
echo "YOUR_KEY_HERE" | uv run final-checker - --mode grade

# 或者启动 Web UI
uv run uvicorn app.main:app --reload
```

不需要手动注册 — `PLUGIN = MyPlugin()` 写在模块末尾，`registry.py` 会通过 `pkgutil.iter_modules` 自动发现。

## 输入

$ARGUMENTS - 供应商名称和凭证格式描述
