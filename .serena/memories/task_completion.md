# Task Completion

For runtime/Python changes: `git add -N` new files, then `make test` and `make check`; inspect `git status --short` afterward because check may fix files. Gate changes need end-to-end regression demonstrating old implementation failure. Hook/skill integration changes also require relevant `tests/STEP0.md` manual checks.
