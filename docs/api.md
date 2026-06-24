# API 文档

当前 API 围绕 `swap_seat-only` 主流程。旧账号池、邀请创建、kick/remove、同步中心等入口已禁用或只保留兼容返回。

默认前缀：

```text
/api
```

如配置了 `API_KEY`，请求需要：

```http
Authorization: Bearer <API_KEY>
```

## 状态与配置

### GET `/api/auth/check`

检查 API Key 是否有效。

### GET `/api/admin/status`

返回管理员 session/workspace 状态。

### GET `/api/config/runtime`

读取当前运行配置字段。

### PUT `/api/config/runtime`

保存运行配置。常用字段：

- `CPA_URL`
- `CPA_KEY`
- `MAIL_PROVIDER`
- `CF_TEMP_EMAIL_*`
- `TEAM_WORKSPACES_JSON`
- `SWAP_SEAT_WHITELIST_EMAILS`

### GET `/api/config/auto-check`

读取自动巡检配置。

### PUT `/api/config/auto-check`

示例：

```json
{
  "interval": 300,
  "target_seats": 2,
  "replace_with_pending_invite": true
}
```

## Team 与运行快照

### GET `/api/teams`

只读返回受管 Team 配置，不触发外部 API。

```json
{
  "teams": [
    {
      "id": "team-a",
      "account_id": "uuid-a",
      "workspace_name": "Team A",
      "enabled": true,
      "max_chatgpt_active": 2,
      "pending_invite_email": "pending-a@example.com",
      "session_present": false,
      "label": "Team A"
    }
  ],
  "total": 1
}
```

### GET `/api/team/members?account_id=<id>`

读取目标 Team 成员与 pending invite。只读，不会 kick/remove/cancel。

### GET `/api/swap/runtime-status`

只读返回：

- 每个 Team 的 cooldown
- quota cache summary
- quota cache entries

不会触发 CPA quota 检查或 Team API 写操作。

## CPA

### GET `/api/cpa/files`

读取 CPA auth-files。

### PATCH `/api/cpa/auth/status`

只允许手动 disable CPA OAuth。

请求：

```json
{
  "name": "codex-user.json",
  "disabled": true
}
```

如果传 `disabled: false`，API 会返回 `410`。enable 必须由 `swap_seat` 自动选择。

## 任务

所有任务接口返回后台任务对象，可通过 `/api/tasks` 或 `/api/tasks/{task_id}` 查看结果。

### POST `/api/tasks/swap-seats`

单 Team seat/OAuth 收敛。

```json
{
  "max_chatgpt_active": 2,
  "account_id": "uuid-a"
}
```

`max_chatgpt_active` 会被限制在 `1~5`。

### POST `/api/tasks/auto-detect-replace`

先执行单 Team `swap_seat`。如果结果为 `no_quota_available`，再消费已有 pending invite。

```json
{
  "email": "pending-a@example.com",
  "account_id": "uuid-a"
}
```

`email` 可留空，系统会从 pending invite 列表中选择可用 CFMail 邮箱。

### POST `/api/tasks/manage-teams`

多 Team 调度。

```json
{
  "max_chatgpt_active": 2,
  "replace_with_pending_invite": true
}
```

每个 Team 会优先使用自己配置的 `max_chatgpt_active`；没有配置时使用请求里的默认值。

### POST `/api/tasks/add`

手动消费已有 pending invite。不会创建 invite。

```json
{
  "email": "pending-a@example.com",
  "account_id": "uuid-a"
}
```

## 任务查询

### GET `/api/tasks`

返回最近后台任务。

### GET `/api/tasks/{task_id}`

返回单个任务。

### POST `/api/tasks/{task_id}/cancel`

请求取消任务。任务会在安全检查点停止。

## 禁用入口

以下类型入口在 `swap_seat-only` 模式下返回 `410`：

- 账号 OAuth 登录
- 本地账号池 enable/disable
- 本地 auth 同步
- CPA 反向拉取到本地
- fill / cleanup / reset-quota
- Team member remove
- 手动任意 seat 修改
- 创建 invite
- 取消 invite
- kick/remove member

即使旧 UI 或脚本误调用，后端也不会执行危险 Team API 操作。
