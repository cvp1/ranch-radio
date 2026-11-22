### Changed

- Changelog entries now go in `changelog.d/<branch>.md`, one file per pull request, instead of the top of `CHANGELOG.md`; `tools/changelog_fold.py --apply` folds them into Unreleased. Parallel pull requests no longer conflict over the changelog (each conflict meant another CI run).
