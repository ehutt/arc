---
name: arc-code-review
description: Perform a focused single-pass code review of the current sandbox or checkout. Use when reviewing a change, PR, branch, or diff for correctness, robustness, simplification, ergonomics, interface quality, test gaps, and security risks. This is the portable normal review; arc's thorough multi-lens review remains arc-only.
---

# Arc code review

Review the current checkout or the repository and change set named by the
user. Work from the current working directory; do not assume an arc project
note, Phoenix repository, configured test command, or named default branch
exists. If a project note, PR description, or plan is available, read it for
intent and acceptance criteria. This skill is a single-pass review, not arc's
thorough multi-lens/debate workflow.

## Arc project context

When the checkout appears to be managed by arc, recover its project context
before reviewing:

1. Prefer `ARC_PROJECT_SLUG` when it is set.
2. Otherwise inspect the current sandbox path, branch name, and `arc` project
   index to identify the matching project slug.
3. If the Obsidian vault is discoverable from arc configuration or the global
   project index, read `Projects/<slug>/index.md` and the project `notes.md`.
4. Use those notes as requirements, history, and acceptance criteria. Do not
   edit their frontmatter or session notes during a review.

If no reliable project mapping or vault path can be established, continue with
the repository, branch, and PR context and state that the arc project notes
were unavailable. Never guess a project note from a similarly named folder.

Prioritize concrete correctness and regression risks over style. Also look for
unnecessary complexity, duplicated state, unjustified abstractions, unsafe
defaults, and interface friction. Trace changed logic through callers, error
paths, boundaries, persistence, concurrency, and backward compatibility.
Report file and line locations, a reproducible failure scenario, severity, and
a practical fix. Do not modify source code, commit, push, post review
comments, or update project metadata. If the user later asks to implement a
finding, treat that as a separate request.

## Review workflow

1. Establish the change set (`git status`, `git diff`, and the relevant base
   branch or PR diff). Prefer an explicit PR/base supplied by the user, then
   `origin/HEAD`, then the current branch's merge-base; never assume `main`.
2. Read relevant project requirements, plans, and surrounding callers.
3. Inspect behavior and regression risks.
4. Check tests and test coverage; run the smallest relevant focused tests when
   practical. Do not run an entire repository suite unless it is clearly small
   or the user asks for it.
5. Check interface and ergonomics, including public API compatibility,
   discoverability, naming, defaults, error messages, and safe paths.
6. Check security, authorization, secrets, validation, injection, and data
   exposure when the change touches those areas.
7. Return a concise report with findings first, then verification and residual
   questions.

## Evidence standard

- Do not report a concern without a concrete trigger → consequence chain.
- For blocking findings, identify the violated invariant, caller/state path,
  input, or missing test that makes the failure credible.
- Do not turn preferences, vague code smells, or hypothetical redesigns into
  findings. If evidence is insufficient, label it a `question` or omit it.
- Prefer one precise finding over several overlapping weak findings.
- Distinguish an actual defect from a suggestion for simplification or
  elegance.
- Never claim a test, lint command, or tool result was run unless it actually
  was run.

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

End with a verdict (`BLOCKING`, `SUGGESTIONS`, or `CLEAN`) and summarize the
review boundary: changed files, important callers/paths inspected, and tests
or lint actually run. A clean result should state what was checked, not merely
say “no issues.”
