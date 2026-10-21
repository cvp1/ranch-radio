#!/usr/bin/env bash
# Trust a Radio Remote server's certificate on this Mac and open it in Brave,
# ready to install as an app (PWA).
#
#   bash macos/install-webapp.sh [host] [cert.pem]
#     host  name in the URL (default: this Mac's name)
#     cert  the server's certificate (default: certs/cert.pem, i.e. this Mac's
#           own server). For another server, copy its certs/cert.pem first:
#             scp linux-station:ranch-radio/certs/cert.pem certs/linux-station.pem
#             bash macos/install-webapp.sh linux-station certs/linux-station.pem
#
# Brave only offers "Install" on a site whose certificate it trusts; clicking
# through the warning isn't enough. Two rules, both learned 2026-10-14:
# - The trust entry carries NO host name (-s). Chromium (so Brave) skips any
#   trust setting with a policy string (net/cert/internal/trust_store_mac.cc),
#   so a host-scoped entry -- what Safari's "visit this website" adds --
#   satisfies macOS's verify-cert but not Brave.
# - The certificate must NOT be a CA. Without -s, a trusted CA's key could mint
#   a certificate for any site; a CA:FALSE cert only vouches for its own names.
#   Servers made CA:TRUE certs before 2026-10-14; those are refused.
#
# Entries are found by SHA-1 fingerprint (every server's cert has the same
# common name). Idempotent. Each trust change opens a macOS password prompt.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:-$(hostname -s | tr "[:upper:]" "[:lower:]")}"
CERT="${2:-certs/cert.pem}"
KC="$HOME/Library/Keychains/login.keychain-db"
URL="https://$HOST:7300/"

[[ -f $CERT ]] || { echo "no $CERT"; exit 1; }
if openssl x509 -in "$CERT" -noout -ext basicConstraints | grep -q "CA:TRUE"; then
  echo "$CERT is a CA certificate; refusing to trust it. Give the server a CA:FALSE cert first."
  exit 1
fi
openssl x509 -in "$CERT" -noout -ext subjectAltName | grep -q "DNS:$HOST\b" \
  || { echo "$CERT has no name $HOST; restart that server with --tls-name $HOST"; exit 1; }

fp=$(openssl x509 -in "$CERT" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)

# This cert's user trust entry: "none", "scoped" (Brave ignores it) or "ok".
entry_state() {
  local d; d=$(mktemp -d)
  security trust-settings-export "$d/ts.plist" >/dev/null 2>&1 || { echo none; return; }
  python3 -I - "$d/ts.plist" "$fp" <<'EOF'
import plistlib, sys
trust = plistlib.load(open(sys.argv[1], "rb")).get("trustList", {})
e = trust.get(sys.argv[2])
if e is None:
    print("none")
elif any("kSecTrustSettingsPolicyString" in s for s in e.get("trustSettings", [])):
    print("scoped")
else:
    print("ok")
EOF
}

state=$(entry_state)
if [[ $state == ok ]] && security verify-cert -q -c "$CERT" -p ssl -s "$HOST" >/dev/null 2>&1; then
  echo "trust: already trusted (no host scope) for $HOST"
else
  if [[ $state != none ]]; then
    echo "trust: replacing an entry Brave ignores -- approve the macOS prompt"
    security remove-trusted-cert "$CERT"
  fi
  echo "trust: adding (SSL) -- approve the macOS prompt"
  security add-trusted-cert -r trustRoot -p ssl -k "$KC" "$CERT"
  security verify-cert -q -c "$CERT" -p ssl -s "$HOST" >/dev/null 2>&1 \
    || { echo "trust: macOS still doesn't trust it for $HOST"; exit 1; }
  [[ $(entry_state) == ok ]] || { echo "trust: entry still host-scoped; Brave will ignore it"; exit 1; }
  echo "trust: ok (sha1 $fp)"
fi

open -a "Brave Browser" "$URL"
cat <<EOF
Brave is on $URL. Quit Brave fully (Cmd-Q) and reopen it if it still says
Not Secure: it keeps its old view of the certificate until restarted. Then
install: the install icon at the right of the address bar, or
menu > Save and share > Install page as app.
EOF
