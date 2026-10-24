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
5. Co-contributors merge their own pull request once CI is green (the
   `tests` check is required; no approval is needed). Use **Create a merge
   commit**, then delete the branch. Ask for a review when you want a
   second pair of eyes, especially for installers, CI, the safety tests,
   the web guard or station ID.

If GitHub says the branch conflicts with `main`, rebase onto `main` and
push again.

## Running the tests

```sh
python3 tests/public_hygiene_test.py   # quick, required
bash tests/run_all.sh                  # the full suite, one test at a time
```

The page tests need Chromium or Chrome (`CHROME=/path/to/chrome` if it isn't
on `PATH`). The rigctl contract tests need Hamlib's `rigctld`.

### Tests that pass on every machine

CI runs on small, busy runners, so a test that only passes on a fast laptop
fails there. Write tests that wait for what they check:

- **Wait on a condition, not a clock.** No `sleep` before a check that
  something happened: wait for it (`tests/testkit.py` has `wait_for`). Give
  that wait a generous ceiling; it returns as soon as the condition holds.
  A sleep is fine for a minimum the protocol needs, or for a quiet window
  before checking that nothing happened.
- **Make the fakes behave like the hardware.** Reply at wire speed, never
  queue replies without bound, and stop writing when stopped.
- **Wait for the server, not just the fake.** A reply the fake sent may not
  have been handled yet. Wait for something the server did in response: a
  frame, a log line, a page event.
- **Take ports from the OS** (`testkit.free_port()`), not from a random range.

To shake out timing bugs before CI does, run the suite in Linux containers
held to a CI runner's CPU budget (needs Docker):

```sh
bash tests/flake_hunt.sh                          # whole suite, 4 runners x 2 passes
ONLY="civ_safety_test" PASSES=4 bash tests/flake_hunt.sh   # one test, more passes
```

It prints failures per test. The bar is none.

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
