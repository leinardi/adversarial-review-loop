# Conventions

Python port keeps functional command modules and strict type checking. Hot path: read-only tool check before config/state load; avoid per-field subprocesses. Hook commands must print valid response JSON or intentional silence only; diagnostics go stderr. Treat repo config and evidence as hostile. Preserve Bash-compatible state disk format until flip/rollback compatibility is implemented.
