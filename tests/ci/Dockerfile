# A CI runner in a box, for tests/flake_hunt.sh: Linux, Python 3.14, Hamlib,
# Chromium, the benlink venv, a non-root user (Chromium's sandbox).
FROM python:3.14-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        chromium libhamlib-utils git procps rsync && rm -rf /var/lib/apt/lists/*
# Chromium in a container: no user-namespace sandbox, a small /dev/shm.
# CI's VM needs neither flag; this wrapper exists only in the image.
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage "$@"\n' \
        > /usr/local/bin/chromium-ci && chmod +x /usr/local/bin/chromium-ci
RUN useradd -m runner
USER runner
COPY --chown=runner requirements.txt /tmp/requirements.txt
RUN python -m venv /home/runner/venv && /home/runner/venv/bin/pip install -q -r /tmp/requirements.txt \
 && python -m venv /home/runner/.local/share/venvs/benlink \
 && /home/runner/.local/share/venvs/benlink/bin/pip install -q \
        "benlink @ git+https://github.com/khusmann/benlink@c4b9d22112402ea193d376f412bc028d1778fdb4"
ENV CHROME=/usr/local/bin/chromium-ci
