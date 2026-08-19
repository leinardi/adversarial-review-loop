ifndef MK_LOCAL_TEST_INCLUDED
MK_LOCAL_TEST_INCLUDED := 1

.PHONY: test
test: ## Run the ocrl selftest against scratch repositories (no model is called)
	@$(REPO_ROOT)/tests/selftest.sh

.PHONY: test-filter
test-filter: ## Run only the selftest sections matching FILTER=<substring>
	@$(REPO_ROOT)/tests/selftest.sh "$(FILTER)"

.PHONY: dry-run
dry-run: ## Print the exact opencode invocation for the current worktree, without calling it
	@$(REPO_ROOT)/scripts/ocrl.sh dry-run

endif
