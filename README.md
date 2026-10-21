# claude-graph

A LangGraph harness for coding tasks. One request goes through five role agents —
**pm → architect → coder → qa → reviewer** — with bounded loops back on failure, and
comes out as a reviewed branch. The product manager writes the spec and acceptance
criteria first; every later role works against them. Every agent is a headless Claude
Code session running inside a sandbox, launched with Apple `container` by `abox`, the
runner from the [sandbox repo](../claude-box).

```
START → pm → architect → coder → qa ──pass──→ reviewer ──approve──→ END  (done)
                          ▲        │ fail           │ changes
                          └────────┘                │
                          └─────────────────────────┘
               architect ◄──────────────────────────┘ redesign
               any loop past --max-iterations → END  (exhausted)
```

No Anthropic API key is involved: the boxes use the Claude Code login that the sandbox
already keeps. The orchestrator never talks to the container engine itself.

## Setup (once)

Prerequisites: macOS 26 or newer on Apple Silicon, Homebrew, a Claude account that
Claude Code can log in with. Nothing here needs an Anthropic API key.

```sh
brew install --cask container                     # Apple `container`; a signed pkg, asks for your password
brew install python@3.14                          # 3.15 is too new for the dependency wheels
git clone --recurse-submodules <this repo> ~/Documents/GitHub/claude-graph
cd ~/Documents/GitHub/claude-graph
/opt/homebrew/opt/python@3.14/bin/python3.14 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/claude-graph setup                      # checks, fixes, and lists what is left
```

`setup` starts the `container` daemon, builds the sandbox image for that engine (a few
minutes, once), links `abox` into `~/.local/bin`, and checks the sandbox login. It prints
one line per item — `ok`, `fixed`, or `TODO` with the exact command — and exits 1 while
anything is left. Re-run it until it is quiet. The two things only a human can do:

1. the `brew install --cask container` above (needs your password);
2. **log in once, inside a box.** Headless boxes cannot answer a login prompt:

   ```sh
   abox ~/Documents/GitHub/claude-graph
   ```

   Inside Claude Code type `/login`, finish in the browser, then `/exit`. The credentials
   persist in the sandbox's own login file under `~/Library/Application Support/` and
   every later box reuses them. Repeat this whenever `setup` or a run reports
   `refresh-expired` or `missing`.

Where the sandbox runner comes from, first match wins: `CLAUDE_GRAPH_RUNNER`; `abox` on
PATH; the `sandbox/` submodule of this repo; a clone next to this repo.
`--runner <path>` overrides per command. The engine is passed explicitly on every call, so
a Docker-flavoured runner you already have stays as it is.

Optional: `gh auth login` on the host. `abox` forwards the existing `gh` token into boxes;
the roles here never push, so it only matters if your repo's tests need GitHub.

## What persists after setup

Nothing in the setup list repeats. On every later `claude-graph run`:

- the Apple `container` daemon is started by `abox` if it is down;
- the image is reused from the engine's store;
- the Claude login is read from the sandbox's own login file (separate from the host's
  Keychain), and a refreshed token is
  written back there so every box shares it; re-login is only needed after weeks of
  not running anything, when the refresh token itself expires;
- Claude Code updates itself: a box's first start after 6 hours (`CLAUDE_BOX_UPDATE_TTL`)
  installs `@anthropic-ai/claude-code@latest` into a host-persisted pnpm dir, later boxes
  reuse it, and a failed update falls back to the version baked into the image;
- the host's `gh auth token`, SSH agent socket, git user name and email, and timezone
  are forwarded into each box automatically. Host `~/.claude` plugins and skills are
  not (by design; `CLAUDE_BOX_SEED_PLUGINS=1` opts in).

Do not export `ANTHROPIC_API_KEY` in the shell you run claude-graph from: `abox`
forwards it, and Claude Code then bills the key instead of using the login.

## Run a task

Two terminals. First the dashboard, then the run:

```sh
claude-graph ui --actions                              # terminal 1 → http://127.0.0.1:8765/
claude-graph run ~/Documents/GitHub/<repo> "add a /health endpoint that reports DB status"
```

What happens: a worktree on branch `claude-graph/<task-id>` is cut from the repo's HEAD
inside a box; then one box per role
runs in it — pm, architect, coder, qa, reviewer — looping back on a QA fail or a
reviewer's `changes` (to the coder) or `redesign` (to the architect), at most
`--max-iterations` coder runs (default 3). The terminal prints a summary at the end;
the dashboard shows every step and its live log while it runs.

```sh
claude-graph run <repo> "<request>" --task-id health-route --max-iterations 2
claude-graph run <repo> --resume health-route        # continue after a crash or Ctrl-C
claude-graph status health-route
claude-graph logs health-route                        # every box log for the task
claude-graph logs health-route qa                     # the latest QA log
```

`run` exits 0 on `done`, 1 on `exhausted`/`failed`, 2 on a usage error.

## Take the result

Nothing is pushed or merged. The deliverable is the branch `claude-graph/<task-id>` in
your repo:

```sh
cd ~/Documents/GitHub/<repo>
git log main..claude-graph/health-route
git diff main...claude-graph/health-route
git merge claude-graph/health-route          # or cherry-pick, or branch -D to discard
abox headless . --worktree health-route --remove          # drop the worktree; the branch stays
```

