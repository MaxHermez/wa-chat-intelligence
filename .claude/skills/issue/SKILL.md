---
name: issue
description: File a Linear issue for the wa-chat-intelligence project using the standard template (Context/Task/Acceptance criteria/Notes).
disable-model-invocation: true
argument-hint: <issue title>
---

File a Linear issue for the wa-chat-intelligence project.

**Title**: $ARGUMENTS

**Defaults** (override only if explicitly told otherwise):
- Team: HHC
- Project: wa-chat-intelligence
- State: Backlog
- Priority: 3 (Normal)

**Process**:
1. If $ARGUMENTS is just a title, ask the user for enough context to fill the template below. If $ARGUMENTS contains a detailed description, extract the information directly.
2. Write the issue description using this exact template:

```
## Context
Why this needs to exist / what problem it solves.

## Task
- Specific step 1
- Specific step 2

## Acceptance criteria
- [ ] Thing that must be true when done
- [ ] Another thing

## Notes
Anything the coding agent should know -- gotchas, related files, dependencies.
```

3. Use the `mcp__linear__save_issue` tool to create the issue.
4. Report back the issue ID (e.g. HHC-117) and URL.
5. Update the issue tracking section in the auto-memory index file (the project's `MEMORY.md` in the `.claude/projects/` directory) with the new issue.
