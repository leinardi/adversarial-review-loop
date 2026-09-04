# Tech Stack

Runtime: Python 3.12 stdlib; guard shim is Bash 3.2+ with an outer watchdog (`timeout`/`gtimeout`, else `perl`) — no GNU coreutils requirement, so stock macOS works. Tests: pytest pinned in `requirements-dev.txt` plus Bash acceptance `tests/selftest.sh`. Build entry points: Make targets in `.mk/`; lint/type via pre-commit, ruff, mypy, shellcheck.
