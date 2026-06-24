# 常见问题

## swap_seat 没有执行任何切换

可能原因：

1. 没有任何成员同时具备 5h + weekly quota。
2. 当前 seat/OAuth 已经是目标状态。
3. 命中了 2 小时 / 每日 3 次冷却。
4. 成员在白名单里。
5. CPA 没有对应邮箱的 Codex OAuth/auth-file。

查看位置：

- WebUI → Seat 调度 → quota cache / cooldown
- WebUI → 任务历史
- WebUI → 日志

## 为什么 quota 明明恢复了仍不检查？

AutoTeam 会记录 exhausted 的 reset 时间。未到 reset 前不会重复检查，避免无用 CPA 调用和无用 swap。

如果确认 reset 时间已经过了但仍未检查：

- 查看 `swap_seat_quota_state.json` 中该账号的 `exhausted_until`。
- 确认 Team `account_id` 是否正确；quota cache 按 `account_id + auth_id` 隔离。

## 新账号为什么没有立刻切上来？

新账号必须满足：

1. 已加入 Team member 列表。
2. CPA 中已经出现该邮箱的 Codex OAuth/auth-file。
3. CPA quota 检查显示 5h 和 weekly 都有剩余。
4. 预切旧成员 Codex 成功，未超过 ChatGPT active 上限。

## pending invite 没有被消费

只有这些条件同时满足才会消费：

- 当前 Team 所有非白名单成员 quota 都耗尽。
- 操作开启 `replace_with_pending_invite`。
- Team pending invite 列表中有 CFMail 邮箱。
- 可以从 CFMail 读取 invite 邮件。
- 注册前旧成员能预切 Codex。

AutoTeam 不会创建新的 invite，也不会取消已有 invite。

## 手动 enable CPA OAuth 报 410

这是预期行为。手动 API 只允许 disable。enable 必须由 `swap_seat` 根据 quota 和 active 保留数自动选择，避免启用已耗尽账号或超过 active 上限。

## 多 Team 操作到了错误 Team

检查：

- `TEAM_WORKSPACES_JSON` 的 `account_id` 是否是 Team/workspace ID，不是个人邮箱。
- 如果每个 Team 需要独立管理员 session，请填写对应 `session_token`。
- WebUI 单 Team 操作必须先选择目标 Team。
- 多 Team 自动调度只处理 `enabled=true` 的 Team。

## 为什么 admin / 母号变成 Codex seat？

这是当前策略：母号/admin 默认不占 ChatGPT active 席位，保持 Codex seat。如果不希望 AutoTeam 管理某个账号，把它加入 `SWAP_SEAT_WHITELIST_EMAILS`。

## 如何确认不会 kick 成员？

运行：

```bash
rg -n "DELETE.*users|POST.*invites|PATCH.*/invites|kick|remove" src/autoteam web/src tests/unit -S --glob "!src/autoteam/web/dist/**"
```

预期只应看到：

- 禁用文案
- 安全闸
- 测试断言

真正的 Team API 写操作只允许 `PATCH /users/{id}` 修改 `seat_type`。

## WebUI 进不去

- 检查 `API_KEY`。
- 检查服务是否在运行：`uv run autoteam api` 或 `docker compose logs -f autoteam`。
- 如果反向代理，确认 `Authorization: Bearer <API_KEY>` 没被代理剥离。

## CPA 连接失败

- 确认 `CPA_URL` 从 AutoTeam 所在环境可访问。
- Docker 下如果 CPA 在宿主机，可能要用 `host.docker.internal` 或宿主机网关 IP。
- 确认 `CPA_KEY` 与 CPA 管理密钥一致。

## Cloudflare Temp Email 连接失败

- `CF_TEMP_EMAIL_BASE_URL` 要填 API 根地址，不是前端页面。
- `CF_TEMP_EMAIL_ADMIN_PASSWORD` 要与服务端管理密码一致。
- `CF_TEMP_EMAIL_DOMAIN` 要是该服务可创建/接收邮件的域名。

## 什么时候会消耗 swap 冷却？

只有实际执行 seat/OAuth 副作用前才占用冷却。以下情况不消耗：

- 没有可用 quota，返回 `no_quota_available`。
- 当前状态已经符合目标，返回 `no_changes_needed`。
- 只读取 runtime status / quota cache。
