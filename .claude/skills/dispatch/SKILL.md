---
name: dispatch
description: "Read a spec, build dependency graph, generate agent prompts, and dispatch parallel agents. Use when the user says 'dispatch', 'spawn agents', or 'implement the spec'."
---

# Spec-to-Agents Dispatch

## Input
The user provides a spec file path or the spec is in the current conversation.

## Workflow

1. **Parse spec** — Read the spec and identify all implementation tasks
2. **Dependency graph** — Determine which tasks depend on which. Tasks touching the same file are sequential. Tasks touching different files can be parallel
3. **Batch grouping** — Group tasks into waves:
   - Wave 1: All tasks with no dependencies (can run in parallel)
   - Wave 2: Tasks that depend only on Wave 1 tasks
   - Wave N: Continue until all tasks are scheduled
4. **Prompt generation** — For each task, generate a detailed agent prompt that includes:
   - Exact file paths to modify
   - Specific line numbers and surrounding context
   - Code snippets from the spec
   - Clear "DO" and "DO NOT" instructions
   - "Use the Edit tool for each change — do NOT rewrite the file"
5. **Dispatch** — Launch agents for the current wave using the Task tool:
   - Use `subagent_type: "general-purpose"` for implementation
   - Run all agents in a wave in parallel (single message, multiple Task tool calls)
   - Wait for all agents in a wave to complete before starting the next wave
6. **Verification** — After all waves complete:
   - Read modified files to spot-check changes
   - Run tests if a test suite exists (`npm test`, `pytest`, etc.)
   - Report any failures
7. **Commit** — If everything looks good, use the /commit skill to commit and push

## Error Recovery
- If an agent fails, read its output to understand why
- Generate a corrected prompt addressing the specific failure
- Re-dispatch only the failed agent
- Max 2 retries per agent before escalating to the user

## Error Classification

Classify failures into categories based on agent output:

| Category | Pattern | Auto-fix strategy |
|----------|---------|-------------------|
| **Edit conflict** | "old_string is not unique" or "old_string not found" | Re-read the file, generate corrected edit with more context |
| **Syntax error** | Parse/compile errors in output | Read the error, fix the specific line |
| **Test failure** | Test output with FAIL | Read failing test, fix the code to match |
| **Ownership/permission** | "EPERM", "Permission denied" | Skip and report to user |
| **Network/timeout** | "ETIMEDOUT", "fetch failed" | Retry unchanged (transient) |
| **Windows compat** | ".cmd", "CRLF", "encoding" | Apply Windows-specific fixes from CLAUDE.md |

## Auto-Retry Flow

1. Agent fails → read output
2. Classify error type
3. If retryable (edit conflict, syntax, test failure, transient):
   a. Generate corrected prompt incorporating the error
   b. Re-dispatch with max_turns reduced
   c. Track retry count (max 2)
4. If non-retryable (permission, unknown):
   a. Log the failure
   b. Continue with other agents
   c. Report to user at the end
5. After all agents (including retries) complete:
   a. Summarize: N succeeded, M failed, K auto-fixed
   b. List any remaining failures with error details

## Resume Capability

Before dispatching, save state to a resume file:
```json
// dispatch/resume-state.json
{
  "spec": "path/to/spec.md",
  "waves": [],
  "completed": ["task-1", "task-2"],
  "failed": [{"task": "task-3", "error": "...", "retries": 1}],
  "current_wave": 2,
  "timestamp": "..."
}
```

If context runs out, the continuation prompt can read this file and pick up where it left off.
