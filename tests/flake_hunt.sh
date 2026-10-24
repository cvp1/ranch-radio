#!/usr/bin/env bash
# Hunt flaky tests: run the suite many times in parallel Linux containers,
# each held to a CI runner's CPU budget, and count failures per test.
#   bash tests/flake_hunt.sh                  4 runners x 2 passes, 2 CPUs each
#   RUNNERS=4 PASSES=3 CPUS=2 MEMORY=1800m bash tests/flake_hunt.sh
#   ONLY="civ_safety_test opengd77_codeplug_test" bash tests/flake_hunt.sh
# ONLY picks tests by skipping the rest (RR_SKIP); it is NOT passed into the
# container, where some tests read ONLY as their own scenario filter and a
# test name matches no scenario: they ran nothing and said PASS (2026-10-23).
# Needs Docker. The checkout is mounted read-only and copied into each
# container, so nothing here touches the working tree. Exit 0 only when
# every pass of every test passed: that, not one green CI run, is the bar.
set -u
cd "$(dirname "$0")/.."
RUNNERS="${RUNNERS:-4}"; PASSES="${PASSES:-2}"; CPUS="${CPUS:-2}"; MEMORY="${MEMORY:-1800m}"
OUT="${OUT:-/tmp/rr-flake-hunt}"; IMAGE=ranch-radio-ci
rm -rf "$OUT"; mkdir -p "$OUT"
docker build -q -t "$IMAGE" -f tests/ci/Dockerfile . >/dev/null || { echo "image build failed"; exit 2; }
names=""
if [[ -n "${ONLY:-}" ]]; then
    for n in $(cd tests && ls *_test.py | sed 's/\.py$//') civ_safety_ic7300 winlink_lane_g90 rigctl_contract rigctl_contract_g90; do
        [[ " $ONLY " == *" $n "* ]] || names+=" $n"
    done
fi
for r in $(seq "$RUNNERS"); do
    docker run --rm --cpus "$CPUS" --memory "$MEMORY" -v "$PWD":/src:ro -v "$OUT":/out \
        -e PASSES="$PASSES" -e RUNNER="$r" -e RR_SKIP="${names} ${RR_SKIP:-}" "$IMAGE" bash -c '
        cp -r /src ~/work && rm -rf ~/work/.venv && ln -s ~/venv ~/work/.venv && cd ~/work
        for p in $(seq "$PASSES"); do
            LOGDIR=/out/r$RUNNER-p$p bash tests/run_all.sh > /out/r$RUNNER-p$p.txt 2>&1
        done' &
done
wait
echo "== $RUNNERS runners x $PASSES passes, $CPUS CPUs each (logs in $OUT)"
cat "$OUT"/r*-p*.txt | grep -E '^(PASS|FAIL) ' | awk '{n[$2]++; if ($1=="FAIL") f[$2]++}
    END {for (t in n) printf "%3d/%-3d %s\n", f[t]+0, n[t], t}' | sort -rn | awk '$1 !~ /^ *0\//' > "$OUT/failures.txt"
runs=$(cat "$OUT"/r*-p*.txt | grep -c '^[0-9]* passed')
echo "complete passes: $runs of $((RUNNERS * PASSES))"
if [[ -s "$OUT/failures.txt" ]]; then echo "failures (failed/runs):"; cat "$OUT/failures.txt"; exit 1; fi
[[ $runs -eq $((RUNNERS * PASSES)) ]] || { echo "some passes did not finish"; exit 1; }
echo "no failures"
