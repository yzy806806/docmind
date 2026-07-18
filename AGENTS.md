# Project Context

**Project:** docmind
**Goal:** 美化项目前端界面，提升流畅度
**Status:** active

## Stop Condition

The project should stop when: 网页操作流畅度得到大幅提升。
If the stop condition appears to be met, the leader should raise a motion for the team to vote on whether to stop.

**Heartbeat Member:** leader (woken every 60 min)

## Team Members

| Profile Name | Role (Template) |
|---|---|
| leader (heartbeat) | leader — Team Leader |
| developer | developer — Developer |
| writer | writer — Writer |
| researcher | researcher — Researcher |
| tester | tester — Tester |
| reviewer | reviewer — Reviewer |
| architect | architect — Architect |

Assign tasks by role name (e.g. `assignee='developer'`). The system routes to the correct worker automatically.

## Active Discussions

- `[motion-f98298718b0d]` Phase 15: Vote — Has Frontend Smoothness Stop Condition Been (steps 0/30, discussing)

## Workflow

1. Check `hermes kanban list` for your assigned tasks.
2. Use `kanban_show()` to read task details.
3. After completing a task, use `kanban_complete(summary=..., metadata=...)`.
4. If blocked, use `kanban_block(reason=...)` with a clear explanation.
5. For design decisions that need team input, use `agora_raise_motion`.
