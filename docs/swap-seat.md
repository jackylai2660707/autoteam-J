# swap_seat 使用手册

本文档描述当前 AutoTeam 的默认主流程：`swap_seat`。旧的注册池、kick/remove、OAuth 本地导入、同步中心都已归档或禁用；新增 invite 只保留为显式 `invite-add` 模式。

## 目标

对每个受管 Team 保证：

1. Team 内始终优先保留有 Codex quota 的成员。
2. ChatGPT seat / CPA OAuth active 数量可配置为 `1~5`，默认 `2`。
3. 母号 / admin 默认保持 Codex seat。
4. 其他成员全部 Codex seat，CPA OAuth disabled standby。
5. 如果 swap 后有效 GPT seat 低于目标，才按补位模式注册新号。
6. 全程不 kick、不 remove、不 cancel invite；默认 pending 模式不创建新 invite。

## 安全边界

### 允许的副作用

- `PATCH /backend-api/accounts/{account_id}/users/{user_id}` 修改 `seat_type`
- CPA API 禁用或启用 OAuth/auth-file
- Cloudflare Temp Email 读取已有 pending invite 邮件并完成注册
- 显式 `invite-add` 模式创建随机 CFMail 地址并发送一个新的 Team invite
- 本地记录 quota cache、冷却、任务日志

### 禁止的副作用

- `DELETE /users/*`
- `DELETE /invites/*`
- 默认/旧路径 `POST /invites`（仅 `invite-add` 或 `AUTO_CHECK_REPLACE_MODE=create_invite` 的受控入口可创建）
- `PATCH /invites/*`
- Team member kick/remove
- pending invite cancel
- 非显式配置模式创建新 Team invite
- 手动 enable CPA OAuth（enable 只能由 swap_seat 根据 quota 自动选择）

后端在 `ChatGPTTeamAPI` 里有安全闸；即使旧代码路径误调用上述接口，也会抛错中止。

## 调度流程

单个 Team 的一次 `swap_seat`：

1. 读取 Team 成员列表。
2. 从 CPA 读取 Codex OAuth/auth-files。
3. 按邮箱匹配 Team member 与 CPA auth。
4. 白名单成员跳过，不查 quota、不切 seat、不启停 CPA OAuth。
5. 对 Team 内非白名单成员检查 CPA quota：
   - `primary_pct` 对应 5h window。
   - `weekly_pct` 对应 weekly window。
   - 任一窗口达到 100% 都不可用。
6. 记录 quota 快照与 reset 时间到 `swap_seat_quota_state.json`。
7. 选出最多 `max_chatgpt_active` 个同时具备 5h + weekly quota 的成员。
8. 先把未选中成员切 Codex，再把选中成员切 ChatGPT。
9. 先 disable 未选中 CPA OAuth，再 enable 选中 CPA OAuth。
10. 如果没有任何 quota 可用，则不做 seat/OAuth 修改，返回 `no_quota_available`。

## quota cache 与 reset 时间

AutoTeam 会记录每个 `account_id + auth_id` 的 quota 状态。这样同一个 OAuth 在不同 Team 下不会互相污染。

缓存规则：

- 新账号没有记录：必须实时检查一次 CPA quota。
- 最近检查过且可用：短时间内复用缓存，避免频繁查询 CPA。
- 已耗尽且 reset 时间未到：直接视为 exhausted，不做无用检查，也不会 swap 到这个账号。
- reset 时间已过：下次 swap 会重新检查。

默认最短重复检查间隔：

```dotenv
SWAP_QUOTA_CHECK_MIN_INTERVAL_SECONDS=900
```

## swap 冷却

每个 Team 独立冷却，scope 为 `account_id`：

- 每天最多 3 次产生 seat/OAuth 副作用的 swap。
- 每次 swap 至少间隔 2 小时。
- 如果本轮只是读取状态、无 quota 可用、或没有变化，不消耗冷却次数。

冷却状态保存在：

```text
swap_seat_cooldown.json
```

## 多 Team 配置

使用 `TEAM_WORKSPACES_JSON` 配置多个 Team。留空时回退当前管理员 session 的默认 Team。

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
    "pending_invite_email": "pending-b@example.com",
    "session_token": "optional-team-session-token"
  }
]
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `account_id` | 是 | Team/workspace ID，也是 quota cache 和冷却隔离主键 |
| `id` | 否 | 配置内可读 ID；默认使用 `account_id` |
| `workspace_name` | 否 | WebUI 展示名称 |
| `enabled` | 否 | `false` 时只展示，不参与自动调度 |
| `max_chatgpt_active` | 否 | 该 Team 保留 ChatGPT/OAuth active 数，范围 `1~5` |
| `pending_invite_email` | 否 | 指定该 Team 低于 GPT seat 目标后优先消费的 pending invite 邮箱 |
| `email` | 否 | 独立管理员邮箱；默认继承当前管理员 |
| `session_token` | 否 | 独立 session；留空时共享当前管理员 session |

## pending invite 替换

