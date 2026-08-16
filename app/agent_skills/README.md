# Agent Skills

把大模型侧技能放在这个目录里即可自动加载。

支持两种形式：

- `app/agent_skills/data-analysis/SKILL.md`
- `app/agent_skills/production-wip.md`

建议每个 Skill 写清楚：

- 什么时候使用
- 可以调用哪些 MCP 工具
- 分析步骤
- 输出格式
- 禁止事项

注意：Skill 只指导模型如何工作，不负责权限控制；权限仍由系统和 MCP 工具校验。
