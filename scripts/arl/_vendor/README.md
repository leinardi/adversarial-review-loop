# Vendored third-party code

## bashlex

|  |  |
| --- | --- |
| Upstream | <https://github.com/idank/bashlex> |
| Version | 0.18 |
| Commit | `ae1e11a8227d7ca8531b94c7fe821b83bd714ca5` (tag `0.18`) |
| Source | `bashlex-0.18.tar.gz` from PyPI |
| sdist SHA-256 | `5bb03a01c6d5676338c36fd1028009c8ad07e7d61d8a1ce3f513b7fff52796ee` |
| Licence | GPL-3.0-or-later — see `bashlex/LICENSE` |

`scripts/arl/cmdshape.py` uses it to decide the shape of a command that wants to create a
commit. It is a real bash parser, pure Python and dependency-free, which is why it can be
vendored at all: the plugin has to work straight from a checkout, on arm and x86, with no
install step and no network.

Vendoring a GPL-3.0 library is why this repository is GPL-3.0-or-later.

### The one change made to it

**This vendored copy of bashlex was modified on 2026-08-21 by the adversarial-review-loop
authors.** That notice is required by GPLv3 §5(a), which asks a modified work to say that it
was modified and to give a relevant date. The modification is the import rewrite below, and
nothing else; any future change to this directory must update the date here.

Every `from bashlex import …` was rewritten to `from arl._vendor.bashlex import …`:

```bash
sed -i -E 's/^(\s*)from bashlex import /\1from arl._vendor.bashlex import /' bashlex/*.py
```

Upstream imports itself by absolute name, which would only resolve if `bashlex` were a
top-level package. Putting this directory on `sys.path` instead would create a top-level
`bashlex` name that a system-installed copy could satisfy first, and the point of vendoring
is to know exactly which parser the gate is running. Nothing else is modified — no
behaviour, no formatting — so a diff against the sdist shows exactly those lines.

The Python lint, type and format hooks all exclude this directory
(`.pre-commit-config.yaml`, `.ruff.toml`, `mypy.ini`), so the tree stays diffable against
upstream instead of drifting into this repository's house style.

### Notes for whoever updates it

- **`bashlex/parsetab.py` is dead weight, deliberately kept.** This copy of PLY has no
  table-reading or table-writing code at all, so the LALR tables are built at import time
  (~55 ms) and that file is never read. It ships anyway so the vendored tree diffs clean
  against the sdist. Do not "fix" the missing cache by pointing PLY at it: a PLY that writes
  tables would write them *into this directory*, which is inside the repository the gate
  reviews, and Rule 3 says nothing is written there. `tests/unit/test_vendor.py` asserts
  that parsing creates no files here.
- Re-run `tests/unit/test_cmdshape.py` and `tests/selftest.sh` after any bump. The command
  corpus is the contract, not bashlex's own test suite.
