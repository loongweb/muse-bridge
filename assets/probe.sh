#!/usr/bin/env bash
# Run this INSIDE Muse's VM (or any other zero-inbound peer) and paste the whole
# output back to WorkBuddy. It answers ONE question: which outbound HTTPS
# destinations can this VM actually reach? Every VPS-free rendezvous design
# depends on the answer, and it has NOT been measured for Muse's Secure VM.
#
#   MB_EXTRA_DOMAINS='bridge.example.com api.example.com' bash probe.sh
#   MB_BRIDGE_URL='https://bridge.example.com' bash probe.sh   # also test health
#
# Read-only: only GET / on public origins. Never sends credentials.
set -uo pipefail

DEFAULT_DOMAINS=(
  "cloudflare.com"
  "github.com"
  "api.github.com"
  "raw.githubusercontent.com"
  "workers.dev"
  "pages.dev"
  "trycloudflare.com"
  "r2.cloudflarestorage.com"
  "onrender.com"
  "fly.dev"
  "railway.app"
  "koyeb.app"
)

printf 'muse-bridge outbound probe  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%-34s %-8s %-10s %s\n' "HOST" "HTTPS" "TIME(s)" "NOTE"
printf '%s\n' "-----------------------------------------------------------------------"

for d in "${DEFAULT_DOMAINS[@]}" ${MB_EXTRA_DOMAINS:-}; do
  [ -n "$d" ] || continue
  out="$(curl -sS -o /dev/null -w '%{http_code} %{time_total}' \
        --max-time 8 --connect-timeout 5 "https://$d/" 2>/dev/null)"
  if [ -z "$out" ]; then
    printf '%-34s %-8s %-10s %s\n' "$d" "BLOCKED" "-" "no response within 8s"
  else
    set -- $out
    code="$1"; secs="$2"
    case "$code" in
      000) verdict="BLOCKED"; note="connection failed" ;;
      4*|5*) verdict="REACH"; note="origin reachable (HTTP $code is fine)" ;;
      *) verdict="REACH"; note="" ;;
    esac
    printf '%-34s %-8s %-10s %s\n' "$d" "$verdict" "$secs" "$note"
  fi
done

if [ -n "${MB_BRIDGE_URL:-}" ]; then
  printf '%s\n' "-----------------------------------------------------------------------"
  h="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
      "${MB_BRIDGE_URL%/}/v2/health" 2>/dev/null)"
  printf 'rendezvous %s/v2/health -> %s\n' "${MB_BRIDGE_URL%/}" "${h:-BLOCKED}"
  [ "$h" = "200" ] \
    && echo "PASS: this VM can reach the rendezvous. A Bearer token is still required for data calls." \
    || echo "FAIL: fix the tunnel / hostname / firewall before building any client on it."
fi

printf '%s\n' "-----------------------------------------------------------------------"
echo "Interpretation:"
echo "  REACH on github.com / api.github.com  -> private-repo rendezvous is viable"
echo "  REACH on workers.dev or your own zone -> Cloudflare-hosted rendezvous is viable"
echo "  REACH on a container platform domain  -> free-container rendezvous is viable"
echo "  Only cloudflare.com reachable        -> use your OWN CF zone hostname (geo/category walls vary)"
echo "  All BLOCKED                          -> this VM has no usable outbound HTTPS; a VPS is unavoidable"
echo
echo "Paste this entire output back to WorkBuddy. Do NOT paste any token here."
