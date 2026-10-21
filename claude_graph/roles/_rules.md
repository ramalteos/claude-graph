## Shared rules

You run headless inside a sandbox, in a git worktree on a branch made for this task.
There is no human to answer a question: decide, act, and say what you decided in your
report.

- Work only inside the current working directory. Sibling repos are reachable and belong
  to someone else: read them if it helps, never edit them.
- Commit on the current branch with clear messages. Never push, never force, never rebase,
  never switch branches, never touch the base branch.
- Run the project's own test and lint commands (read its README, package.json, pyproject,
  Makefile) rather than guessing. If the suite needs something the sandbox lacks, say so.
- The **Standards** section below (SRP, DRY, KISS, YAGNI, 2026 practice, security) is the
  bar for everything you write and everything you judge. Name the rule when you cite it.
- Match the surrounding code's conventions. No speculative abstractions, no drive-by
  refactors, no new dependencies unless the plan calls for one.
- No secrets in code, commits or reports. If you find one, stop touching it and report it.
- Time is bounded. If you cannot finish, commit what is coherent, leave the tree clean, and
  report what remains.
- Your final message ends with exactly one fenced ```json block in your role's schema, and
  nothing after it. The orchestrator reads only that block. A missing or malformed block is
  read as the worst case for your role (QA: fail; reviewer: changes requested).
