ifndef MK_LOCAL_TEST_INCLUDED
MK_LOCAL_TEST_INCLUDED := 1

PYTHON ?= python3

# Worker count for the unit tests. `auto` is one per core; set PYTEST_WORKERS=1 to debug a
# failure without workers reordering the output.
PYTEST_WORKERS ?= auto

# uv runs the *tests* when it is installed: it resolves requirements-dev.txt into a cached
# environment with no virtualenv to manage, and it works on distributions where pip refuses
# to touch the system interpreter (PEP 668). It is a developer convenience only -- uv is
# deliberately **not** on the runtime path, because `uv run` reads `pyproject.toml` and
# `.python-version` from the current directory, which under a hook is the repository under
# review. See AGENTS.md, "Why the gate does not run under uv".
#
# An explicit PYTHON= on the command line wins, and so does ARL_NO_UV=1.
UV := $(shell command -v uv 2>/dev/null)
ifeq ($(origin PYTHON),command line)
UV :=
endif
ifneq ($(strip $(ARL_NO_UV)),)
UV :=
endif

UV_RUN = $(UV) run --no-project --with-requirements $(REPO_ROOT)/requirements-dev.txt python

.PHONY: dev-deps
dev-deps: ## Install the pinned Python development dependencies (pytest, pytest-xdist)
ifneq ($(strip $(UV)),)
	@$(UV_RUN) -c 'import pytest, xdist'
	@printf 'arl: dev dependencies ready in uv'"'"'s cache; make test uses them automatically.\n'
else
	@$(PYTHON) -m pip install -r $(REPO_ROOT)/requirements-dev.txt
endif

.PHONY: test
test: test-unit test-accept ## Run the unit tests and the arl selftest (no model is called)

.PHONY: test-unit
test-unit: ## Run the Python unit tests
ifneq ($(strip $(UV)),)
	@$(UV_RUN) -m pytest -n $(PYTEST_WORKERS)
else
	@$(PYTHON) -c 'import pytest' 2>/dev/null || { \
	  printf 'arl: %s cannot import pytest, so either the package or the interpreter itself is missing.\n' '$(PYTHON)' >&2; \
	  printf 'arl: install the pinned dev dependencies with: make dev-deps\n' >&2; \
	  exit 1; \
	}
	@if $(PYTHON) -c 'import xdist' 2>/dev/null; then \
	  $(PYTHON) -m pytest -n $(PYTEST_WORKERS); \
	else \
	  printf 'arl: pytest-xdist is not installed, so the unit tests run serially (several times slower).\n' >&2; \
	  printf 'arl: install it with: make dev-deps\n' >&2; \
	  $(PYTHON) -m pytest; \
	fi
endif

.PHONY: test-accept
# The selftest shards itself across the cores by default; ARL_SELFTEST_JOBS=1 runs it
# straight through with its output unbuffered, which is what you want when a section fails.
test-accept: ## Run the arl selftest against scratch repositories (no model is called)
	@$(REPO_ROOT)/tests/selftest-parallel.sh

.PHONY: test-filter
test-filter: ## Run only the selftest sections matching FILTER=<substring>, serially
	@$(REPO_ROOT)/tests/selftest.sh "$(FILTER)"

.PHONY: dry-run
dry-run: ## Print the exact reviewer invocation for the current worktree, without calling it
	@$(REPO_ROOT)/scripts/arl.sh dry-run

endif
