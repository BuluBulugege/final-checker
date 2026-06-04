# final-checker · 插拔式 API Key 检查器

一行一个 API key 丢进来，插件自动识别是 **Gemini / OpenAI / Anthropic** 哪一家，
自动调用对应检查。两种模式：**测活**（能不能调用）和 **测等级**（探测限额/等级）。
FastAPI 后端 + Neo-Brutalism Web UI，异步任务、实时进度、可配置并发。

## 快速开始

```bash
uv venv && uv pip install -e ".[dev]"

# Web UI + REST API
uv run uvicorn app.main:app --reload
# 打开 http://127.0.0.1:8000

# 或者用 CLI 批量跑
uv run final-checker keys.txt --mode grade --concurrency 5
cat keys.txt | uv run final-checker - --mode health
```

## 两种模式

| 模式 | 名称 | 做什么 |
|------|------|--------|
| `health` | 测活 | 用最便宜的一次调用证明 key 能用 |
| `grade`  | 测等级 | 完整探测等级/限额（同时也证明了存活） |

## 等级判定逻辑（严格按需求）

### Gemini
- **pro 模型**（`gemini-3.1-pro`）：50 req/s 压 30s，看第一次 429 的时刻。
  - 30s 内不返回 429 → `t3`
  - 15s 内返回 429 → `t1`
  - 15–30s 之间 → `t2`
- **图像模型**（`gemini-3.1-flash-image`）：50 张/s 压 30s，同样的分桶。
- 最终标签组合：`t{pro}-{image}`，例如 `t3-3`（两项都通过）、`t3-1`（pro 是 t3、图像是 t1）。
- Gemini 没有限额响应头，所以等级完全靠 429 时刻经验推断（限额是按项目算的，不是按 key）。

### OpenAI
- **等级 T1–T5**：从 `x-ratelimit-limit-tokens`（TPM）响应头对照文档阈值推断。
- **图像 rpm**：`gpt-image-2` 以 30 req/s 压测，最多发 40 次，遇到第一个 429 停下，
  得出最大 rpm `r`。标签形如 `T5-r100`（T5 密钥、image2 约 100 rpm；`T5-r0` 表示发了 0 次）。
- `r` 归类（存在 `details.image_rpm_bucket`）：`0→0`、`(0,25]→1`、`(25,100]→2`、`(100,200]→3`、`(200,300]→4`、`>300→5`。
- **组织/个人**：调 `/v1/me` 取组织名写进备注（例如「组织: Acme (org-xxxx)」或「个人账户」），
  并标注 key 类型（project / service account / user）。
- 注意区分 429 的两种含义：`rate_limit_exceeded`（真限流）vs `insufficient_quota`（账单问题）。

### Anthropic
- 枚举 `GET /v1/models` 拿到**所有模型**，逐个用 `max_tokens:1` 的便宜调用读
  `anthropic-ratelimit-*-limit` 响应头（RPM / ITPM / OTPM，200 和 429 都带）。
- 按 RPM 对照官方等级：`50→T1`、`1000→T2`、`2000→T3`、`4000→T4`。
- **企业级（企业级）判定**：RPM 不在标准值、出现 `anthropic-priority-*` 头、或限额超过 T4 上限
  → 直接标 `企业级`。备注里列出每个模型的 RPM/ITPM/OTPM。
- Anthropic 没有图像模型，也没有 TPD 响应头（日限是美元额度，不是速率头）。

## 架构（插拔式）

```
app/
  models.py        共享数据模型（KeyResult / ProbeOutcome / BurstResult …）
  config.py        所有速率/时长/模型ID/阈值（可被 .env 覆盖，FC_ 前缀）
  ratetest.py      复用的突发压测器：N req/s 压 D 秒，记录首个 429 时刻
  http_util.py     httpx 客户端 + 响应解析助手
  jobs.py          异步任务管理：信号量控制并发，SSE 进度广播
  main.py          FastAPI 路由 + SSE + 静态 UI
  cli.py           命令行批量入口
  plugins/
    base.py        插件契约：matches / health_check / grade_check
    registry.py    自动发现：放一个含 PLUGIN 实例的模块即自动注册
    gemini.py  openai.py  anthropic.py
  static/          Neo-Brutalism Web UI
```

**加一个第四家供应商**：在 `app/plugins/` 放一个新模块，实现 `CheckerPlugin`，
模块末尾写 `PLUGIN = YourPlugin()` 即可，registry 会自动发现并接入路由。

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/jobs` | `{keys, mode, concurrency, full_load}` → 立即返回 `job_id` |
| `GET`  | `/api/jobs/{id}` | 当前完整快照（轮询用） |
| `GET`  | `/api/jobs/{id}/stream` | SSE 实时进度（snapshot / key / job / done 事件） |
| `POST` | `/api/jobs/{id}/cancel` | 取消任务 |
| `GET`  | `/api/plugins` | 已注册插件 |
| `GET`  | `/api/config` | 当前可调参数（只读） |

## 安全与成本

- 密钥只在内存中处理，**不落盘**；返回给客户端的 key 一律脱敏（`sk-proj-…1234`）。
- `full_load`（全速压测）会**真实**按 spec 速率打官方 API，会产生费用并可能触发风控。
  关掉它会用温和的 `gentle_*` 速率省钱。所有速率都在 `config.py` 里可调。
- `.gitignore` 已屏蔽 `keys*.txt`、`.env`、结果导出等，避免误提交密钥。

## 测试

```bash
uv run pytest -q     # 全程 mock 网络（respx），不打真实 API
```
