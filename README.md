# Feishu Multi-Tenant LangGraph Agent

多企业抬头、多飞书自建应用/机器人共用同一个 LangGraph 智能体的接入服务。

## 当前推荐启动方式：飞书长连接

长连接模式不需要公网域名，也不需要配置 HTTP 回调地址，适合本地先联调。

```powershell
cd D:\project\feishu-langgraph-agent
uv sync
uv run python -m app.bootstrap
uv run python -m app.feishu_ws
```

启动后会读取：

```text
config/feishu_apps.local.json
```

并为其中每个飞书自建应用启动一个长连接 client。

## HTTP Webhook 模式

如果以后要部署到服务器，可以用 HTTP 模式：

```powershell
cd D:\project\feishu-langgraph-agent
uv run python -m app.bootstrap
uv run uvicorn app.main:app --host 0.0.0.0 --port 8088
```

健康检查：

```text
GET http://localhost:8088/health
```

飞书事件地址：

```text
POST https://your-domain/feishu/events/{bot_code}
```

## 飞书后台配置

在每个自建应用里配置：

1. 开启「机器人」能力。
2. 进入「事件与回调」。
3. 订阅方式选择「使用长连接接收事件」。
4. 添加事件：`im.message.receive_v1`。
5. 在「回调配置」中启用 `card.action.trigger`，同样使用长连接。
6. 开启机器人接收消息、发送消息相关权限。
7. 发布应用版本。

长连接模式下，不需要填写请求网址。

## 邮箱分析功能

邮箱绑定在飞书卡片内完成：用户直接填写邮箱账号和密码，点击「验证并绑定」；
密码框默认遮罩，也可以临时显示确认。绑定和卡片回调都由长连接处理，不需要公网域名
或独立的邮箱登录页面。

```powershell
uv run python -m app.feishu_ws
```

邮件数据会写入独立 SQL Server 数据库 `AI_outlook_email`；首次连接时自动执行
`schema/email_sqlserver_tables.sql` 创建 `asi` schema 和邮件表。部署主机需要安装
Microsoft ODBC Driver 17/18，或修改 `EMAIL_SQLSERVER_DRIVER` 使用已安装版本。

生成 32 字节 Base64 邮箱凭据密钥：

```powershell
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

将输出保存到服务器环境变量 `EMAIL_CREDENTIAL_KEY`，不要写入仓库。完整配置项见
`.env.example`。启用后可在飞书中发送“分析我的邮件”“重新绑定邮箱”或
“每天9:30和17:20推送当天重要邮件”。

## Bot Code

当前配置的 bot code：

```text
asi-wip
asi-wip-weijie
asi-wip-yuantong
asi-wip-weide
asi-wip-yuanda
asi-wip-chuangshiji
asi-wip-fantai
asi-wip-yayuan
asi-wip-shanghaiyayuan
```

## MCP

默认 MCP 地址在 `.env`：

```env
MCP_BASE_URL=http://127.0.0.1:8765/mcp/
```

你现有 MCP 项目：

```text
D:\project\openclaw-mcp\servers\src\sqlserver_pyodbc
```

先启动 MCP HTTP 服务，再启动本项目。

## 千问模型

`.env` 已使用 DashScope OpenAI 兼容接口：

```env
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen3.7-plus
```

## 测试

```powershell
uv sync --extra dev
uv run pytest
```

## 生产环境安全配置

正式环境至少应设置：

```env
APP_ENV=production
PERMISSION_FAIL_CLOSED=true
FEISHU_SIGNATURE_MAX_AGE_SECONDS=300
FEISHU_EVENT_WORKERS=8
FEISHU_EVENT_QUEUE_SIZE=64
MAX_FILE_UPLOAD_BYTES=26214400
SQLITE_BUSY_TIMEOUT_MS=10000
```

- HTTP 回调在配置 `encrypt_key` 后会强制校验签名、时间戳和 verification token。
- 请求中的 tenant/app 标识不会覆盖机器人绑定的租户配置。
- 同一 `message_id` 只处理一次；失败任务允许重试，处理中断超过 10 分钟后允许重新领取。
- 长连接使用有界工作池；队列满时会记录错误，应通过监控及时扩容。
- 生产环境不会自动创建示例机器人和示例身份。
- SQLite 已启用 WAL 与等待超时，适合单机灰度；多实例正式部署仍建议迁移到企业数据库和外部消息队列。

## 注意

- `.env` 和 `config/*.local.json` 包含密钥，已加入 `.gitignore`，不要提交到 Git。
- 工具参数里的 `feishuOpenId` 由系统注入，不要让用户或模型自行填写。
- MCP 工具侧仍然应该保留硬权限校验，Agent 侧权限只是第一层拦截。
