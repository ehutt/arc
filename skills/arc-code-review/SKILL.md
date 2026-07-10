---
name: arc-code-review
description: Perform a rigorous multi-perspective code review of the current sandbox or checkout. Use when reviewing a change, PR, branch, or diff and looking for behavior regressions, test gaps, interface/ergonomics problems, security risks, or a synthesis of findings.
---

# Arc code review

Review the current checkout or the repository and change set named by the
user. Work from the current working directory; do not assume an arc project
note or a Phoenix repository exists. If a project note, PR description, or
plan is available, read it for intent and acceptance criteria.

Choose the perspectives that fit the request. For a full review, cover
behavior, tests, interface/ergonomics, and security when relevant. For a
focused review, use only the requested perspective.

Detailed perspective prompts are available in `references/behavior.md`,
`references/tests.md`, `references/interface.md`, and
`references/security.md`. The synthesis guidance is in
`references/synthesis.md`; read the relevant files when producing a structured
multi-agent-style review.

Prioritize concrete correctness and regression risks over style. Trace changed
logic through callers, error paths, boundaries, persistence, concurrency, and
backward compatibility. Report file and line locations, a reproducible failure
scenario, severity, and a practical fix. Do not modify source code, commit,
push, post review comments, or update project metadata unless the user
explicitly asks for implementation after the review.

## Review workflow

1. Establish the change set (`git status`, `git diff`, and the relevant base
   branch or PR diff).
2. Read relevant project requirements, plans, and surrounding callers.
3. Inspect behavior and regression risks.
4. Check tests and test coverage; run focused tests when practical.
5. Check interface and ergonomics, including public API compatibility.
6. Check security, authorization, secrets, validation, injection, and data
   exposure when the change touches those areas.
7. Return a concise report with findings first, then tests run and residual
   questions.

## Finding format

Use this structure unless the user requests another format:

```text
### [blocking|suggestion|question] Short title
**File:** path/to/file.py:123
**Issue:** Trigger/state → incorrect behavior or consequence.
**Suggested fix:** Concrete remediation.
```

Severity guidance:

- `blocking`: real failure, regression, security issue, data loss, or broken
  invariant that should stop merge.
- `suggestion`: worthwhile improvement without a demonstrated blocking failure.
- `question`: missing context needed to determine correctness.

End with a verdict (`BLOCKING`, `SUGGESTIONS`, or `CLEAN`) and summarize test,
lint, and other verification results. Never claim a command passed unless it
was actually run.
