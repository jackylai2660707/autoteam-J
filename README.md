<div align="center">

# AutoTeam

**swap_seat-only 的 ChatGPT Team seat / CPA OAuth 调度控制台**

AutoTeam 现在只围绕一个目标工作：读取 CPA 中的 OAuth/auth-files，检查 Team 成员的 Codex quota，并在不移除任何成员的前提下收敛 seat 与 OAuth active/disabled 状态。

[快速开始](docs/getting-started.md) · [swap_seat 手册](docs/swap-seat.md) · [配置说明](docs/configuration.md) · [API 文档](docs/api.md)

</div>

---

> **安全边界**：当前版本是 `swap_seat-only`。项目不会 kick/remove Team member，不会 cancel pending invite，不会创建新 invite。Team API 写操作只允许修改成员 `seat_type`；OAuth/auth 的启用与禁用由 CPA 管理 API 完成。

## 核心能力

| 功能 | 说明 |
|---|---|
| 🔁 Seat 调度 | 根据 CPA quota 把有额度成员切到 ChatGPT seat，把额度耗尽或未选中的成员切到 Codex seat |
| 🧠 Quota 判断 | 同时考虑 5h primary window 与 weekly window；任一窗口耗尽都视为不可用 |
| 🏢 多 Team 管理 | 通过 `TEAM_WORKSPACES_JSON` 管理多个 Team，每个 Team 独立 quota cache、冷却和 pending invite |
| ☁️ CPA 管理 | CPA 是 OAuth/auth 真相源；AutoTeam 只调用 CPA API 读取 auth、检查 quota、启停 OAuth active/disabled |
| 📧 Pending invite 消费 | 只有 Team 内现有成员 quota 全部耗尽时，才使用已有 pending invite 的 CFMail 邮箱注册新号 |
| 🧊 冷却与缓存 | 每个 Team 每天最多 swap 3 次、每次间隔至少 2 小时；quota 结果会记录 reset 时间，避免无用检查和无用 swap |
| 🛡️ 白名单 | 白名单成员不检查 quota、不切 seat、不启停 CPA OAuth |

## 明确不会做的事

- 不 kick / remove Team member
- 不 cancel pending invite
- 不创建新 invite
- 不维护旧本地 OAuth 登录流
- 不把本地 auth 同步回 CPA 作为主流程
- 不把账号池状态作为 quota 真相源

## 快速开始

```bash
uv sync
uv run playwright install chromium
uv run autoteam api
```

打开 WebUI：

```text
http://127.0.0.1:8787
```

第一次进入后按顺序配置：

1. `API_KEY`
2. 管理员 session / workspace
3. `CPA_URL` / `CPA_KEY`
4. Cloudflare Temp Email / CFMail
5. 多 Team 与白名单
6. 自动巡检策略

详见 [从零开始](docs/getting-started.md)。

## 常用命令

单 Team 手动收敛：

```bash
uv run autoteam swap-seats 2
```

多 Team 自动调度，Team 耗尽后可消费 pending invite：

```bash
uv run autoteam manage-teams 2
```

只检查并收敛，不消费 pending invite：

```bash
uv run autoteam manage-teams 2 --no-replace
```

启动 WebUI / API：

```bash
uv run autoteam api --host 0.0.0.0 --port 8787
```

## 关键配置示例

```dotenv
MAIL_PROVIDER=cloudflare_temp_email
CF_TEMP_EMAIL_BASE_URL=https://tempmail.example.com
CF_TEMP_EMAIL_ADMIN_PASSWORD=your_admin_password
CF_TEMP_EMAIL_DOMAIN={random}.a.com;{random}.b.com

CPA_URL=http://127.0.0.1:8317
CPA_KEY=your_cpa_key

AUTO_CHECK_TARGET_SEATS=2
AUTO_CHECK_REPLACE_WITH_PENDING_INVITE=true
SWAP_SEAT_WHITELIST_EMAILS=owner@example.com;keep@example.com

TEAM_WORKSPACES_JSON=[{"id":"team-a","account_id":"uuid-a","workspace_name":"Team A","max_chatgpt_active":2,"pending_invite_email":"pending-a@example.com"},{"id":"team-b","account_id":"uuid-b","workspace_name":"Team B","max_chatgpt_active":1,"pending_invite_email":"pending-b@example.com"}]
```

## WebUI 页面

| 页面 | 用途 |
|---|---|
| 总览 | 只读查看 Team、quota cache、swap 冷却、CPA OAuth active/standby |
| Seat 调度 | 执行单 Team swap、单 Team 自动检测替换、多 Team 自动调度 |
| Team 成员 | 查看成员和 pending invite；可手动消费已有 pending invite，不提供 remove/cancel |
| 配置面板 | 管理 CPA、CFMail、多 Team、白名单、管理员 session、巡检 |
| 任务历史 | 查看 swap 与 pending invite 任务结果 |
| 日志 | 查看运行日志 |

## 文档

| 文档 | 内容 |
|---|---|
| [swap_seat 手册](docs/swap-seat.md) | 调度策略、安全边界、多 Team、pending invite、冷却与缓存 |
| [从零开始](docs/getting-started.md) | 安装、配置、启动 WebUI、首次运行 |
| [配置说明](docs/configuration.md) | `.env` 与 WebUI 配置项 |
| [API 文档](docs/api.md) | 当前保留的 swap_seat API |
| [工作原理](docs/architecture.md) | 后端模块、WebUI、调度流程与安全闸 |
| [Docker 部署](docs/docker.md) | Docker Compose、数据目录、代理和更新 |
| [常见问题](docs/troubleshooting.md) | quota、pending invite、CPA、CFMail、冷却排障 |

## 数据文件

| 文件 | 说明 |
|---|---|
| `.env` | 运行配置 |
| `state.json` | 管理员 session 与当前 workspace 信息 |
| `swap_seat_quota_state.json` | quota cache、5h/weekly reset 时间 |
| `swap_seat_cooldown.json` | 每个 Team 的 swap 冷却记录 |
| `accounts.json` | 仅用于 pending invite 注册状态兼容追踪，不是 quota 真相源 |

## License

MIT
