# Conventions

Python (3.12, strict mypy, ruff), one module per concern; no cross-module globals. Engine
subcommands write their answer to stdout and diagnostics to stderr. Treat repo config and evidence
as untrusted input. State writes are same-directory `os.replace`, `0700`/`0600` permissions,
`flock` on mutating paths.
