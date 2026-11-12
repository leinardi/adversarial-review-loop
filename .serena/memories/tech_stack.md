# Tech Stack

Runtime: Python 3.12 stdlib, launched by the Bash script `scripts/arl.sh`; a Claude Code mod
(TypeScript) from phase 4. Tests: pytest pinned in `requirements-dev.txt` plus the `arl.sh` smoke
test `tests/selftest.sh`. Build entry points: Make targets in `.mk/`; lint/type via pre-commit,
ruff, mypy, shellcheck, eslint, tsc.
