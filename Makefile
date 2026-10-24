# One-liners for a fresh clone. `make setup` is the whole bootstrap.
PY ?= $(shell command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3)
VENV := .venv
BIN := $(VENV)/bin

.PHONY: setup install test lint lock

install: $(BIN)/claude-graph

$(BIN)/claude-graph: pyproject.toml requirements.lock
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
	  || { echo "claude-graph needs Python 3.12 or newer; $(PY) is too old."; \
	       echo "  brew install python@3.14   or   make setup PY=/path/to/python3.1x"; exit 1; }
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -q -c requirements.lock -e '.[dev]'
	@touch $@

setup: install
	$(BIN)/claude-graph setup

lint: install
	$(BIN)/ruff check . && $(BIN)/ruff format --check .

test: lint
	$(BIN)/pytest -q

# Re-resolve pyproject in a throwaway venv; the lock is replaced only by a non-empty freeze.
lock:
	@set -e; T=$$(mktemp -d); trap 'rm -rf "$$T"' EXIT; \
	$(PY) -m venv "$$T/venv"; "$$T/venv/bin/pip" install -q -e '.[dev]'; \
	"$$T/venv/bin/pip" freeze --exclude-editable > "$$T/raw"; sort "$$T/raw" > "$$T/pins"; \
	[ -s "$$T/pins" ] || { echo "pip freeze produced nothing; lock left untouched"; exit 1; }; \
	{ sed -n '1,3p' requirements.lock; cat "$$T/pins"; } > requirements.lock.new; \
	mv requirements.lock.new requirements.lock; echo "requirements.lock updated"
