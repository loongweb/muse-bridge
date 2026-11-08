#!/usr/bin/env bash
# muse-bridge HTTP client for the PEER side (Muse). Self-contained: bash + curl
# + coreutils only. No python, no ssh, no install inside the Secure VM.
#
#   export MB_URL='https://bridge.example.com'
#   export MB_TOKEN_FILE='/path/to/0600-token'   # never put the token in argv
#   export MB_ROOT='/srv/muse-bridge/mail'       # must equal BRIDGE.json
#   export MB_ME='muse'  MB_PEER='workbuddy'
#   bash mb-http.sh inbox | read <id> | attach <id> | ack <id> | ls | doctor
#   printf 'summary\nbody...\n' | bash mb-http.sh send workbuddy muse [attach_dir]
#
# Same contract as mb.sh: reading NEVER consumes; only ack does.
set -euo pipefail

: "${MB_URL:?set MB_URL to the https rendezvous origin}"
: "${MB_TOKEN_FILE:?set MB_TOKEN_FILE to your 0600 token file}"
MB_ROOT="${MB_ROOT:-/srv/muse-bridge/mail}"
MB_ME="${MB_ME:-muse}"
MB_PEER="${MB_PEER:-workbuddy}"
MB_OUT="${MB_OUT:-./mb-downloads}"

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }
[ -r "$MB_TOKEN_FILE" ] || { echo "token file unreadable: $MB_TOKEN_FILE" >&2; exit 2; }
case "$MB_URL" in https://*) ;; http://127.0.0.1*|http://localhost*) ;; *) echo "refusing plaintext remote MB_URL" >&2; exit 2;; esac
MB_URL="${MB_URL%/}"

CFG="$(mktemp "${TMPDIR:-/tmp}/mbhttp.XXXXXX")"
trap 'rm -f "$CFG"' EXIT
chmod 600 "$CFG"
# Build curl's config from the token FILE stream: the secret never enters argv.
{ printf 'header = "Authorization: Bearer '; tr -d '\r\n' < "$MB_TOKEN_FILE"; printf '"\n'; } > "$CFG"

enc() { printf '%s' "$1" | sed 's|/|%2F|g'; }
hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else openssl dgst -sha256 "$1" | sed 's/.*= //'; fi
}
check_id() {
  case "$1" in
    */*|*..*|*" "*) echo "invalid message id: $1" >&2; exit 2 ;;
  esac
  [ -n "$1" ] || { echo "empty message id" >&2; exit 2; }
}

# req METHOD "route?query" [data_file] [out_file]
# returns 0=200, 2=404, 1=other failure. Body always goes to a FILE.
req() {
  local method="$1" route="$2" data="${3:-}" out="${4:-/dev/null}" code
  local -a a=(-sS -K "$CFG" -X "$method" -o "$out" -w '%{http_code}')
  [ -n "$data" ] && a+=(--data-binary "@$data")
  code="$(curl "${a[@]}" "$MB_URL$route" 2>/dev/null)" || {
    echo "curl failed for $route" >&2; return 1; }
  case "$code" in
    200) return 0 ;;
    401) echo "unauthorized: check MB_TOKEN_FILE" >&2; return 1 ;;
    404) return 2 ;;
    *)   echo "http $code for $route" >&2; return 1 ;;
  esac
}

# list names in one directory; the server returns {"items":["a","b"]}
api_list() {
  local p="$1" tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/mblist.XXXXXX")"
  if ! req GET "/v2/list?p=$(enc "$p")" "" "$tmp"; then rm -f "$tmp"; return 1; fi
  tr -d '\n' < "$tmp" \
    | sed -n 's/.*"items"[[:space:]]*:[[:space:]]*\[\(.*\)\].*/\1/p' \
    | tr ',' '\n' | sed 's/^[[:space:]]*"//; s/"[[:space:]]*$//' \
    | sed '/^$/d'
  rm -f "$tmp"
}
json_ok() {
  local f="$1" code
  code="$(tr -d '\n' < "$f")"
  printf '%s' "$code" | grep -q '"ok"[[:space:]]*:[[:space:]]*true'
}
ids() { api_list "$MB_ROOT/$1" 2>/dev/null | grep '\.md$' | sed 's/\.md$//' | sort || true; }

