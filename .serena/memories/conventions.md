# Conventions

Python (3.12, strict mypy, ruff) keeps functional command modules, one per concern; no cross-module globals. Hot path: read-only tool check before config/state load; in-process JSON, no per-field subprocesses. Hook commands must print valid response JSON or intentional silence only; diagnostics go stderr. Treat repo config and evidence as hostile. State writes are same-directory `os.replace`, `0700`/`0600` permissions, `flock` on mutating paths.
