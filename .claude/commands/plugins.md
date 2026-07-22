---
description: "List registered plugins and their detection patterns"
---

# /plugins — 查看已注册的检查插件

列出当前所有自动发现的检查插件及其凭证匹配模式。

## 步骤

运行以下命令查看已注册插件：

```bash
uv run python -c "
from app.plugins.registry import discover_plugins
plugins = discover_plugins()
print(f'已注册 {len(plugins)} 个插件:\n')
for p in plugins:
    print(f'  {p.name}')
"
```

然后读取每个插件的 `matches()` 方法和模块 docstring，汇总成表格展示给用户。

关键文件：
- `app/plugins/registry.py` — 自动发现逻辑
- `app/plugins/base.py` — CheckerPlugin 基类
- `app/plugins/*.py` — 各插件实现
