# Docker 部署

Docker 模式同样运行 `swap_seat-only` WebUI/API。

## 快速启动

```bash
git clone https://github.com/jackylai2660707/autoteam-J.git
cd autoteam-J
mkdir -p data
cp .env.example data/.env
docker compose up -d
```

打开：

```text
http://<server>:8787
```

## 数据目录

`docker-compose.yml` 会把运行数据放在 `data/`。建议备份：

| 文件 | 说明 |
|---|---|
| `data/.env` | 配置 |
| `data/state.json` | 管理员 session/workspace |
| `data/swap_seat_quota_state.json` | quota cache |
| `data/swap_seat_cooldown.json` | swap 冷却 |
| `data/accounts.json` | pending invite 注册状态兼容记录 |

## 推荐配置

编辑 `data/.env`：

```dotenv
API_KEY=change-me

CPA_URL=http://host.docker.internal:8317
CPA_KEY=your_cpa_key

MAIL_PROVIDER=cloudflare_temp_email
CF_TEMP_EMAIL_BASE_URL=https://tempmail.example.com
CF_TEMP_EMAIL_ADMIN_PASSWORD=your_admin_password
CF_TEMP_EMAIL_DOMAIN={random}.a.com;{random}.b.com

AUTO_CHECK_TARGET_SEATS=2
AUTO_CHECK_REPLACE_WITH_PENDING_INVITE=true
AUTO_CHECK_REPLACE_MODE=pending_invite
TEAM_WORKSPACES_JSON=[]
```

如果 CPA 跑在同一台宿主机，Linux 上可能需要在 compose 中配置 `extra_hosts` 或直接填写宿主机网关 IP。

## 更新

```bash
git pull
docker compose build
docker compose up -d
```

## 查看日志

```bash
docker compose logs -f autoteam
```

## Playwright 代理

如需浏览器流量走代理：

```dotenv
PLAYWRIGHT_PROXY_URL=socks5://host.docker.internal:1080
PLAYWRIGHT_PROXY_BYPASS=localhost,127.0.0.1
```

带认证的 SOCKS5 不受 Chromium 支持；需要认证时建议使用 HTTP 代理：

```dotenv
PLAYWRIGHT_PROXY_URL=http://user:pass@host.docker.internal:1080
```

## 安全提醒

- 不要把真实 `API_KEY`、`CPA_KEY`、管理员 session 提交到 git。
- WebUI 只建议暴露在内网或反向代理鉴权后。
- Docker 部署不改变安全边界：仍然不会 kick/remove/cancel invite；只有显式 `invite-add` 模式会创建一个新的 CFMail invite。
