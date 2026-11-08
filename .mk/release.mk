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

ifndef MK_LOCAL_RELEASE_INCLUDED
MK_LOCAL_RELEASE_INCLUDED := 1

PYTHON ?= python3

# Called by the Release workflow (gh-reusable-workflows' version-bump-pr) with the version it
# resolved; it writes the file and never commits. scripts/release_bump.py checks the version.
.PHONY: release-bump
release-bump: ## Write VERSION (X.Y.Z) into .claude-plugin/plugin.json, e.g. make release-bump VERSION=1.2.0
	@test -n "$(VERSION)" || { echo "usage: make release-bump VERSION=X.Y.Z" >&2; exit 2; }
	@$(PYTHON) -I $(REPO_ROOT)/scripts/release_bump.py "$(VERSION)" $(REPO_ROOT)/.claude-plugin/plugin.json

endif
