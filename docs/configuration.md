# 配置说明

当前 AutoTeam 只支持 `swap_seat-only` 主流程。配置重点是 CPA、CFMail、多 Team、白名单、管理员 session 和自动巡检。

## 必填与推荐配置

| 配置项 | 必填 | 说明 |
|---|---:|---|
| `API_KEY` | 推荐 | WebUI / API 鉴权；首次启动可自动生成 |
| `CPA_URL` | 是 | CPA / CLIProxyAPI 地址 |
| `CPA_KEY` | 是 | CPA 管理密钥 |
| `MAIL_PROVIDER` | pending 替换时需要 | 推荐 `cloudflare_temp_email` |
| `CF_TEMP_EMAIL_BASE_URL` | pending 替换时需要 | Cloudflare Temp Email API 根地址 |
| `CF_TEMP_EMAIL_ADMIN_PASSWORD` | pending 替换时需要 | Cloudflare Temp Email 管理密码 |
| `CF_TEMP_EMAIL_DOMAIN` | pending 替换时需要 | 邮箱域名，支持多域名与随机子域名 |
| `TEAM_WORKSPACES_JSON` | 多 Team 时需要 | 多 Team 配置 JSON |
| `SWAP_SEAT_WHITELIST_EMAILS` | 否 | 白名单，不查 quota、不切 seat、不启停 CPA OAuth |
| `AUTO_CHECK_INTERVAL` | 否 | 自动巡检间隔，秒 |
| `AUTO_CHECK_TARGET_SEATS` | 否 | 默认 ChatGPT/OAuth active 保留数，`1~5` |
| `AUTO_CHECK_REPLACE_WITH_PENDING_INVITE` | 否 | Team 全员 quota 耗尽时是否消费 pending invite |
| `SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS` | 否 | 可用 quota 的最短重复检查间隔，默认 `900` |

## CPA

CPA 是 OAuth/auth 真相源。AutoTeam 不再把本地 auth 当成主状态，也不再把本地 OAuth 主动同步回 CPA。

```dotenv
CPA_URL=http://127.0.0.1:8317
CPA_KEY=your_cpa_key
```

AutoTeam 会调用 CPA 完成：

- list auth-files
- check Codex quota
- set auth disabled / active

手动 API 只允许 disable；enable 只能由 swap_seat 自动决策。

## Cloudflare Temp Email / CFMail

```dotenv
MAIL_PROVIDER=cloudflare_temp_email
CF_TEMP_EMAIL_BASE_URL=https://tempmail.example.com
CF_TEMP_EMAIL_ADMIN_PASSWORD=your_admin_password
CF_TEMP_EMAIL_DOMAIN={random}.a.com;{random}.b.com
```

`CF_TEMP_EMAIL_DOMAIN` 支持：

| 写法 | 行为 |
|---|---|
| `a.com` | 固定域名 |
| `*.a.com` | 随机子域名 |
| `{random}.a.com` | 随机子域名 |
| `xxxxx.a.com` | 连续 `x` 作为随机子域名占位 |
| `xxxxx.a.com;xxxxx.b.com` | 多域名随机选择 |

CloudMail 配置仍保留兼容，但 pending invite 注册主路径推荐 Cloudflare Temp Email。

## 多邮箱服务

WebUI 支持配置多个邮箱服务，并写入：

```dotenv
MAIL_SERVICES_JSON=[...]
MAIL_SERVICE_DEFAULT=cf-1
```

pending invite 注册时会优先按邮箱域名匹配服务，匹配不到时使用默认服务。

## 多 Team

`TEAM_WORKSPACES_JSON` 可以是数组，也可以是包含 `teams` / `workspaces` / `items` 的对象。

```json
[
  {
    "id": "team-a",
    "account_id": "uuid-a",
    "workspace_name": "Team A",
    "enabled": true,
    "max_chatgpt_active": 2,
    "pending_invite_email": "pending-a@example.com"
  }
]
```

字段：

| 字段 | 说明 |
|---|---|
| `account_id` | Team/workspace ID，必填 |
| `id` | 配置 ID，选填 |
| `workspace_name` | 展示名称，选填 |
| `enabled` | 是否参与调度，默认 `true` |
| `max_chatgpt_active` | 当前 Team active 保留数，`1~5` |
| `pending_invite_email` | 指定 pending invite 邮箱，选填 |
| `email` | 独立管理员邮箱，选填 |
| `session_token` | 独立 session，选填；留空共享默认管理员 session |

## 白名单

```dotenv
SWAP_SEAT_WHITELIST_EMAILS=owner@example.com;keep@example.com
```

分隔符支持逗号、分号、空格、换行。

白名单成员会完全跳过 swap_seat 管理，但其现有 ChatGPT seat / active OAuth 会占用 active 容量。

## 自动巡检

```dotenv
AUTO_CHECK_INTERVAL=300
AUTO_CHECK_TARGET_SEATS=2
AUTO_CHECK_REPLACE_WITH_PENDING_INVITE=true
```

行为：

- 单 Team：按当前 Team 执行 swap 或自动检测替换。
- 多 Team：逐个 enabled Team 执行调度。
- 只有 `AUTO_CHECK_REPLACE_WITH_PENDING_INVITE=true` 且 Team 全员 quota 耗尽时，才消费 pending invite。

## 冷却与 quota cache

冷却文件：

```text
swap_seat_cooldown.json
```

quota cache 文件：

```text
swap_seat_quota_state.json
```

规则：

- 每个 Team 每天最多 3 次有副作用 swap。
- 每次有副作用 swap 至少间隔 2 小时。
- quota cache 按 `account_id + auth_id` 隔离。
- 新账号无记录时先检查 quota。
- 未到 reset 时间的 exhausted 账号不会被重新检查，也不会被 swap 上来。

## 管理员 session

管理员 session 只用于 Team API，不是 OAuth 真相源。

WebUI 路径：

```text
配置面板 → 管理员 / 主号
```

也可以 CLI：

```bash
uv run autoteam admin-login --email admin@example.com
```

## 归档兼容项

以下配置可能仍存在，但不属于 swap_seat 主流程：

- `SYNC_TARGET_SUB2API`
- `SUB2API_*`
- 旧本地账号池同步字段
- 旧 OAuth 手动导入字段

WebUI 会把这些字段放在归档兼容区域，避免影响 swap_seat 主流程。
