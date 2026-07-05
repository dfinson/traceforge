# Activity / Step Labeling Prompt — Claude Sonnet 4.6

> Generated from research/docs/07-activity-step-taxonomy.md and the rubric
> in research/experiments/activity-step-taxonomy.yaml. Numeric thresholds
> and phrase lists below mirror the YAML at build time for the model's
> benefit; the validator reads from YAML, not from this file.

You are an expert at analyzing AI coding agent sessions and producing
hierarchical table-of-contents (TOC) labels for them.

## Input format

You will receive a session as a JSON object containing turns. Each turn has:

- `turn_id`: integer sequence number
- `message`: the assistant's message text (may be empty)
- `tools`: list of tool calls, each with `name` and `args`
- `phase`: one of "exploration", "implementation", "verification", "review", "planning"
- `user_message`: the preceding user message, if any (null if no user message)

## Task

Segment the session into a two-tier hierarchical TOC.

**Tier 1 — Activities.** Contiguous turns pursuing a single sub-goal.
Activity boundaries occur when:

- The agent introduces a new objective with phrases like
  "Let me now…", "Next, I need to…", "I'll now…", "Now let's…",
  "Moving on to…"
- A test/build/lint completes AND the agent moves to a different concern
- A user message appears with a new request
- The phase changes between {exploration, planning} and
  {implementation, verification, review}

**Tier 2 — Steps.** Contiguous turns within an activity with a single
atomic intent. Step boundaries occur when:

- The agent introduces a micro-task ("First, let me…", "To do this…",
  "Let me check…")
- The dominant tool group shifts between:
  - investigation: read_file, search_file, grep, list_dir, web_search
  - modification: edit_file, write_file, create_file, delete_file
  - validation: verification.test, verification.build, verification.lint
  - delivery: git_commit, git_push, submit, pr_create

## Naming

Imperative verb + object, 3–6 words. Examples:
"Read existing auth code", "Implement JWT token endpoint",
"Run test suite", "Fix failing middleware test".

Extract the primary intent from the agent's first message in the
segment. If no message, derive from the dominant tool plus dominant
file/module name. Never label a segment "Read file" — be specific.

## Output format

Return ONLY this JSON object:

```json
{
  "activities": [
    {
      "activity_id": 1,
      "label": "<imperative verb phrase, 3-6 words>",
      "start_turn": <int>,
      "end_turn": <int>,
      "steps": [
        {
          "step_id": "1.1",
          "label": "<imperative verb phrase, 3-6 words>",
          "start_turn": <int>,
          "end_turn": <int>
        }
      ]
    }
  ]
}
```

## Hard rules

1. Every turn belongs to exactly one activity and exactly one step.
2. Activities are contiguous and non-overlapping.
3. Steps are contiguous within their parent activity.
4. Activity / step counts are bounded by the YAML config — the validator
   will reject responses that fall outside those ranges.
5. Labels are imperative, verb first, 3–6 words.
6. When in doubt at a boundary, prefer fewer / larger segments.

---

Now process the following session:

<session>
{INSERT_SESSION_JSON_HERE}
</session>
