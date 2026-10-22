# Contributing to Ranch Radio

Changes come in as pull requests against `main`. Nobody pushes to `main`
directly except the maintainer's own tooling.

## The flow

1. Branch from the latest `main`, one branch per change:
   `git switch main && git pull && git switch -c short-description`
2. Make the change, with a test when it changes behavior (see `tests/`).
3. Run the tests locally (below), push your branch, and open a pull request.
   Say in a few lines what changed, why, and how you tested it. Mark it a
   draft until it's ready.
4. CI runs the full suite on every push to the pull request. It must be
   green before anything else happens.
5. The maintainer reviews and approves. Approved pull requests are merged
   automatically within about 15 minutes, after a final privacy check.
   **Don't press Merge yourself**; the merge has to go through that check.

Labels you may see:

| Label | Meaning |
|---|---|
| `quick-review` | CI is green and the change is small; waiting on the maintainer's approval |
| `careful-review` | It touches a sensitive area (installers, CI, safety tests, the web guard, station ID) or is large; it gets a closer look |
| `needs-rebase` | It conflicts with `main`: rebase onto `main` and push again |

A push after approval needs a fresh approval, so finish your edits first.

## Running the tests

```sh
python3 tests/public_hygiene_test.py   # quick, required
bash tests/run_all.sh                  # the full suite, one test at a time
```

The page tests need Chromium or Chrome (`CHROME=/path/to/chrome` if it isn't
on `PATH`). The rigctl contract tests need Hamlib's `rigctld`.

## Ground rules

- **No transmitting in tests.** The suite drives fake radios (`tests/fake_*.py`);
  keep it that way. Anything that changes how or when the radio keys up needs
  a test that proves it stays safe.
- **Nothing personal in the repo.** No real host files (yours live in
  `~/.config/ranch-radio/hosts/`, outside the checkout), home directories,
  LAN addresses other than the `192.168.1.x` examples, logs, packet captures
  or certificates. `tests/public_hygiene_test.py` checks for these.
- New dependencies go in `requirements.txt` with a reason in the pull request.
- Keep one concern per pull request; small ones get reviewed fastest.

By contributing you agree your work is released under the repository's MIT
license.
