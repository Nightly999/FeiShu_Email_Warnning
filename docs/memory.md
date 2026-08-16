# Memory Design

The agent uses two memory layers.

## Short-term memory

Short-term memory is stored in `conversation_turn`.

- Scope: `tenant_key + app_id + open_id + chat_id + session_id`
- Update: automatic after each successful agent reply
- Usage: injected into the system prompt as recent conversation context
- Limit: controlled by `short_memory_turns`

This keeps multi-turn conversation coherent without storing the full chat history in every LLM request.

`/new` creates a new `conversation_session`. New questions no longer load short-term turns from the previous session.

## Long-term memory

Long-term memory is stored in `agent_memory`.

- Scope: `tenant_key + app_id + open_id + chat_id`
- Update: explicit user commands only
- Usage: injected into the system prompt in private chats
- Limit: controlled by `long_memory_limit`

Supported commands:

- `记住：...`
- `查看记忆`
- `忘记：...`
- `/new`

The model does not write long-term memory by itself. This avoids silently storing incorrect or sensitive facts.

## Privacy Boundary

Private chats can load and manage long-term memory.

Group chats do not load, show, write, or delete long-term personal memory by default. They only use short-term context scoped to the current user and chat.

This follows the same practical boundary as OpenClaw: personal memory should not leak into shared conversations.
