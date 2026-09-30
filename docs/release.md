# Releasing

A release is a version bump in `.claude-plugin/plugin.json`, reviewed as a pull request, then tagged
`adversarial-review-loop--v<version>` with a GitHub release. The tag shape is the one Claude Code's plugin dependency
resolver looks for and the one `claude plugin tag` produces (see `.cz.toml`).

## Cutting a release

1. Dispatch the **Release** workflow (`.github/workflows/release.yaml`) from `main`. Leave `version` empty to derive it
   from the Conventional Commits since the last release tag, or pass one (`3.1.0` or `v3.1.0`) to force it.
    - A derived version comes from `cz bump --get-next`; see the bump table in [`AGENTS.md`](../AGENTS.md#commits). With
      nothing but "none" commits since the last tag, the run fails with "nothing to bump".
    - An explicit version must be higher than the latest release.
2. The workflow commits the new version to `.claude-plugin/plugin.json` on a `release/v<version>` branch, opens a pull
   request, and dispatches `ci.yaml` on that branch, so the required checks report even when GitHub does not fire
   `pull_request` for a pull request opened with `GITHUB_TOKEN`.
3. Review the pull request and merge it like any other.
4. The merge is a push to `main` that changes the version, which `auto-tag-release.yaml` acts on (every other push exits
   at its first step). It tags the merge commit and creates the GitHub release with notes generated from the merged pull
   requests. A version lower than the highest release does not take GitHub's **Latest** badge.

## When something fails

- **The release workflow failed after pushing the branch.** Re-run it. A leftover `release/v<version>` branch without an
  open pull request is deleted and recreated; one with an open pull request stops the run, so merge or close that pull
  request first.
- **`auto-tag-release.yaml` failed.** Re-run the failed run. It creates the tag only if it is missing, and fails if the
  tag exists on a different commit (tags are immutable, see below); then it creates the release only if it is missing.

## Repository settings this depends on

- **Settings → Actions → General → Workflow permissions:** "Allow GitHub Actions to create and approve pull requests" is
  enabled, so the release workflow can open its pull request. It does not let the workflow merge anything.
- **Rulesets** (managed in [gh-leinardi-iac](https://github.com/leinardi/gh-leinardi-iac)): release tags are immutable,
  and the default branch requires the CI checks, so the bump pull request needs them green like any other.

After a release, a local marketplace install picks up skill changes only once the plugin cache is refreshed; see
[the install cache](../AGENTS.md#the-install-cache-and-what-it-means-for-iterating) in `AGENTS.md`.
