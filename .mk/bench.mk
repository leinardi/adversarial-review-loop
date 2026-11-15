# This file is part of adversarial-review-loop.
#
# Copyright (c) 2026 Roberto Leinardi
#
# adversarial-review-loop is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# adversarial-review-loop is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with adversarial-review-loop.  If not, see <http://www.gnu.org/licenses/>.
#

ifndef MK_LOCAL_BENCH_INCLUDED
MK_LOCAL_BENCH_INCLUDED := 1

PYTHON ?= python3

# The reviewer benchmark (tests/bench/README.md). It calls real models and costs money, so it
# is never part of `make test` or CI. Cases and runs live under ARL_BENCH_DIR, never here.
BENCH_PREFIX ?= baseline
BENCH_JOBS ?= 4
# Extra arguments for bench.py matrix, such as --replace or --runs 3.
BENCH_ARGS ?=

# -B: the harness must not leave __pycache__ inside the checkout it benchmarks.
.PHONY: bench-baseline
bench-baseline: ## Run the 8-cell round-1 bench matrix on this checkout (CALLS MODELS, costs money)
	@$(PYTHON) -B $(REPO_ROOT)/tests/bench/bench.py matrix --prefix $(BENCH_PREFIX) --jobs $(BENCH_JOBS) $(BENCH_ARGS)

endif