默认自动替换不创建 invite。你可以先在 Team 中准备 pending invite，后续系统只消费已有 pending invite 中的 CFMail 邮箱；如需自动创建新 invite，显式设置 `AUTO_CHECK_REPLACE_MODE=create_invite`。

自动替换触发条件：

1. 当前 Team 已执行一次 swap 检查。
2. swap 后有效 GPT seat 少于目标保留数（自管可用 GPT、白名单 GPT、受保护 GPT 都会计入）。
3. 当前操作开启了 `replace_with_pending_invite`。
4. pending 模式能从 Team pending invite 列表中找到可用 CFMail 邮箱；create 模式能创建随机 CFMail 地址并发送 invite。
5. 注册前只读检查确认当前仍未达到 GPT active 目标。

注册成功后：

- 新号切 ChatGPT seat。
- 新号 CPA OAuth 设为 active。
- 旧号保持 Codex + CPA disabled standby。

## CFMail / Cloudflare Temp Email

主路径推荐使用 Cloudflare Temp Email：

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
| `*.a.com` | 启用随机子域名 |
| `{random}.a.com` | 启用随机子域名 |
| `xxxxx.a.com` | 连续 `x` 视为随机子域名前缀占位 |
| `xxxxx.a.com;xxxxx.b.com` | 多域名随机选择 |

## 白名单

```dotenv
SWAP_SEAT_WHITELIST_EMAILS=owner@example.com;keep@example.com
```

白名单成员：

- 不检查 quota。
- 不切 seat。
- 不启停 CPA OAuth。
- 仍会计入当前已有 ChatGPT seat / active OAuth 数，可能减少可分配给其他成员的 active 容量。

## WebUI 操作

### 总览

只读展示：

- Team enabled/total
- quota cache available/exhausted/stale
- CPA OAuth active/standby
- 每个 Team 的 cooldown

### Seat 调度

可执行：

- 单 Team `swap_seat`
- 单 Team 自动检测并替换
- 多 Team 自动调度
- 手动 disable CPA OAuth

不可执行：

- 手动 enable CPA OAuth
- kick/remove member
- cancel invite
- create invite

### Team 成员

用于查看：

- 已加入成员
- pending invite
- seat type
- CPA OAuth 状态
- quota cache

pending invite 行提供“消费此 invite 替换”按钮，但不会取消 invite，也不会创建新 invite。Seat 调度页的“新增 invite 注册”按钮会显式创建一个随机 CFMail invite。

## CLI 命令

```bash
# 单 Team swap，保留 2 个 ChatGPT/OAuth active
uv run autoteam swap-seats 2

# 先 swap；如果 GPT seat 低于目标，则消费 pending invite
uv run autoteam auto-detect-replace 2

# 先 swap；如果 GPT seat 低于目标，则创建新 CFMail invite
uv run autoteam auto-detect-replace 2 --replace-mode create_invite

# 指定 pending invite 邮箱
uv run autoteam auto-detect-replace 2 --email pending@example.com

# 多 Team 调度；低于目标时消费各自 pending invite
uv run autoteam manage-teams 2

# 多 Team 调度；低于目标时自动创建新 CFMail invite
uv run autoteam manage-teams 2 --replace-mode create_invite

# 多 Team 只做 swap，不消费 pending invite
uv run autoteam manage-teams 2 --no-replace

# 启动 WebUI / API
uv run autoteam api --host 0.0.0.0 --port 8787
```

## API 入口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/teams` | 读取受管 Team 配置 |
| `GET` | `/api/team/members?account_id=...` | 读取目标 Team 成员与 pending invite |
| `GET` | `/api/swap/runtime-status` | 读取 cooldown + quota cache，只读无副作用 |
| `POST` | `/api/tasks/swap-seats` | 单 Team swap |
| `POST` | `/api/tasks/auto-detect-replace` | 单 Team 自动检测并替换 |
| `POST` | `/api/tasks/manage-teams` | 多 Team 调度 |
| `POST` | `/api/tasks/add` | 手动消费已有 pending invite |
| `POST` | `/api/tasks/invite-add` | 显式创建随机 CFMail invite 并注册上传 PAT |
| `PATCH` | `/api/cpa/auth/status` | 仅允许手动 disable CPA OAuth |

## 验证建议

提交或上线前建议运行：

```bash
cd web
npm run build
```

```bash
PYTHONPATH=src python -m pytest \
  tests/unit/test_api_swap_only_disabled.py \
  tests/unit/test_team_context.py \
  tests/unit/test_api_team_members.py \
  tests/unit/test_manager_emergency_invite.py \
  tests/unit/test_swap_seat.py \
  tests/unit/test_cpa_sync.py \
  tests/unit/test_chatgpt_transport.py \
  tests/unit/test_mail_provider.py \
  tests/unit/test_cloudflare_temp_email.py -q
```

安全扫描：

```bash
rg -n "removeTeamMember|delete_invite|cancel.*invite|kick_member|remove_member|DELETE.*users|PATCH.*/invites|POST.*invites|invite_member\(" src/autoteam web/src tests/unit -S --glob "!src/autoteam/web/dist/**"
```

预期只应看到禁用文案、安全闸或测试断言。
