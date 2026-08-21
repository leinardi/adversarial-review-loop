# Tech Stack

Runtime: Python 3.12 stdlib during additive port; current live gate Bash 4.4+ with GNU coreutils. Tests: pytest pinned in `requirements-dev.txt` plus Bash acceptance `tests/selftest.sh`. Build entry points: Make targets in `.mk/`; lint/type via pre-commit, ruff, mypy, shellcheck.
