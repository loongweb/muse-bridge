#!/usr/bin/env bash
# muse-bridge / rendezvous-init.sh
#
# Run ONCE on the rendezvous host (a VPS both WorkBuddy and Muse can reach over
# outbound SSH). Creates the canonical mailbox layout plus two self-contained
# clients that live ON THE RENDEZVOUS HOST, so neither side needs to install
# anything locally: both just `ssh <host> <mail_root>/mb.sh ...`.
#
# Usage:
#   ./rendezvous-init.sh [MAIL_ROOT] [WORKBUDDY_NAME] [MUSE_NAME]
#   ./rendezvous-init.sh /srv/muse-bridge/mail workbuddy muse
#
# Safe to re-run: it never deletes messages. It DOES refresh send.sh / mb.sh,
# so back them up first if you have local modifications.
set -euo pipefail

ROOT="${1:-/srv/muse-bridge/mail}"
A="${2:-workbuddy}"
B="${3:-muse}"
ROOT="${ROOT%/}"

case "$A" in *[!a-z0-9_-]*|"") echo "invalid name: $A" >&2; exit 2;; esac
case "$B" in *[!a-z0-9_-]*|"") echo "invalid name: $B" >&2; exit 2;; esac
[ "$A" != "$B" ] || { echo "names must differ" >&2; exit 2; }
case "$ROOT" in /*) ;; *) echo "mail_root must be absolute" >&2; exit 2;; esac

for who in "$A" "$B"; do
  mkdir -p "$ROOT/$who/done" "$ROOT/$who/attachments"
done
chmod 750 "$ROOT"
for who in "$A" "$B"; do chmod 750 "$ROOT/$who" "$ROOT/$who/done" "$ROOT/$who/attachments"; done

cat > "$ROOT/BRIDGE.json" <<EOF
{
  "protocol": "muse-bridge/2",
  "mail_root": "$ROOT",
  "participants": ["$A", "$B"],
  "created": "$(date +%Y-%m-%dT%H:%M:%S%z)",
  "note": "Both sides MUST use this exact mail_root. Path drift makes mail invisible."
}
EOF
chmod 640 "$ROOT/BRIDGE.json"

sed "s|__MAIL_ROOT__|$ROOT|g" > "$ROOT/send.sh" <<'EOS'
#!/usr/bin/env bash
# muse-bridge v2 atomic send. Body on stdin.
#   send.sh <to> <from> [attachments_dir] < body.txt
# Prints the message id. Attachments are written BEFORE the .md file, so the
# message is the commit point: a reader never sees half-delivered files.
set -euo pipefail
ROOT="__MAIL_ROOT__"
TO="${1:?usage: send.sh <to> <from> [attachments_dir] < body}"
FROM="${2:?usage: send.sh <to> <from> [attachments_dir] < body}"
ADIR="${3:-}"
hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}
case "$TO" in *[!a-z0-9_-]*|"") echo "bad recipient" >&2; exit 2;; esac
case "$FROM" in *[!a-z0-9_-]*|"") echo "bad sender" >&2; exit 2;; esac

DIR="$ROOT/$TO"
[ -d "$DIR" ] || { echo "unknown recipient box: $TO" >&2; exit 2; }
mkdir -p "$DIR/done" "$DIR/attachments"
ID="$(date +%s)000-$FROM-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"

MANIFEST=""
if [ -n "$ADIR" ] && [ -d "$ADIR" ]; then
  mkdir -p "$DIR/attachments/$ID"
  for f in "$ADIR"/*; do
    [ -f "$f" ] || continue
    n="$(basename "$f")"
    case "$n" in *[!A-Za-z0-9._-]*|.*|*..*) echo "unsafe attachment name: $n" >&2; exit 2;; esac
    cp "$f" "$DIR/attachments/$ID/$n"
    h="$(hash_of "$DIR/attachments/$ID/$n")"
    s="$(wc -c < "$DIR/attachments/$ID/$n" | tr -d ' ')"
    [ -z "$MANIFEST" ] || MANIFEST="$MANIFEST,"
    MANIFEST="$MANIFEST{\"name\":\"$n\",\"sha256\":\"$h\",\"size\":$s}"
  done
  MANIFEST="[$MANIFEST]"
fi

TMP="$DIR/.tmp-$ID"
{
  echo "Id: $ID"
  echo "From: $FROM"
  echo "To: $TO"
  echo "Ts: $(date +%Y-%m-%dT%H:%M:%S%z)"
  echo "X-Bridge-Version: 2"
  echo "X-Kind: task"
  [ -z "$MANIFEST" ] || echo "X-Attachments: $MANIFEST"
  echo
  cat
} > "$TMP"
chmod 640 "$TMP"
mv "$TMP" "$DIR/$ID.md"
echo "$ID"
EOS

sed "s|__MAIL_ROOT__|$ROOT|g; s|__DEFAULT_ME__|$B|g" > "$ROOT/mb.sh" <<'EOS'
#!/usr/bin/env bash
# muse-bridge minimal client for the peer side (typically Muse).
# Lives ON THE RENDEZVOUS HOST; call it over SSH, no local install needed.
#
#   mb.sh inbox                  list unacked ids (NEVER consumes)
#   mb.sh read <id>              print raw message (NEVER consumes)
#   mb.sh ack  <id>              confirm handled -> moves into done/
#   mb.sh attach <id> [outdir]   print attachment paths + sha256
#   mb.sh send <to> <from> [attach_dir] < body
#   mb.sh ls                     mailbox overview
#   mb.sh doctor                 read-only health check
#
# CONTRACT: reading is not consuming. Only `ack` moves a message. A watcher
# must NEVER call ack on behalf of the agent that has to handle the message.
set -euo pipefail
ROOT="__MAIL_ROOT__"
ME="${MB_ME:-__DEFAULT_ME__}"
CMD="${1:-help}"; shift || true
hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

ids() {
  who="${1:-$ME}"
  ls -1A "$ROOT/$who" 2>/dev/null | grep '\.md$' | sed 's/\.md$//' | sort || true
}

case "$CMD" in
  inbox)
    ids "${1:-$ME}"
    ;;
  read)
    id="${1:?usage: mb.sh read <id> [who]}"; who="${2:-$ME}"
    case "$id" in *[!0-9a-z_-]*|"") echo "bad id" >&2; exit 2;; esac
    cat "$ROOT/$who/$id.md"
    ;;
  ack)
    id="${1:?usage: mb.sh ack <id> [who]}"; who="${2:-$ME}"
    case "$id" in *[!0-9a-z_-]*|"") echo "bad id" >&2; exit 2;; esac
    src="$ROOT/$who/$id.md"; dst="$ROOT/$who/done/$id.md"
    mkdir -p "$ROOT/$who/done"
    # hard link = atomic claim; a second concurrent ack cannot win or overwrite
    if ln "$src" "$dst" 2>/dev/null; then
      rm -f "$src"
      if [ -d "$ROOT/$who/attachments/$id" ]; then
        mkdir -p "$ROOT/$who/done/attachments"
        if ! mv "$ROOT/$who/attachments/$id" "$ROOT/$who/done/attachments/$id" 2>/dev/null; then
          echo "WARN: attachment dir left behind for $id; run mb.sh doctor" >&2
        fi
      fi
      echo "acked $id"
    else
      echo "not in inbox (already acked or missing): $id" >&2
      exit 1
    fi
    ;;
  attach)
    id="${1:?usage: mb.sh attach <id> [outdir] [who]}"; outdir="${2:-}"; who="${3:-$ME}"
    d="$ROOT/$who/attachments/$id"
    if [ ! -d "$d" ]; then echo "no attachments for $id"; exit 0; fi
    for f in "$d"/*; do
      [ -f "$f" ] || continue
      printf '%s  %s\n' "$(hash_of "$f")" "$f"
      if [ -n "$outdir" ]; then mkdir -p "$outdir"; cp "$f" "$outdir/"; fi
    done
    ;;
  send)
    exec "$ROOT/send.sh" "$@"
    ;;
  ls)
    for who in "$ME" "${MB_PEER:-}"; do
      [ -n "$who" ] || continue
      printf '%-14s unacked=%-4s done=%-4s\n' "$who" \
        "$(ids "$who" | wc -l | tr -d ' ')" \
        "$(find "$ROOT/$who/done" -maxdepth 1 -type f -name '*.md' 2>/dev/null | wc -l | tr -d ' ')"
    done
    ;;
  doctor)
    echo "mail_root: $ROOT   me: $ME"
    [ -f "$ROOT/BRIDGE.json" ] && grep -E 'mail_root|protocol' "$ROOT/BRIDGE.json" \
      || echo "WARN: no BRIDGE.json (path drift risk)"
    live="$(ids "$ME" | tr '\n' ' ')"
    orphans=""
    for d in "$ROOT/$ME/attachments"/*; do
      [ -d "$d" ] || continue
      b="$(basename "$d")"
      case " $live " in *" $b "*) ;; *) orphans="$orphans $b";; esac
    done
    if [ -n "$orphans" ]; then
      echo "FAIL: orphan attachment dirs (message missing or already acked):$orphans"
    else
      echo "PASS: no orphan attachments"
    fi
    n="$(ids "$ME" | wc -l | tr -d ' ')"
    [ "$n" -lt 20 ] && echo "PASS: $n unacked" || echo "WARN: $n unacked (backlog)"
    echo "PASS: this client never auto-acks"
    ;;
  help|*)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
EOS

chmod 750 "$ROOT/send.sh" "$ROOT/mb.sh"

echo "rendezvous ready: $ROOT"
echo "  participants : $A / $B"
echo "  clients      : $ROOT/send.sh   $ROOT/mb.sh"
echo "  marker       : $ROOT/BRIDGE.json (canonical mail_root — both sides MUST match)"
echo
echo "From either side, over outbound SSH only:"
echo "  ssh <host> $ROOT/mb.sh inbox"
echo "  echo 'hello' | ssh <host> $ROOT/mb.sh send $B $A"
echo
echo "Give the output of 'bridge.py muse-prompt' to Muse for its onboarding."
