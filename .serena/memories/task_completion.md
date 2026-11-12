# Task Completion

For runtime/Python changes: `git add -N` new files, then `make test` and `make check`; inspect
`git status --short` afterward because check may fix files. Behaviour changes need a test that
fails on the old code. Mod and host integration changes also need the relevant `tests/STEP0.md`
live checks.
