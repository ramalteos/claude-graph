# claude-graph

A LangGraph harness for coding tasks. One request goes through five role agents —
**pm → architect → coder → qa → reviewer** — with bounded loops back on failure, and
comes out as a reviewed draft pull request. The product manager writes the spec and
acceptance criteria first; every later role works against them. Every agent is a headless
Claude Code session running inside a sandbox, launched with Apple `container` by `abox`,
the runner from the [sandbox repo](https://github.com/ramalteos/claude-box), vendored
here as the `sandbox/` git submodule.

```
START → pm → architect → coder → publish → qa ──pass──→ reviewer ──approve──→ END  (done)
                          ▲                  │ fail           │ changes
                          └──────────────────┘                │
                          └───────────────────────────────────┘
               architect ◄────────────────────────────────────┘ redesign
               any loop past --max-iterations → END  (exhausted)
```

`publish` runs on the host, not in a box: it pushes the task branch, opens one draft pull
request on `origin` (the first time), and waits for the PR's checks. QA then runs the unit
tests locally and takes integration, end-to-end and build results from CI; the reviewer
reviews the pull request on GitHub and posts its findings there; on `approve` the PR is
marked ready for review. Nothing is ever merged by the graph. `--no-publish` keeps a task
local: no push, no PR, QA runs everything itself.

No Anthropic API key is involved: the boxes use the Claude Code login that the sandbox
already keeps. The orchestrator never talks to the container engine itself.

## Quick start

```sh
make setup                                        # inside this clone, once per machine; must end with "everything is in place"
claude-graph ui --actions &                       # dashboard: http://127.0.0.1:8765/
cd <repo>
claude-graph run "<what you want done>"           # or: claude-graph run <repo> "<request>" from anywhere
```

`setup` links `claude-graph` and `abox` into `~/.local/bin` (and says so if that folder is
not on your PATH) and lists anything missing with the command that fixes it; the one step it cannot do for you is the sandbox login
(`abox <repo>`, then `/login`, `/exit`). Ctrl-C during a run stops the box; continue later
with `claude-graph run --resume <task-id>`. Details below.

## Setup (once)

Prerequisites: macOS 26 or newer on Apple Silicon, Homebrew, a Claude account that
Claude Code can log in with. Nothing here needs an Anthropic API key.

```sh
brew install --cask container                     # Apple `container`; a signed pkg, asks for your password
brew install python@3.14                          # 3.12 or newer (3.15 works too); macOS's own python3 is 3.9
git clone --recurse-submodules <this repo>
cd claude-graph
make setup                                        # venv + pinned install + `claude-graph setup`
```

The submodule URL is relative (`../claude-box`), so clone from the host that has both
repos side by side and make sure you can read both. Without the submodule, `setup` tells
you to run `git submodule update --init`, or you point `--runner` at any current `abox`.

`make setup` creates `.venv` with the newest `python3.1x` on your PATH (override with
`make setup PY=/path/to/python`), installs the exact versions in `requirements.lock`,
then runs `claude-graph setup`. That starts the `container` daemon, builds the sandbox
image for that engine (a few minutes, once), links `claude-graph` and `abox` into
`~/.local/bin`, and checks the sandbox login. It prints
one line per item — `ok`, `fixed`, or `TODO` with the exact command — and exits 1 while
anything is left. Re-run it until it is quiet. The two things only a human can do:

1. the `brew install --cask container` above (needs your password);
2. **log in once, inside a box.** Headless boxes cannot answer a login prompt:

   ```sh
   abox <path to this clone>                      # setup prints the exact command
   ```

   Inside Claude Code type `/login`, finish in the browser, then `/exit`. The credentials
   persist in the sandbox's own login file under `~/Library/Application Support/` and
   every later box reuses them. Repeat this whenever `setup` or a run reports
   `refresh-expired` or `missing`.

Where the sandbox runner comes from, first match wins: `CLAUDE_GRAPH_RUNNER`; `abox` on
PATH; the `sandbox/` submodule of this repo; a clone next to this repo.
`--runner <path>` overrides per command. The engine is passed explicitly on every call, so
a Docker-flavoured runner you already have stays as it is; `setup` refuses a runner too
old to have the `headless` command.

Repos anywhere on disk work. Those under `CLAUDE_BOX_ROOT` (default `~/Documents/GitHub`)
are mounted with their siblings at the same path inside the box; a repo elsewhere is
mounted alone, and its task worktrees land in `<repo>/.claude-box-runs/` (git-excluded).

`gh auth login` on the host, with an account that can push to the target repo and open
pull requests on it. The host uses it to push and to open the PR; `abox` forwards the same
token into boxes, where QA reads CI logs and the reviewer posts its review. Only
`--no-publish` tasks work without it.

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
cd <repo>                                              # terminal 2
claude-graph run "add a /health endpoint that reports DB status"
```

`claude-graph run <repo> "<request>"` works from anywhere; without a path the current
directory is the repo, as with the sandbox CLI. The first word is the repo when it is a
git checkout (so `claude-graph run myrepo "…"` works from its parent folder) or is spelled
as a path (`./x`, `../x`, `/x`) to a directory; a bare word that merely matches a plain
folder name is part of the request.

What happens: a worktree on branch `claude-graph/<task-id>` is cut from the repo's HEAD
inside a box; then one box per role runs in it — pm, architect, coder, qa, reviewer —
looping back on a QA fail or a reviewer's `changes` (to the coder) or `redesign` (to the
architect), at most `--max-iterations` coder runs (default 3). After every coder run the
host pushes the branch, opens the draft PR against the branch that was checked out when
the task started (once), and waits up to `--ci-timeout` seconds (default 900) for the PR's
checks; the result goes into QA's and the reviewer's briefs. The terminal prints a summary
at the end; the dashboard shows every step, its live log, the PR and its CI state.

```sh
claude-graph run <repo> "<request>" --task-id health-route --max-iterations 2
claude-graph run <repo> --resume health-route        # continue after a crash or Ctrl-C
claude-graph status health-route
claude-graph logs health-route                        # every box log for the task
claude-graph logs health-route qa                     # the latest QA log
```

`run` exits 0 on `done`, 1 on `exhausted`/`failed`, 2 on a usage error, 130 on Ctrl-C,
SIGTERM or a hangup from a closed terminal (the box is stopped and its record closed;
`--resume` continues from the last finished node). Under `nohup` a hangup stays ignored.
`--resume` also reopens a finished task: a `failed` one runs the role that failed again; an
`exhausted` one needs `--max-iterations` above its old cap and continues the loop.

## Take the result

The deliverable is the pull request from `claude-graph/<task-id>`, a draft until the
reviewer approves, with the reviewer's findings as a review comment and CI run on it.
Merge it on GitHub, or close it; the graph never merges. With `--no-publish` the
deliverable is the local branch instead:

```sh
cd <repo>
git log main..claude-graph/health-route
git diff main...claude-graph/health-route
git merge claude-graph/health-route          # or cherry-pick, or branch -D to discard
abox headless . --worktree health-route --remove          # drop the worktree; the branch stays
```

Flags for `run`: `--engine container|docker|fake|auto` (default `container`),
`--runner <path>`, `--timeout <seconds per box>` (default 3600), `--no-publish`,
`--ci-timeout <seconds>` (default 900).

### Dashboard (`ui`)

`claude-graph ui` serves a read-only page; `claude-graph ui --actions` also lets the page
act, with a token minted per start (same model as the sandbox project's own dashboard).
Localhost only: any other `Host` header is refused, and a POST without the page's token
is refused.

Data, polled every 3 s from one `/api/state` call:

- **header** — engine, login state (`ok`, `expired`, `refresh-expired`, `missing`,
  `unreadable`, `no-expiry`, `api-key`), boxes in flight, spend total and today;
- **In flight** — one card per box: role, task, model, effort, elapsed, box name;
- **Needs you** — expired login, failed / exhausted / stopped tasks (orchestrator died
  mid-run), boxes silent for over 20 minutes;
- **Tasks** — a board by status (`running`, `stopped`, `failed`, `exhausted`, `done`);
  each card shows the pipeline strip with the live node, iteration, cost, branch, the PR
  with its CI state, commits ahead of base with a diffstat, QA and review verdicts, the
  next action;
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
| cancel | SIGTERM to the orchestrator (it stops its box and closes the record, exit 130) |
| stop box | `abox stop <box>` (the graph records a failed step and continues) |
| drop worktree | `abox headless <repo> --worktree <id> --remove` (branch stays) |
| delete | removes the task's snapshot, logs and checkpoints; branch and worktree stay. Refused while the orchestrator is alive; a box record left open by a dead one gets `abox stop` (best effort, failures reported) before it is cleared |
| purge finished | delete for every task that is not running; reports any box it could not stop |

`claude-graph ui --port 9000` picks another port.

### Per-role model and effort

`<repo>/claude-graph.toml` (or `~/.config/claude-graph/config.toml`):

```toml
[models]
coder = "sonnet"      # override per role
[efforts]
qa = "low"            # default for every role: high
```

Defaults: every role on `opus` at effort `high`. Values are Claude Code `--model` /
`--effort` arguments.

## How a step works

1. The graph node asks the runner for a box: role, task id, attempt number, repo.
2. The orchestrator writes the role's system prompt (`claude_graph/roles/<role>.md` +
   `_rules.md` + `_standards.md`: SRP, DRY, KISS, YAGNI, 2026 practice, security, checkable
   by rule name) and the brief built from state (request, plan, prior QA/review notes) to
   files, then runs `abox headless <repo> --worktree <task-id> --name graph-<task>-<role>-<n> …`.
   `abox` creates the worktree in-container on the first call and reuses it after.
3. The box's stream-json goes to `<state>/logs/<task-id>/<nn>-<role>.log`. The final
   `result` event's text is the role's answer; its last fenced `json` block is the report.
4. The node parses the report and updates state. Unparseable reports take the conservative
   value: QA → fail, reviewer → changes, architect → raw text as the plan.
5. After the coder, `publish` (host side, `git push` and `gh`) pushes the branch, keeps one
   draft PR per task, and records the PR's checks as `ci_status` (`pass`, `fail`, `none`,
   `timeout`) plus a per-check summary that QA and the reviewer read.

Report contract:

| role | block |
| --- | --- |
| pm | `{"spec": "…", "acceptance_criteria": ["…"]}` |
| architect | `{"plan": "…"}` |
| coder | `{"summary": "…"}` |
| qa | `{"passed": true \| false, "report": "…"}` |
| reviewer | `{"verdict": "approve" \| "changes" \| "redesign", "notes": "…"}` |

State lives in `~/Library/Application Support/claude-graph/` (override with
`CLAUDE_GRAPH_STATE`): `checkpoints.sqlite` (LangGraph checkpoints, one thread per task),
`logs/<task-id>/`, `tasks/<task-id>.json` (final state).

## Tests

```sh
make test                   # ruff check + format check + pytest; no containers, no tokens
.venv/bin/pytest -q         # the suite alone: a scripted fake runner + a fake abox
```

The suite passes on macOS and Linux, Python 3.12 or newer. Dependencies are pinned in `requirements.lock` (a pip constraints file); after changing
them in `pyproject.toml`, run `make lock` and commit the result.

The bash side of the seam is tested in the sandbox repo: `bash tests/headless.test.sh` there,
against its `fake` engine. With the submodule checked out, `tests/test_boxes.py` also drives
the real runner under that fake engine and checks the Python side reads its output (skipped
without the submodule).

## Troubleshooting

- **Boxes hang at "Checking connectivity" / endless API retries** on Apple `container`:
  the host's IP forwarding was switched off (a VPN client does this on connect and does
  not restore it). `abox` re-enables it and may ask for `sudo` once.
- **`CLAUDE_BOX_ENGINE=container but the 'container' CLI is not on PATH`**: `brew install --cask container`.
- **Image build fails with `corepack: not found`**: an old sandbox Dockerfile; update the
  submodule (`git submodule update --remote sandbox`) and build again.
- **`runner not found`**: `git submodule update --init`, or pass `--runner <path to abox>`.
- **A task's first box fails with an auth error**: log in once interactively (Setup, item 2).
- **Stale box name already running**: `abox ps`, then `abox stop <box>`;
  a crashed orchestrator can leave a box behind, and `--resume` reuses the next attempt
  number, not the same name.
- **`sandbox runner … is too old`**: the runner found first (see the lookup order above)
  predates the `headless` command. Pull it, or pass `--runner sandbox/abox`.
- **Task fails right after the coder with `ci: error`**: the push or `gh pr create` failed;
  the summary holds git's or gh's message. Usual causes: no `origin`, no push rights, `gh`
  not logged in. `--no-publish` runs the task without GitHub.
- **The reviewer could not approve on GitHub**: expected. The forwarded token is the PR
  author's, and GitHub refuses approving your own PR, so the review is a comment and the
  verdict lives in the graph.
