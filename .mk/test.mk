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
# An explicit PYTHON= on the command line wins, and so does OCRL_NO_UV=1.
UV := $(shell command -v uv 2>/dev/null)
ifeq ($(origin PYTHON),command line)
UV :=
endif
ifneq ($(strip $(OCRL_NO_UV)),)
UV :=
endif

UV_RUN = $(UV) run --no-project --with-requirements $(REPO_ROOT)/requirements-dev.txt python

.PHONY: dev-deps
dev-deps: ## Install the pinned Python development dependencies (pytest, pytest-xdist)
ifneq ($(strip $(UV)),)
	@$(UV_RUN) -c 'import pytest, xdist'
	@printf 'ocrl: dev dependencies ready in uv'"'"'s cache; make test uses them automatically.\n'
else
	@$(PYTHON) -m pip install -r $(REPO_ROOT)/requirements-dev.txt
endif

.PHONY: test
test: test-unit test-accept ## Run the unit tests and the ocrl selftest (no model is called)

.PHONY: test-unit
test-unit: ## Run the Python unit tests
ifneq ($(strip $(UV)),)
	@$(UV_RUN) -m pytest -n $(PYTEST_WORKERS)
else
	@$(PYTHON) -c 'import pytest' 2>/dev/null || { \
	  printf 'ocrl: %s cannot import pytest, so either the package or the interpreter itself is missing.\n' '$(PYTHON)' >&2; \
	  printf 'ocrl: install the pinned dev dependencies with: make dev-deps\n' >&2; \
	  exit 1; \
	}
	@if $(PYTHON) -c 'import xdist' 2>/dev/null; then \
	  $(PYTHON) -m pytest -n $(PYTEST_WORKERS); \
	else \
	  printf 'ocrl: pytest-xdist is not installed, so the unit tests run serially (several times slower).\n' >&2; \
	  printf 'ocrl: install it with: make dev-deps\n' >&2; \
	  $(PYTHON) -m pytest; \
	fi
endif

.PHONY: test-accept
test-accept: ## Run the ocrl selftest against scratch repositories (no model is called)
	@$(REPO_ROOT)/tests/selftest.sh

.PHONY: test-filter
test-filter: ## Run only the selftest sections matching FILTER=<substring>
	@$(REPO_ROOT)/tests/selftest.sh "$(FILTER)"

.PHONY: dry-run
dry-run: ## Print the exact opencode invocation for the current worktree, without calling it
	@$(REPO_ROOT)/scripts/ocrl.sh dry-run

endif
