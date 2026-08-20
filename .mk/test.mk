ifndef MK_LOCAL_TEST_INCLUDED
MK_LOCAL_TEST_INCLUDED := 1

PYTHON ?= python3

.PHONY: dev-deps
dev-deps: ## Install the pinned Python development dependencies (pytest)
	@$(PYTHON) -m pip install -r $(REPO_ROOT)/requirements-dev.txt

.PHONY: test
test: test-unit test-accept ## Run the unit tests and the ocrl selftest (no model is called)

.PHONY: test-unit
test-unit: ## Run the Python unit tests
	@$(PYTHON) -c 'import pytest' 2>/dev/null || { \
	  printf 'ocrl: %s cannot import pytest, so either the package or the interpreter itself is missing.\n' '$(PYTHON)' >&2; \
	  printf 'ocrl: install the pinned dev dependencies with: make dev-deps\n' >&2; \
	  exit 1; \
	}
	@$(PYTHON) -m pytest

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
