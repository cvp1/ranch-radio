# changelog.d: one file per pull request

A pull request adds its CHANGELOG entry here, as `changelog.d/<branch>.md`,
and does not edit `CHANGELOG.md`. Every PR adding a line at the top of the
same Unreleased section made each merge conflict with every other open PR,
and each conflict cost a fresh CI run (2026-11-21: one PR ran CI four times).
Files with different names never conflict.

A fragment is one or more sections, each a Keep a Changelog heading and its
bullets:

```markdown
### Added

- The JS8 tab shows a waterfall of the engine's audio.

### Fixed

- ...
```

Headings: `### Added`, `### Changed`, `### Fixed`, `### Security`,
`### Removed`, `### Deprecated`. Anything else is refused.

`python3 tools/changelog_fold.py` shows what folding would do; `--apply`
moves every fragment's bullets to the top of its section under
`## [Unreleased]` in `CHANGELOG.md` (making the section if it isn't there)
and removes the folded fragments with `git rm`. Fold before a release, or
whenever; it is one commit, and nobody else edits those lines.
