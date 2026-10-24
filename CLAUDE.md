# claude-graph — project instructions

A LangGraph harness: pm → architect → coder → qa → reviewer, each node a headless Claude
Code session in a sandbox box launched by the runner (`abox`, from the `sandbox/`
submodule). The orchestrator is Python; the engine side is bash in the sandbox repo.

## Invariants

- **Python never sees a container path and never talks to the container engine.** The
  runner owns worktrees, mounts and the engine; `claude_graph/boxes.py` owns the
  subprocess and the log. Anything engine-specific belongs in the sandbox repo.
- **A role is a `.md` under `claude_graph/roles/` plus one node in `graph.py`.** Reports are
  the last fenced ```json block of the box's final `result` text, validated strictly
  (`reports.py`); an unparseable report takes the conservative branch (qa fail, reviewer
  `changes`).
- **The dashboard reads files, it never recomputes state.** `tasks/<id>.json` is rewritten
  atomically after every node; `logs/<id>/<nn>-<role>.meta.json` is the per-box record.
  A record left open by a dead orchestrator is stale, not running.
- **Nothing is pushed or merged by the graph.** The deliverable is branch
  `claude-graph/<task-id>` in the target repo.
- Docs and commands say "sandbox" / "runner"; the engine-side project name stays in code
  identifiers only.

## Working here

- `make setup` bootstraps a clone (venv, pinned install, `claude-graph setup`).
- `make test` = ruff check + format check + pytest. No containers, no tokens: the suite
  runs against `tests/fake_claude_box.sh` and `tests/fake_runner.py`. Verified on macOS and
  Linux, Python 3.12 or newer.
- Dependencies are pinned in `requirements.lock` (pip constraints). After touching
  `pyproject.toml` dependencies run `make lock` and commit the result.
- The bash side of the headless seam is tested in the sandbox repo:
  `bash tests/headless.test.sh` there, against its fake engine.
- Design notes and plans live under `docs/superpowers/` and are gitignored on purpose.
