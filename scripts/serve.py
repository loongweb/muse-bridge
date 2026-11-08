#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muse-bridge HTTP rendezvous server — standard library only.

For peers that can only make OUTBOUND HTTPS calls (Muse's zero-inbound Secure VM,
a laptop behind NAT, a free container host). The mailbox protocol v2 is unchanged:
this server only maps the existing file operations onto HTTP and reuses
LocalBackend so atomicity, hard-link claims and permissions are identical to
the ssh/local backends.

Typical use — WorkBuddy's own machine, no VPS:

    python3 serve.py --root ~/.local/share/muse-bridge/mail --init-token
    cloudflared tunnel --url http://127.0.0.1:8787     # quick tunnel, no account
    # or a named tunnel on your own CF zone (see assets/tunnel-init.sh)

Then point the peer at https://<hostname> with backend=http.

Security defaults (do not relax without a reason):
  * binds 127.0.0.1 only. Public exposure is the tunnel's job, not the server's.
  * every /v2/* call except /v2/health requires a Bearer token read from a
    0600 file. The token never appears in argv, config templates or logs.
  * all paths are confined to mail_root; traversal and symlink escapes rejected.
  * request bodies are capped.

Only the durable mailbox lives here. Nothing auto-acks: the watcher and this
server NEVER consume a message.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import bridge as B  # noqa: E402

PROTOCOL = "muse-bridge/2"
DEFAULT_MAIL_ROOT = "/srv/muse-bridge/mail"


class _DiskCfg:
    """Minimal shim so LocalBackend can do the actual file work unchanged."""

    def __init__(self, root: str, mail_root: str):
        self._root = root
        self._mail_root = mail_root

    def get(self, key, default=None):
        return self._root if key == "local_root" else default

    @property
    def mail_root(self):
        return self._mail_root


class RendezvousHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "muse-bridge/2"

    def log_message(self, fmt, *args):
        # Never log query strings or headers (they can carry paths and tokens).
        route = urlparse(self.path).path if args else ""
        sys.stderr.write(f"[serve] {self.command} {route} {args[1] if len(args) > 1 else ''}\n")

    # ---------------------------------------------------------------- helpers
    def _json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, status, data: bytes, ctype="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        want = "Bearer " + self.server.token
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got, want):
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise B.BridgeError("bad Content-Length")
        if n < 0 or n > self.server.max_bytes:
            raise B.BridgeError("request body too large")
        return self.rfile.read(n) if n else b""

    def _param(self, q, name):
        v = q.get(name, [""])[0]
        if not v:
            raise B.BridgeError(f"missing query parameter: {name}")
        return v

    def _check(self, p):
        """Confine every client-supplied path to mail_root. LocalBackend._p
        then strips the prefix; realpath guards against symlink escapes."""
        mr = self.server.mail_root
        if not p or not (p == mr or p.startswith(mr + "/")):
            raise B.BridgeError("path outside mail_root")
        if ".." in p.split("/") or os.path.normpath(p) != p:
            raise B.BridgeError("path traversal rejected")
        target = self.server.disk._p(p)
        real = os.path.realpath(str(target))
        root = os.path.realpath(str(self.server.disk.root))
        if real != root and not real.startswith(root + os.sep):
            raise B.BridgeError("symlink escape rejected")
        return p

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        if u.path == "/v2/health":
            return self._json(200, {"ok": True, "protocol": PROTOCOL})
        if not self._authorized():
            return
        try:
            if u.path == "/v2/list":
                p = self._check(self._param(q, "p"))
                return self._json(200, {"items": self.server.disk.list_dir(p)})
            if u.path == "/v2/read":
                p = self._check(self._param(q, "p"))
                try:
                    return self._raw(200, self.server.disk.read_bytes(p))
                except B.BridgeError:
                    return self._json(404, {"error": "missing"})
            if u.path == "/v2/stat":
                p = self._check(self._param(q, "p"))
                return self._json(200, {
                    "exists": self.server.disk.exists(p),
                    "is_dir": self.server.disk.is_dir(p),
                    "size": self.server.disk.size(p),
                    "mtime": self.server.disk.mtime(p),
                })
            return self._json(404, {"error": "unknown route"})
        except B.BridgeError as e:
            return self._json(400, {"error": str(e)})

    def do_PUT(self):
        u = urlparse(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        if not self._authorized():
            return
        try:
            if u.path != "/v2/write":
                return self._json(404, {"error": "unknown route"})
            p = self._check(self._param(q, "p"))
            data = self._body()
            self.server.disk.write_atomic(p, data)
            return self._json(200, {"ok": True, "size": len(data)})
        except B.BridgeError as e:
            return self._json(413 if "too large" in str(e) else 400, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query, keep_blank_values=True)
        if not self._authorized():
            return
        try:
            if u.path == "/v2/mkdir":
                p = self._check(self._param(q, "p"))
                self.server.disk.mkdir(p)
                return self._json(200, {"ok": True})
            if u.path == "/v2/claim":
                src = self._check(self._param(q, "src"))
                dst = self._check(self._param(q, "dst"))
                return self._json(200, {"ok": self.server.disk.claim(src, dst)})
            if u.path == "/v2/move-tree":
                src = self._check(self._param(q, "src"))
                dst = self._check(self._param(q, "dst"))
                return self._json(200, {"ok": self.server.disk.move_tree(src, dst)})
            return self._json(404, {"error": "unknown route"})
        except B.BridgeError as e:
            return self._json(400, {"error": str(e)})


class RendezvousServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, root, mail_root, token, max_bytes, participants):
        self.disk = B.LocalBackend(_DiskCfg(root, mail_root))
        self.mail_root = mail_root
        self.token = token
        self.max_bytes = max_bytes
        super().__init__(addr, RendezvousHandler)
        self._bootstrap(root, mail_root, participants)

    def _bootstrap(self, root, mail_root, participants):
        self.disk.mkdir(mail_root)
        for who in participants:
            self.disk.mkdir(f"{mail_root}/{who}")
            self.disk.mkdir(f"{mail_root}/{who}/done")
            self.disk.mkdir(f"{mail_root}/{who}/attachments")
        marker = f"{mail_root}/BRIDGE.json"
        if not self.disk.exists(marker):
            self.disk.write_atomic(marker, (json.dumps({
                "protocol": PROTOCOL,
                "mail_root": mail_root,
                "participants": participants,
                "created": B.now_iso(),
                "transport": "http",
                "note": "Both sides MUST use this exact mail_root. Path drift makes mail invisible.",
            }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_token(path: Path, init: bool) -> str:
    path = Path(os.path.expanduser(str(path)))
    if init:
        if path.exists():
            raise SystemExit(f"refusing to overwrite existing token file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(32) + "\n", "utf-8")
        os.chmod(path, 0o600)
        print(f"token file created (0600): {path}")
        print("Read it with 'cat' and give it to the peer over a secure channel.")
        print("It is never printed here, never in argv, never in logs.")
    if not path.is_file():
        raise SystemExit(f"token file missing: {path} (use --init-token once)")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"token file must be 0600: {path} (now {oct(mode)}); chmod 600 it")
    tok = path.read_text("utf-8").strip()
    if len(tok) < 16:
        raise SystemExit("token file is too short / empty")
    return tok


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="serve.py",
        description="Zero-dependency HTTP rendezvous for muse-bridge (bind 127.0.0.1 by default).")
    p.add_argument("--root", required=True, help="local directory that holds the mailbox")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--mail-root", default=DEFAULT_MAIL_ROOT,
                   help="logical absolute path both peers must agree on")
    p.add_argument("--token-file", default="~/.config/muse-bridge/http-token")
    p.add_argument("--init-token", action="store_true", help="create the 0600 token file once")
    p.add_argument("--participants", default="workbuddy,muse",
                   help="comma-separated box names (must match config.me / config.peer)")
    p.add_argument("--max-mb", type=int, default=32, help="max request body in MiB")
    a = p.parse_args(argv)

    mail_root = a.mail_root.rstrip("/")
    if not mail_root.startswith("/") or ".." in mail_root.split("/"):
        raise SystemExit("--mail-root must be an absolute path without '..'")
    parts = [x.strip() for x in a.participants.split(",") if x.strip()]
    if len(parts) != 2 or any(not B.valid_box(x) for x in parts) or parts[0] == parts[1]:
        raise SystemExit("--participants must be two distinct [a-z0-9_-] names")
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {a.host} exposes this mailbox on the network. "
              f"Prefer 127.0.0.1 plus an authenticated tunnel.", file=sys.stderr)

    token = read_token(Path(a.token_file), a.init_token)
    root = str(Path(os.path.expanduser(a.root)).resolve())
    srv = RendezvousServer((a.host, a.port), root, mail_root, token,
                           a.max_mb * 1024 * 1024, parts)
    print(f"[serve] {PROTOCOL} listening on http://{a.host}:{a.port}")
    print(f"[serve] mail_root = {mail_root}  ->  {root}")
    print(f"[serve] token = file only ({Path(os.path.expanduser(a.token_file))})")
    print("[serve] NEVER auto-acks. Reading is not consuming.")
    print("[serve] expose explicitly, e.g. cloudflared tunnel --url "
          f"http://127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[serve] stopping", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
