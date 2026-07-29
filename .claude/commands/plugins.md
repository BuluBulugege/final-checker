---
description: "List registered plugins and their detection patterns"
---

# /plugins — 查看已注册的检查插件

列出当前所有自动发现的检查插件及其凭证匹配模式。

## 步骤

运行以下命令查看已注册插件（`all_plugins()` 返回**已启用**的插件，按
`(meta.priority, meta.name)` 排序 — 即 dispatch 的实际匹配顺序）：

```bash
uv run python -c "
from app.plugins.registry import all_plugins
plugins = all_plugins()
print(f'已启用 {len(plugins)} 个插件（按 dispatch 优先级排序）:\n')
for p in plugins:
    m = p.meta
    print(f'  [{m.priority:>3}] {m.name} v{m.version} — {m.description}')
    print(f'        格式: {m.key_format_hint}')
    print(f'        能力: {\", \".join(m.capabilities)} | enabled={m.enabled}')
"
```

然后读取每个插件的 `matches()` 方法和模块 docstring，汇总成表格展示给用户。

## 行为要点（与实现一致）

- **自动发现**：`app/plugins/` 下任何暴露模块级 `PLUGIN`（`CheckerPlugin` 实例）
  的模块在首次调用 `all_plugins()`/`dispatch()` 时经 `pkgutil.iter_modules`
  自动注册，无需手动登记；重名插件会在 `register()` 时直接 raise。
- **dispatch 顺序**：`dispatch(key)` 按 `all_plugins()` 的顺序逐个尝试
  `matches()`，先中先得。当前优先级：anthropic 10 → aws_bedrock 20 →
  azure 30 → gcp 40 → gemini 50 → openai 90。openai 必须最后：它宽松的
  `sk-` 匹配器否则会吞掉 Anthropic 的 `sk-ant-…` key。
- **enabled 过滤**：`meta.enabled = False` 的插件保持注册但不参与
  `all_plugins()`/`dispatch()`。
- **PluginMeta 字段**：`name` / `version` / `description` /
  `key_format_hint`（拼“不支持的 key”报错用）/ `capabilities`
  （`["health", "grade"]`）/ `priority` / `enabled`。
- **可选 hook**：`mask()`（自定义脱敏显示）、`stitch()`（多行凭证拼接）、
  `extract_candidates()`（all_combos 聚合包展开）——核心代码只通过这些
  hook 了解插件，新增插件不需要改核心。

关键文件：
- `app/plugins/registry.py` — 自动发现与 dispatch（`all_plugins()`、`dispatch()`、`register()`）
- `app/plugins/base.py` — `CheckerPlugin` 基类、`PluginMeta`、可选 hook 约定
- `app/plugins/*.py` — 各插件实现
