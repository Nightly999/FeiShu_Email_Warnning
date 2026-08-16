# Model Routing

The project supports model routing and fallback through `app/models`.

## Routes

Routes are configured in `config/models.local.json`.

- `text`: normal LangGraph chat and MCP tool use
- `vision`: uploaded image analysis
- `video`: uploaded video analysis through models that support `video_url`

Example:

```json
{
  "routes": {
    "text": ["qwen3.7-plus", "qwen3-coder-plus", "qwen3-coder-next"],
    "vision": ["qwen3-vl-plus", "qwen3.7-plus"],
    "video": ["qwen3.7-plus", "qwen3-vl-plus"]
  }
}
```

If the first model times out or errors, the runtime tries the next model in the route.

Video input uses OpenAI-compatible `video_url` messages. For local Feishu uploads, the file is encoded as a Base64 Data URL before being sent to the `video` route. Keep `MAX_VIDEO_UPLOAD_BYTES` conservative because Base64 video input can be large.

## Secrets

Keep API keys in `.env`.

`config/models.local.json` should normally contain model ids, routes, input types, and timeout settings only.

## OpenClaw Reference

OpenClaw uses the same general idea: primary models, fallback models, and a separate image model route. This project implements the same pattern in a small Python module instead of copying OpenClaw's full agent runtime.
