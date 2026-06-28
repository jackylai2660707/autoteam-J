# 从零开始使用 AutoTeam swap_seat

当前 AutoTeam 默认是 `swap_seat-only` 控制台。它不 kick 成员、不维护旧账号池；主流程只做 CPA quota 检查、seat 收敛、CPA OAuth active/disabled 启停，以及在 swap 后 GPT seat 低于目标时按模式补位。需要创建新 invite 时，使用显式 `invite-add` 或 `AUTO_CHECK_REPLACE_MODE=create_invite`。

## 1. 准备项

| 准备项 | 用途 |
|---|---|
| ChatGPT Team 管理员 session | 读取 Team 成员、修改 seat_type |
| CPA / CLIProxyAPI | OAuth/auth 真相源；读取 auth-files、检查 quota、启停 OAuth |
| Cloudflare Temp Email / CFMail | 读取 pending invite 邮件，完成新号注册 |
| 已存在的 pending invite | 只有 swap 后 GPT seat 低于目标时才会被消费；create 模式可自动创建新 invite |

## 2. 安装

```bash
uv sync
# 默认使用 CloakBrowser 捕获 session；只有显式设置
# BROWSER_BACKEND=playwright 时才需要安装 Playwright Chromium：
# uv run playwright install chromium
```

Linux 也可以使用：

```bash
bash setup.sh
```

## 3. 启动 WebUI

```bash
uv run autoteam api --host 0.0.0.0 --port 8787
```

浏览器打开：

```text
http://127.0.0.1:8787
```

首次启动只强制需要 `API_KEY`。其他配置可以进入 WebUI 后补齐。

## 4. 配置顺序

进入「配置面板」后建议按这个顺序配置。

### 4.1 管理员 / 主号

管理员登录态只用于 Team API：

- 读取成员和 pending invite
- 修改成员 seat type
- 识别 workspace/account_id

主号 OAuth/auth 由 CPA 管理。swap_seat 会把 admin 视为默认 Codex seat，不占用 ChatGPT active 名额，除非你把 admin 加入白名单。

### 4.2 CPA 管理

填写：

```dotenv
CPA_URL=http://127.0.0.1:8317
CPA_KEY=your_cpa_key
```

CPA 中需要已有 Codex OAuth/auth-files。AutoTeam 会通过 CPA：

- 列出 OAuth/auth-files
- 调用 quota 检查
- enable / disable OAuth active

### 4.3 CFMail / Cloudflare Temp Email

推荐主路径：

```dotenv
MAIL_PROVIDER=cloudflare_temp_email
CF_TEMP_EMAIL_BASE_URL=https://tempmail.example.com
CF_TEMP_EMAIL_ADMIN_PASSWORD=your_admin_password
CF_TEMP_EMAIL_DOMAIN={random}.a.com;{random}.b.com
```

多域名用分号、逗号或换行分隔。`xxxxx.a.com`、`*.a.com`、`{random}.a.com` 都会启用随机子域名。

### 4.4 多 Team

如果只管理当前管理员 session 的默认 Team，可以留空。

如果要同时管理多个 Team，填写 `TEAM_WORKSPACES_JSON`：

```json
[
  {
    "id": "team-a",
    "account_id": "uuid-a",
    "workspace_name": "Team A",
    "enabled": true,
    "max_chatgpt_active": 2,
    "pending_invite_email": "pending-a@example.com"
  },
  {
    "id": "team-b",
    "account_id": "uuid-b",
    "workspace_name": "Team B",
    "enabled": true,
    "max_chatgpt_active": 1,
    "pending_invite_email": "pending-b@example.com"
  }
]
```

### 4.5 安全 / 白名单

```dotenv
SWAP_SEAT_WHITELIST_EMAILS=owner@example.com;keep@example.com
```

白名单成员不会被检查 quota，也不会被切 seat 或启停 CPA OAuth。

### 4.6 巡检设置

推荐：

```dotenv
AUTO_CHECK_INTERVAL=300
AUTO_CHECK_TARGET_SEATS=2
AUTO_CHECK_REPLACE_WITH_PENDING_INVITE=true
AUTO_CHECK_REPLACE_MODE=pending_invite
```

`AUTO_CHECK_TARGET_SEATS` 是默认 ChatGPT/OAuth active 保留数，允许 `1~5`。单个 Team 的 `max_chatgpt_active` 优先级更高。`AUTO_CHECK_REPLACE_MODE=create_invite` 会在需要补位时自动创建随机 CFMail invite。

## 5. 首次运行

在 WebUI「Seat 调度」页：

1. 选择目标 Team。
2. 点「执行 swap_seat」。
3. 查看任务历史和日志。
4. 确认 quota cache、CPA OAuth active/standby、seat 状态符合预期。

CLI 等价命令：

```bash
uv run autoteam swap-seats 2
```

多 Team：

```bash
uv run autoteam manage-teams 2
```

只做 swap，不消费 pending invite：

```bash
uv run autoteam manage-teams 2 --no-replace
```

## 6. 日常使用

推荐保持 API/WebUI 运行，使用自动巡检：

- 有 quota 的成员自动保持 ChatGPT/OAuth active。
- quota 耗尽成员自动回到 Codex + disabled standby。
- 没有可用 quota 时不做无用 swap。
- 如果开启自动补位，会在 GPT seat 低于目标后按模式消费已有 pending invite 或创建新 invite。
- 如需创建新 invite，请在 Seat 调度页点击“新增 invite 注册”并确认，或运行 `uv run autoteam invite-add 2 --force-create-invite`。

## 7. 如何确认不会 kick

项目里有三层保护：

1. WebUI 不提供 remove / kick / cancel invite 按钮。
2. API 对旧 remove/kick/cleanup/invite 创建入口返回 `410`。
3. Team API 传输层禁止 DELETE users、DELETE invites、POST invites、PATCH invites。

可以运行：

```bash
rg -n "DELETE.*users|POST.*invites|PATCH.*/invites|kick|remove" src/autoteam web/src tests/unit -S --glob "!src/autoteam/web/dist/**"
```

预期只剩禁用文案、安全闸和测试。
