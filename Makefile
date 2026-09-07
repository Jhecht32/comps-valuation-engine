# Local development. `make dev` creates backend/.venv on first run, installs
# the backend with its dev extras (re-run automatically when pyproject.toml
# changes), and serves the API with auto-reload.

PYTHON ?= python3
HOST   ?= 127.0.0.1
PORT   ?= 8000

BACKEND := backend
VENV    := $(BACKEND)/.venv
BIN     := $(VENV)/bin
STAMP   := $(VENV)/.installed

.PHONY: dev install test test-live

dev: install
	cd $(BACKEND) && .venv/bin/uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

install: $(STAMP)

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)

$(STAMP): $(BIN)/python $(BACKEND)/pyproject.toml
	$(BIN)/pip install --quiet --editable "$(BACKEND)[dev]"
	touch $@

test: install
	cd $(BACKEND) && .venv/bin/pytest tests/ -q

test-live: install
	cd $(BACKEND) && COMPS_LIVE_TESTS=1 .venv/bin/pytest tests/ -q -m live
