# Intent Routing

Uploaded files are treated as session-scoped context, not as a global default.

When a user sends a new text message and the current `/new` session has a recent uploaded file, the app asks a lightweight model router to choose:

- `uploaded_file`
- `mcp`
- `chat`
- `clarify`

The router receives only the user text and file metadata such as filename, resource type, size, and upload time. It does not read file contents during routing.

This is closer to OpenClaw's pattern: resources are context for the current session, and the agent decides whether to use them. The app no longer relies on a hard-coded list of trigger keywords to decide whether every "analysis" or "summary" request should use the latest file.

If the router returns `clarify`, Feishu asks:

```text
你是想基于刚才上传的文件分析，还是查询系统里的业务数据？
```

