## Standards

These hold for every role, in every repo. They are written to be checkable: a reviewer
should be able to point at a line and name the rule it breaks. Where an existing file
disagrees with a rule here, match the file's conventions and say so in your report; a
diff that fights its surroundings costs more to review than it saves.

### Single responsibility

- A function does one thing at one level of abstraction. If you cannot name it without
  "and", split it.
- A module has one reason to change. A file that needs edits for two unrelated reasons is
  two files.
- A function that both decides and acts is two functions: return the decision, act in the
  caller. Decisions are testable without mocks.
- Constructors and module top level do no I/O. Wire dependencies in.

### DRY, with the rule of three

- Duplicated **knowledge** is the bug: two places that must change together and only one
  does. Duplicated text is often fine.
- Two occurrences: leave them. Three: abstract. A wrong abstraction is load-bearing;
  duplication is merely visible.
- Never abstract across a boundary you do not own.
- One source of truth per fact: a type, a constant, a migration, a config key.

### KISS

- Prefer the boring construct: a function over a class, a class over a framework, a
  straight line over a state machine. Reach for the complex one only after the simple one
  failed, and say what failed.
- Delete rather than comment out. Git remembers.
- No speculative configuration. A flag with one value is a constant with extra steps.

### YAGNI

- Solve the task in front of you. Not the general case, not the next three tasks, not a
  plugin architecture nobody asked for.
- No new dependency unless the plan calls for it. Before adding one: is it in the standard
  library, is it already in the lockfile, is it more than fifty lines of work? Every
  dependency is a supply chain, a licence and an upgrade.
- No public surface, option or parameter without a caller in this change.

### Modern practice (2026)

- **Types at the edges.** Validate untrusted input where it enters and parse it into a
  typed shape; never pass raw request bodies inward. TypeScript: no `any` in a public
  signature, `unknown` plus a parse. Python: annotate; the checker runs in CI.
- **Errors carry context and are handled where they can be.** No empty catch, no
  swallow, no log-and-rethrow at every layer. Fail fast with the identifiers a reader needs.
- **Async without leaks.** Every await has a timeout or a cancellation path; every spawned
  task is awaited or explicitly detached; concurrency over a list you did not size is bounded.
- **Immutability by default.** Return new values; mutate only on a measured hot path.
- **Tests are the specification.** Cover the behaviour the task names and its failure
  path. A test that passes against both the correct and the broken implementation is worse
  than none: watch it fail first. Test through the public surface.
- **Migrations and schema changes are reversible and ordered.** Additive first, backfill,
  then remove; never one deploy that writes the new shape and stops writing the old.
- **Observability is part of the change.** A path that can fail in production logs enough
  to diagnose it, with identifiers and without secrets or personal data.
- **Accessibility and i18n for anything user-facing.** Semantic elements, labelled
  controls, keyboard reachable, contrast checked; no sentence built by string concatenation.
- **Performance where it is measured.** No optimisation without a number. The N+1 query
  is not premature optimisation; it is a defect.

### Security, in every change

- **No secrets anywhere.** Not in code, fixtures, logs, commits, reports or error messages.
  Found one? Stop touching it, do not move it, report it with its path.
- **Every input is hostile until parsed.** Validate type, length, range and character set at
  the boundary; reject, do not sanitise into shape. Path segments match an allowlist and are
  resolved and checked for containment before use; never trust `..`, absolute paths or
  symlinks from outside.
- **No string-built commands or queries.** Parameterised SQL only; subprocess with an
  argument list, never a shell string; no `eval`, no dynamic `import` of user data, no
  templates rendered with user input unescaped.
- **Encode on output.** HTML-escape anything that reaches a page; set `Content-Type`;
  JSON responses are objects, never top-level arrays from user data.
- **Authorise on the server for every request**, from the session, never from a role or id
  the client sent. Deny by default. Check ownership, not just authentication.
- **Least privilege.** New tokens, roles and scopes are the narrowest that work; nothing
  runs as root or with wildcard permissions because it was easier.
- **Supply chain.** Pin versions and verify checksums for anything downloaded in a build.
  No new dependency without a reason in the report; prefer the standard library. Keep the
  lockfile the single source of versions and never hand-edit it.
- **Crypto and randomness.** Use the platform's audited primitives (`secrets`,
  `crypto.randomUUID`, libsodium-class libraries); never roll your own, never `random` for
  anything security-relevant, constant-time comparison for tokens.
- **Data hygiene.** Personal data stays out of logs and test fixtures; delete what is no
  longer needed; keep retention explicit.
- **Fail closed.** A missing config value, a failed check, an unexpected state denies; it
  does not fall through to the permissive branch.

### Write for a reader with thirty seconds

- No chronology: state the conclusion, not what you tried first.
- Do not restate the task or narrate the diff; cite `path:line`.
- Say each thing once. One line of uncertainty, with what would settle it.
- Findings ranked, at most five blocking ones; "3 more of the same kind in `<dir>`".
