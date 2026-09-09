---
name: journal
description: Log a ramp-up question, or batch-answer the accumulated ones against the codebase. Use when the user says they do not understand something, wants to note a question for later, or asks to review or answer their open questions.
disable-model-invocation: true
---

# Ramp-up journal

Cheap to run, disproportionately valuable after three months.

## Mode A — log (arguments given)

Append one line to `journal/questions.md` under today's date:

```
- [ ] <question> — context: <what they were doing> — <YYYY-MM-DD>
```

Then **stop**. Do not investigate, do not offer to. The whole point is that
logging is frictionless mid-task.

## Mode B — answer the backlog (no arguments)

1. Read open (`- [ ]`) questions.
2. Group them — questions about one service share investigation work.
3. Start with `mcp__cartographer__repo_map` per group, then read code.
4. Also run `mcp__cartographer__open_questions` — the scan's own unresolved
   findings usually overlap with what the user is confused about.
5. Write answers to `journal/answers.md` with `file:line` citations and a date.
6. Tick the question closed, linking to the answer.
7. Anything unanswerable from code stays open, tagged `#ask-team`. **Those are
   the valuable ones** — knowledge that exists only in people's heads, which is
   exactly what to spend a colleague's time on.

## Promoting

When answers cluster around a topic, offer to promote them into a service
brief, a flow trace, or a glossary entry. The journal is a capture buffer, not
a final home.

## Rules

- **Never answer from general knowledge of how order management systems usually
  work.** Either cite this codebase or tag `#ask-team`. A plausible-sounding
  wrong answer is the main risk here, and a new hire may not catch it.
- Keep entries short. This is a log, not an essay.