CMD="${1:-help}"; shift || true
case "$CMD" in
  inbox)
    who="${1:-$MB_ME}"; ids "$who"
    ;;
  read)
    id="${1:?usage: read <id> [who]}"; who="${2:-$MB_ME}"; check_id "$id"
    tmp="$(mktemp "${TMPDIR:-/tmp}/mbread.XXXXXX")"
    req GET "/v2/read?p=$(enc "$MB_ROOT/$who/$id.md")" "" "$tmp" || {
      rm -f "$tmp"; echo "message not found: $id" >&2; exit 1; }
    cat "$tmp"; rm -f "$tmp"
    ;;
  attach)
    id="${1:?usage: attach <id> [who]}"; who="${2:-$MB_ME}"; check_id "$id"
    mkdir -p "$MB_OUT/$id"; found=0
    for n in $(api_list "$MB_ROOT/$who/attachments/$id" 2>/dev/null || true); do
      req GET "/v2/read?p=$(enc "$MB_ROOT/$who/attachments/$id/$n")" "" "$MB_OUT/$id/$n" || continue
      printf '%s  %s\n' "$(hash_of "$MB_OUT/$id/$n")" "$MB_OUT/$id/$n"
      found=1
    done
    [ "$found" = 1 ] || echo "no attachments on $id (downloads at $MB_OUT/$id)"
    ;;
  ack)
    id="${1:?usage: ack <id> [who]}"; who="${2:-$MB_ME}"; check_id "$id"
    tmp="$(mktemp "${TMPDIR:-/tmp}/mback.XXXXXX")"
    req POST "/v2/claim?src=$(enc "$MB_ROOT/$who/$id.md")&dst=$(enc "$MB_ROOT/$who/done/$id.md")" "" "$tmp" || {
      rm -f "$tmp"; echo "claim failed: $id" >&2; exit 1; }
    if ! json_ok "$tmp"; then rm -f "$tmp"; echo "not in inbox (already acked or missing): $id" >&2; exit 1; fi
    rm -f "$tmp"
    # Best-effort archive of attachments; failure must not undo the ack.
    req POST "/v2/move-tree?src=$(enc "$MB_ROOT/$who/attachments/$id")&dst=$(enc "$MB_ROOT/$who/done/attachments/$id")" "" /dev/null || true
    echo "acked $id"
    ;;
  send)
    to="${1:?usage: send <to> <from> [attach_dir] < body}"
    from="${2:?usage: send <to> <from> [attach_dir] < body}"
    adir="${3:-}"
    id="$(date +%s)000-$from-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    manifest=""
    if [ -n "$adir" ] && [ -d "$adir" ]; then
      for f in "$adir"/*; do
        [ -f "$f" ] || continue
        n="$(basename "$f")"
        case "$n" in */*|*..*|*" "*) echo "skipping unsafe attachment name: $n" >&2; continue;; esac
        req PUT "/v2/write?p=$(enc "$MB_ROOT/$to/attachments/$id/$n")" "$f" /dev/null || exit 1
        h="$(hash_of "$f")"; s="$(wc -c < "$f" | tr -d ' ')"
        [ -z "$manifest" ] || manifest="$manifest,"
        manifest="$manifest{\"name\":\"$n\",\"sha256\":\"$h\",\"size\":$s}"
      done
      [ -z "$manifest" ] || manifest="[$manifest]"
    fi
    tmp="$(mktemp "${TMPDIR:-/tmp}/mbsend.XXXXXX")"
    {
      echo "Id: $id"; echo "From: $from"; echo "To: $to"
      echo "Ts: $(date +%Y-%m-%dT%H:%M:%S%z)"
      echo "X-Bridge-Version: 2"; echo "X-Kind: task"
      [ -z "$manifest" ] || echo "X-Attachments: $manifest"
      echo
      cat
    } > "$tmp"
    req PUT "/v2/write?p=$(enc "$MB_ROOT/$to/$id.md")" "$tmp" /dev/null || {
      rm -f "$tmp"; echo "message commit failed; attachments may be orphaned" >&2; exit 1; }
    rm -f "$tmp"
    echo "$id"
    ;;
  ls)
    for who in "$MB_ME" "$MB_PEER"; do
      printf '%-12s unacked=%-4s done=%s\n' "$who" \
        "$(ids "$who" | wc -l | tr -d ' ')" \
        "$(api_list "$MB_ROOT/$who/done" 2>/dev/null | grep -c '\.md$' || true)"
    done
    ;;
  doctor)
    tmp="$(mktemp "${TMPDIR:-/tmp}/mbdoc.XXXXXX")"
    if req GET "/v2/read?p=$(enc "$MB_ROOT/BRIDGE.json")" "" "$tmp"; then
      if grep -q "\"mail_root\"[[:space:]]*:[[:space:]]*\"$MB_ROOT\"" "$tmp"; then
        echo "PASS: BRIDGE.json mail_root matches $MB_ROOT"
      else
        echo "FAIL: BRIDGE.json mail_root differs from MB_ROOT=$MB_ROOT"; grep mail_root "$tmp"
      fi
    else
      echo "FAIL: cannot read BRIDGE.json (wrong MB_ROOT or unreachable rendezvous)"
    fi
    rm -f "$tmp"
    loose="$(api_list "$MB_ROOT/$MB_ME" 2>/dev/null | grep -v '\.md$' | grep -v '^done$' | grep -v '^attachments$' || true)"
    if [ -z "$loose" ]; then echo "PASS: no orphan attachments in inbox root"
    else echo "FAIL: loose files in inbox root:"; printf '  %s\n' "$loose"; fi
    echo "unacked: $(ids "$MB_ME" | wc -l | tr -d ' ')"
    ;;
  *)
    sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
