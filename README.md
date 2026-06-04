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

### GCP（Service Account）
- 输入是**整块 service-account JSON**（`{"type":"service_account", ...}`）；粘进文本框即可，
  解析器自动把 JSON 块当成一条凭证识别（其余行仍按「每行一个」）。
- 用私钥离线签 RS256 JWT，向 `oauth2.googleapis.com/token` 换 access token（官方 `google-auth` 签名 + httpx 异步换取，不引入 `requests`）。`invalid_grant` → 密钥失效。
- GCP 不分等级（标签固定 `GCP`），所有信息进备注 + **可下载的完整 JSON 报告**：
  - **项目概览**：元数据、计费状态、已启用 API 数、SA 实际权限（`testIamPermissions`）
  - **服务器**：Compute 实例跨所有 zone 聚合（运行 / 总计 / 各区）
  - **数据库**：Cloud SQL / AlloyDB / Spanner / Firestore 各自数量
  - **Vertex 模型**：扫所有 region 的 publisher + 调优模型；RPM/TPM 优先读 Cloud Quotas 配额，拿不到则留模型清单
- 备注塞不下的完整信息 → 行内有「⬇ 下载完整信息」按钮，点击下载该 key 的 `gcp-<project>.json`
  （报告通过 `GET /api/jobs/{id}/download/{index}` 单独提供，不塞进 SSE 流）。

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
    gemini.py  openai.py  anthropic.py  gcp.py
  parsing.py       输入解析：JSON 块（GCP）当一条，其余按行
  redact.py        密钥脱敏：从任何错误/详情字符串里抹掉 key 形状的串
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

## 长期监控系统 🆕

**自动健康检查**：将测试通过的密钥添加到长期监控，系统每 10 分钟自动探活一次。

### 特性

- ✅ **智能添加**：只允许 alive 的密钥加入监控（添加时自动健康检查）
- ✅ **自动探活**：后台调度器每 10 分钟检查一次所有密钥
- ✅ **死亡重试**：密钥失败后会在 24h、36h、48h 自动重试，仍失败则标记为 abandoned
- ✅ **去重机制**：SHA256 hash 防止重复，重复密钥会更新状态
- ✅ **管理界面**：Neo-Brutalism 风格的 Web 管理面板
- ✅ **筛选搜索**：按平台、状态、关键词筛选密钥

### 快速开始

1. **配置密码**（可选）：
```bash
cp .env.example .env
# 编辑 .env 设置 ADMIN_PASSWORD 和 JWT_SECRET
```

2. **访问管理界面**：
```
http://127.0.0.1:8000/admin
```

3. **添加密钥到长期监控**：
   - 从短期测试结果批量/选择移入
   - 或通过 API 直接添加（会先健康检查）

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/long-term/auth` | 管理员登录（返回 JWT token） |
| `GET`  | `/api/long-term/keys` | 列出密钥（支持筛选、分页） |
| `POST` | `/api/long-term/keys` | 添加密钥（自动健康检查） |
| `POST` | `/api/long-term/keys/move` | 从短期结果移入 |
| `POST` | `/api/long-term/keys/{id}/check` | 手动探活单个密钥 |
| `DELETE` | `/api/long-term/keys/{id}` | 删除密钥 |
| `POST` | `/api/long-term/check-duplicate` | 检查密钥是否重复 |

### 环境变量

```bash
ADMIN_PASSWORD=your-secure-password     # 管理员密码（默认: change-me-in-production）
JWT_SECRET=your-jwt-secret              # JWT 签名密钥
DB_PATH=./data.db                       # 数据库路径（可选）
```

### 死亡重试策略

1. 密钥首次失败 → 标记为 `dead`，记录死亡时间
2. 死亡后 **24 小时** → 第 1 次重试
3. 死亡后 **36 小时** → 第 2 次重试
4. 死亡后 **48 小时** → 第 3 次重试
5. 仍失败 → 标记为 `abandoned`，停止监控

### 安全说明

- 管理界面需要 JWT 认证
- 密钥数据加密存储（SHA256 hash）
- 默认密码 **必须** 在生产环境修改
- 建议使用 HTTPS + 反向代理部署
