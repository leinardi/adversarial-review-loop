# Releasing

A release is a version bump in `.claude-plugin/plugin.json`, reviewed as a pull request, then tagged
`adversarial-review-loop--v<version>` with a GitHub release. The tag shape is the one Claude Code's
plugin dependency resolver looks for and the one `claude plugin tag` produces. Both halves run in
`.github/workflows/release.yaml`, on gh-reusable-workflows' `version-bump-pr`, `release-on-merge`
and `simple-tag-and-release`.

## Cutting a release

1. Dispatch the **Release** workflow from `main`. Leave `version` empty to derive it from the
   Conventional Commits since the last release tag, or pass one (`3.1.0` or `v3.1.0`) to force it.
   Tick `dry_run` to see the version and the bump without pushing anything.
    - A derived version comes from svu; see the bump rules in [`AGENTS.md`](../AGENTS.md#commits).
      With nothing but "none" commits since the last tag, the run fails with "nothing to bump".
    - Any version must be higher than the one `plugin.json` carries.
2. The workflow runs `make release-bump VERSION=<version>`, commits the result as
   `github-actions[bot]` on a `release/adversarial-review-loop--v<version>` branch, opens a pull
   request, and dispatches `ci.yaml` on that branch, so the required checks report even though
   GitHub fires no `pull_request` event for a pull request opened with `GITHUB_TOKEN`.
3. Review the pull request and merge it with a merge commit once CI is green. Do not push to the
   branch or use "Update branch": to bring it up to date, dispatch the Release workflow again with
   the same version.
4. The merge is a push to `main` that changes `plugin.json`. The workflow checks that the merge is
   exactly that bump pull request, waits for CI on the merge commit, then tags it and creates the
   GitHub release with notes generated from the merged pull requests. A version lower than the
   highest release does not take GitHub's **Latest** badge. A hand edit of `plugin.json` that did
   not come from the bump pull request releases nothing.

## When something fails

Every step is idempotent: re-run the failed run, or dispatch again with the same version.

- **The bump failed after pushing the branch.** Dispatch again: an open bump pull request is
  reused or brought up to date, and a branch left without one gets a new pull request. A pull
  request on that branch opened by anyone else stops the run.
- **The release after the merge failed.** Re-run the failed run. The tag is created by publishing
  the release, the last step, and a re-run finishes what an earlier one started; a tag on another
  commit stops it (tags are immutable, see below).

## Repository settings this depends on

- **Settings → Actions → General → Workflow permissions:** "Allow GitHub Actions to create and
  approve pull requests" is enabled, so the release workflow can open its pull request. It does not
  let the workflow merge anything.
- **Rulesets** (managed in [gh-leinardi-iac](https://github.com/leinardi/gh-leinardi-iac)): release
  tags are immutable, and the default branch requires the CI checks, so the bump pull request needs
  them green like any other.

After a release, a local marketplace install picks up skill changes only once the plugin cache is
refreshed; see [the install cache](../AGENTS.md#the-install-cache-and-what-it-means-for-iterating)
in `AGENTS.md`.
