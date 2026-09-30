# Contributing

Read [`AGENTS.md`](AGENTS.md) before changing anything: it holds the five rules a change must not break and the index of
invariants behind them. It applies equally to human and AI-assisted contributions.

## Setup

Install [`pre-commit`](https://pre-commit.com/), then install the hooks once with `make pre-commit-install`. It installs both the
`pre-commit` and the `commit-msg` hooks, so commit messages are checked when you commit, not when the release runs.

```console
make dev-deps     # the pinned development dependencies, once per checkout
make test         # the pytest suite and the shim selftest; no model is called
make check        # pre-commit on every file
make check-stage  # pre-commit on the staged files only
```

`make help` lists every target. The tests need `jq`; see [Development](README.md#-development) for what each suite covers.

`make check` only sees files git already knows about, so run `git add -N` on a new file before it.

With the plugin enabled, its hooks run in every Claude Code session. When developing this repository, disable it in
`.claude/settings.local.json` and load the working tree with `claude --plugin-dir .` only when you mean to use the loop.

## Pull requests

Every change lands through a pull request against `main` with the required CI checks green. A change to the gate needs a test
that fails on the old code.

## Commit messages

All commits must follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) with a scope:
`<type>(<scope>)[!]: <description>`. The `conventional-pre-commit` hook enforces this on `commit-msg`, and the
`conventional-commits` CI job checks it again on every pull request. Release notes are not built from these messages:
`gh release create --generate-notes` lists the merged pull requests by title.

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `style`, `revert`. Use a lower-case,
imperative description:

```text
fix(cmdshape): reject git commit --only
feat(harness): add a reviewer harness
feat(config)!: rename the block_severity key
```

Mark breaking changes with `!` before the colon or a `BREAKING CHANGE: <description>` footer.

## Versioning

A release with no explicit version is derived from these types by commitizen: since the last release, any `feat` makes the next
release a minor, any `fix`, `perf` or `refactor` a patch, and `!` or a `BREAKING CHANGE:` footer a major; `build`, `chore`,
`ci`, `docs`, `revert`, `style` and `test` bump nothing. See [`docs/release.md`](docs/release.md).

Pull requests are merged with merge commits; squash and rebase merging are disabled. Every commit in a pull request therefore lands
on `main` as it is and counts toward the version, so each commit needs a correct type, not just the pull request as a whole.
