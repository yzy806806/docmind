# Project Context

**Project:** docmind
**Goal:** 自我进化，持续开发docmind项目
**Status:** completed

## Description

接手https://github.com/yzy806806/docmind项目开发，让该项目独立运行，且拥有功能完善、美观的webui交互页面。

## Stop Condition

The project should stop when: 运行稳定，对比同类产品，没有功能缺失。
If the stop condition appears to be met, the leader should raise a motion for the team to vote on whether to stop.

**Heartbeat Member:** leader (woken every 30 min)

## Team Members

| Name | Role |
|------|------|
| researcher | Researcher |
| developer | Developer |
| architect | Architect |
| tester | Tester |
| leader (heartbeat) | Team Leader |
| reviewer | Reviewer |

When creating follow-up tasks, assign them to the appropriate team member above.
Use `hermes profile list` to verify member availability.

## Workflow

1. Check `hermes kanban list` for your assigned tasks.
2. Use `kanban_show()` to read task details.
3. After completing a task, use `kanban_complete(summary=..., metadata=...)`.
4. If blocked, use `kanban_block(reason=...)` with a clear explanation.
5. For design decisions that need team input, use `agora_raise_motion`.