Flags for `run`: `--engine container|docker|fake` (default `container`),
`--runner <path>`, `--timeout <seconds per box>` (default 3600).

### Dashboard (`ui`)

`claude-graph ui` serves a read-only page; `claude-graph ui --actions` also lets the page
act, with a token minted per start (same model as the sandbox project's own dashboard).
Localhost only: any other `Host` header is refused, and a POST without the page's token
is refused.

Data, polled every 3 s from one `/api/state` call:

- **header** — engine, login state (`ok`, `expired`, `refresh-expired`, `missing`,
  `api-key`), boxes in flight, spend total and today;
- **In flight** — one card per box: role, task, model, effort, elapsed, box name;
- **Needs you** — expired login, failed / exhausted / stopped tasks (orchestrator died
  mid-run), boxes silent for over 20 minutes;
- **Tasks** — a board by status (`running`, `stopped`, `failed`, `exhausted`, `done`);
  each card shows the pipeline strip with the live node, iteration, cost, branch,
  commits ahead of base with a diffstat, QA and review verdicts, the next action;
- **Activity** — every box run across tasks, newest first; **Logs** — every log file;
- **Roles and spend** — per role: model, runs, share of reports that parsed, average
  duration, cost; **Engine** — `abox ps`; **Config** — defaults and state dir.

The drawer opens a task document (request, spec, acceptance criteria, plan, coder
summary, QA report, review notes, step table), a live log tail (rendered or raw,
following), or the branch diff (`git log` + `git diff base...branch` on the host).
Browser notifications on task completion once you click "notify me".

Actions (`--actions` only; destructive ones ask you to type the task id):

| action | what it runs |
| --- | --- |
| new task | `claude-graph run <repo> "<request>" [--task-id] [--max-iterations]` as a detached process |
| resume | `claude-graph run <repo> --resume <task-id>` |
| cancel | SIGTERM to the orchestrator, then `abox stop` on its running box |
| stop box | `abox stop <box>` (the graph records a failed step and continues) |
| drop worktree | `abox headless <repo> --worktree <id> --remove` (branch stays) |
| delete | removes the task's snapshot, logs and checkpoints; branch and worktree stay |
| purge finished | delete for every task that is not running |

`claude-graph ui --port 9000` picks another port.

### Per-role model and effort

`<repo>/claude-graph.toml` (or `~/.config/claude-graph/config.toml`):

```toml
[models]
coder = "sonnet"      # override per role
[efforts]
qa = "low"            # default for every role: high
```

Defaults: `pm` and `architect` on `fable`, `coder`, `qa` and `reviewer` on `opus`,
all at effort `high`. Values are Claude Code `--model` / `--effort` arguments.

## How a step works

1. The graph node asks the runner for a box: role, task id, attempt number, repo.
2. The runner writes the role's system prompt (`claude_graph/roles/<role>.md` + `_rules.md`
   + `_standards.md`: SRP, DRY, KISS, YAGNI, 2026 practice, security, checkable by rule name)
   and the brief built from state (request, plan, prior QA/review notes) to files, then runs
   `abox headless <repo> --worktree <task-id> --name graph-<task>-<role>-<n> …`.
   The runner creates the worktree in-container on the first call and reuses it after.
3. The box's stream-json goes to `<state>/logs/<task-id>/<nn>-<role>.log`. The final
   `result` event's text is the role's answer; its last fenced ```json block is the report.
4. The node parses the report and updates state. Unparseable reports take the conservative
   value: QA → fail, reviewer → changes, architect → raw text as the plan.

Report contract:

| role | block |
| --- | --- |
| pm | `{"spec": "…", "acceptance_criteria": ["…"]}` |
| architect | `{"plan": "…"}` |
| coder | `{"summary": "…"}` |
| qa | `{"passed": true|false, "report": "…"}` |
| reviewer | `{"verdict": "approve"|"changes"|"redesign", "notes": "…"}` |

State lives in `~/Library/Application Support/claude-graph/` (override with
`CLAUDE_GRAPH_STATE`): `checkpoints.sqlite` (LangGraph checkpoints, one thread per task),
`logs/<task-id>/`, `tasks/<task-id>.json` (final state).

## Tests

```sh
.venv/bin/pytest            # no containers, no tokens: a scripted fake runner + a fake abox
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

The bash side of the seam is tested in the sandbox repo: `bash tests/headless.test.sh` there,
against its `fake` engine.

## Troubleshooting

- **Boxes hang at "Checking connectivity" / endless API retries** on Apple `container`:
  the host's IP forwarding was switched off (a VPN client does this on connect and does
  not restore it). `abox` re-enables it and may ask for `sudo` once.
- **`CLAUDE_BOX_ENGINE=container but the 'container' CLI is not on PATH`**: `brew install --cask container`.
- **Image build fails with `corepack: not found`**: Vite+ 1.3 dropped its corepack shim;
  the sandbox Dockerfile (2026-10-21) uses the pnpm shim instead. Pull the sandbox repo.
- **`runner not found`**: `git submodule update --init`, or pass `--runner <path to abox>`.
- **A task's first box fails with an auth error**: log in once interactively (Setup, item 2).
- **Stale box name already running**: `abox ps`, then `abox stop <box>`;
  a crashed orchestrator can leave a box behind, and `--resume` reuses the next attempt
  number, not the same name.
