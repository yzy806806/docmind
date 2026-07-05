# Project Context

**Project:** docmind
**Goal:** 持续开发docmind
**Status:** active

## Description

持续开发https://github.com/yzy806806/docmind项目,团队共同讨论开发方向。

## Stop Condition

The project should stop when: 易用性与性能达到最优，对比同类项目，功能无缺失。
If the stop condition appears to be met, the leader should raise a motion for the team to vote on whether to stop.

**Heartbeat Member:** leader (woken every 30 min)

## Team Members

| Name | Role |
|------|------|
| leader (heartbeat) | Team Leader |
| developer | Developer |
| writer | Writer |
| researcher | Researcher |
| tester | Tester |
| reviewer | Reviewer |
| architect | Architect |

When creating follow-up tasks, assign them to the appropriate team member above.
Use `hermes profile list` to verify member availability.

## Workflow

1. Check `hermes kanban list` for your assigned tasks.
2. Use `kanban_show()` to read task details.
3. After completing a task, use `kanban_complete(summary=..., metadata=...)`.
4. If blocked, use `kanban_block(reason=...)` with a clear explanation.
5. For design decisions that need team input, use `agora_raise_motion`.
