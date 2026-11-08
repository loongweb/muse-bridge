#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muse-bridge — WorkBuddy <-> Muse rendezvous mailbox client (protocol v2, v1-compatible).

Muse runs in a zero-inbound Secure VM: it cannot be reached by SSH, webhook or mail.
The only workable topology is a rendezvous both sides can reach over OUTBOUND
connections, with files as durable storage and an explicit ack as the consumption
signal. Three transports: ssh (VPS), local (same machine), http (VPS-free: CF
Tunnel on the WorkBuddy machine, a free container, or any HTTPS rendezvous).

Invariants enforced by this client:
  1. Reading is NOT consuming. Only `ack` moves a message into done/.
  2. The watcher NEVER acks. It only mirrors messages into a local queue.
  3. Attachments are written BEFORE the message (the message is the commit point)
     and are archived together with it on ack.
  4. No credential, host or path is hardcoded in this file. All come from config.
  5. Messages marked X-Requires-Approval are surfaced as warnings, never executed.

Standard library only. Python 3.9+. POSIX and Windows (local backend) supported.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.0.0"
ID_RE = re.compile(r"^[0-9]{10,}-[a-z0-9_-]{1,32}-[0-9a-f]{8}$")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
KINDS = ("ask", "reply", "notify", "task")

DEFAULTS = {
    "version": 2,
    "me": "workbuddy",
    "peer": "muse",
    "backend": "ssh",
    "ssh_host": "",
    "ssh_host_fallback": "",
    "ssh_connect_timeout": 12,
    "mail_root": "/srv/muse-bridge/mail",
    "local_root": "",
    "http": {"base_url": "", "token_file": "", "timeout": 20},
    "bell": {"enabled": False, "host": "", "port": 18765, "token_file": ""},
    "poll_sec": 60,
    "log_max_bytes": 1048576,
    "done_retention_days": 30,
    "strict_v1": True,
}


class BridgeError(Exception):
    pass


def eprint(*a):
    print(*a, file=sys.stderr)


def now_iso():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_id(mid: str) -> bool:
    return bool(ID_RE.match(mid))


def valid_box(name: str) -> bool:
    return bool(NAME_RE.match(name))


def valid_attachment_name(name: str) -> bool:
    return bool(SAFE_FILE_RE.match(name)) and ".." not in name


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

class Config:
    def __init__(self, path: str | None = None, overrides: dict | None = None):
        self.path = Path(os.path.expanduser(
            path or os.environ.get("MUSE_BRIDGE_CONFIG")
            or "~/.config/muse-bridge/config.json"))
        self.data = json.loads(json.dumps(DEFAULTS))
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if not isinstance(loaded, dict):
                    raise BridgeError("config root must be a JSON object")
                self.data = deep_merge(self.data, loaded)
            except (ValueError, OSError) as e:
                raise BridgeError(f"cannot read config {self.path}: {e}")
        if overrides:
            self.data = deep_merge(self.data, overrides)
        self._validate()

    def _validate(self):
        d = self.data
        if not valid_box(str(d.get("me", ""))):
            raise BridgeError("config.me must match ^[a-z0-9_-]{1,32}$")
        if not valid_box(str(d.get("peer", ""))):
            raise BridgeError("config.peer must match ^[a-z0-9_-]{1,32}$")
        if d["me"] == d["peer"]:
            raise BridgeError("config.me and config.peer must differ")
        if d.get("backend") not in ("ssh", "local", "http"):
            raise BridgeError("config.backend must be 'ssh', 'local' or 'http'")
        root = str(d.get("mail_root", ""))
        if not root.startswith("/") or ".." in root.split("/"):
            raise BridgeError("config.mail_root must be an absolute path without '..'")
        if d["backend"] == "ssh" and not d.get("ssh_host"):
            raise BridgeError("config.ssh_host is required when backend=ssh")
        if d["backend"] == "local" and not d.get("local_root"):
            raise BridgeError("config.local_root is required when backend=local")
        if d["backend"] == "http":
            h = d.get("http") or {}
            base = str(h.get("base_url") or "")
            u = urllib.parse.urlparse(base)
            if not base or u.scheme not in ("http", "https") or not u.netloc \
                    or u.query or u.fragment or u.path.rstrip("/"):
                raise BridgeError("config.http.base_url must be a plain http(s) origin, no path/query")
            if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise BridgeError("refusing plaintext http to a remote host: token would leak; use https")
            if not h.get("token_file"):
                raise BridgeError("config.http.token_file is required (0600 file; never inline the token)")
        try:
            int(d.get("poll_sec", 60))
            int(d.get("log_max_bytes", 1048576))
        except (TypeError, ValueError):
            raise BridgeError("poll_sec / log_max_bytes must be integers")

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def me(self):
        return self.data["me"]

    @property
    def peer(self):
        return self.data["peer"]

    @property
    def mail_root(self):
        return self.data["mail_root"].rstrip("/")

    @property
    def state_dir(self) -> Path:
        p = Path(os.path.expanduser(
            os.environ.get("MUSE_BRIDGE_STATE") or "~/.local/state/muse-bridge"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def token_path(self) -> Path:
        tf = (self.data.get("bell") or {}).get("token_file")
        return Path(os.path.expanduser(tf or "~/.config/muse-bridge/bus-token"))

    def save_template(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise BridgeError(f"config already exists: {self.path} (refusing to overwrite)")
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.chmod(self.path, 0o600)
        return self.path


# --------------------------------------------------------------------------
# Backends — one narrow interface, two implementations
# --------------------------------------------------------------------------

class LocalBackend:
    """Pure-Python backend. Works on macOS/Linux/Windows and in self-tests."""

    def __init__(self, cfg: Config):
        self.root = Path(os.path.expanduser(cfg.get("local_root"))).resolve()
        self.relocate = str(cfg.mail_root).rstrip("/")

    def _p(self, path: str) -> Path:
        p = str(path)
        if p.startswith(self.relocate + "/"):
            p = p[len(self.relocate) + 1:]
        elif p == self.relocate:
            p = "."
        return self.root / p

    def mkdir(self, path):
        self._p(path).mkdir(parents=True, exist_ok=True)

    def list_dir(self, path):
        p = self._p(path)
        if not p.is_dir():
            return []
        return sorted(x.name for x in p.iterdir())

    def read_bytes(self, path):
        try:
            return self._p(path).read_bytes()
        except FileNotFoundError:
            raise BridgeError(f"missing: {path}")

    def write_atomic(self, path, data: bytes):
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".tmp-{os.getpid()}-{time.time_ns()}"
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        os.replace(tmp, p)

    def claim(self, src, dst) -> bool:
        """Atomically claim src by hard-linking to dst, then unlinking src.

        Hard link instead of rename: it fails if dst already exists, so two
        concurrent acks can never both succeed and can never overwrite done/.
        """
        s, d = self._p(src), self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(s, d)
        except (FileExistsError, FileNotFoundError):
            return False
        try:
            s.unlink()
        except FileNotFoundError:
            pass
        return True

    def move_tree(self, src, dst) -> bool:
        s, d = self._p(src), self._p(dst)
        if not s.exists():
            return False
        d.parent.mkdir(parents=True, exist_ok=True)
        if d.exists():
            return False
        try:
            shutil.move(str(s), str(d))
            return True
        except (OSError, shutil.Error):
            return False

    def exists(self, path):
        return self._p(path).exists()

    def is_dir(self, path):
        return self._p(path).is_dir()

    def size(self, path):
        try:
            return self._p(path).stat().st_size
        except OSError:
            return 0

    def mtime(self, path):
        try:
            return self._p(path).stat().st_mtime
        except OSError:
            return 0.0

    def describe(self):
        return f"local:{self.root}"


class SSHBackend:
    """Rendezvous on a remote host. Uses one bash -s call per operation, with
    SSH ControlMaster multiplexing so a 60s poll does not pay a full handshake.
    Automatic failover to ssh_host_fallback on connection-level failures."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.primary = cfg.get("ssh_host")
        self.fallback = cfg.get("ssh_host_fallback") or ""
        self.active = self.primary
        self.timeout = int(cfg.get("ssh_connect_timeout", 12))
        self.ctl = Path(cfg.state_dir) / "ssh-ctl-%r@%h:%p"
        self.ctl.parent.mkdir(parents=True, exist_ok=True)

    def _ssh_args(self, host):
        return [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.timeout}",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={self.ctl}",
            "-o", "ControlPersist=600", host, "bash", "-s",
        ]

    def _run_once(self, host, script):
        r = subprocess.run(self._ssh_args(host), input=script,
                           capture_output=True, text=True, timeout=self.timeout + 45)
        if r.returncode != 0:
            raise BridgeError((r.stderr or "").strip() or f"ssh exit {r.returncode}")
        return r.stdout

    def run(self, script: str) -> str:
        try:
            return self._run_once(self.active, script)
        except (BridgeError, subprocess.TimeoutExpired, OSError) as first:
            alt = self.fallback if self.active == self.primary else self.primary
            if not alt or alt == self.active:
                raise
            try:
                out = self._run_once(alt, script)
                self.active = alt
                eprint(f"[bridge] {self.primary} unreachable, switched to {alt}")
                return out
            except (BridgeError, subprocess.TimeoutExpired, OSError):
                raise first

    @staticmethod
    def q(s: str) -> str:
        """Shell-safe single quoting, with no interpolation of caller data."""
        return "'" + str(s).replace("'", "'\\''") + "'"

    def mkdir(self, path):
        self.run(f"mkdir -p {self.q(path)}\n")

    def list_dir(self, path):
        out = self.run(
            f"d={self.q(path)}; if [ -d \"$d\" ]; then ls -1A \"$d\"; fi\n")
        return [x for x in out.splitlines() if x and x not in (".", "..")]

    def read_bytes(self, path):
        out = self.run(
            f"f={self.q(path)}; if [ ! -f \"$f\" ]; then echo MISSING; "
            f"else base64 < \"$f\" | tr -d '\\n'; fi\n").strip()
        if out == "MISSING" or not out:
            raise BridgeError(f"missing: {path}")
        return unb64(out)

    def write_atomic(self, path, data: bytes):
        d = os.path.dirname(path)
        script = (
            f"d={self.q(d)}; f={self.q(path)}; mkdir -p \"$d\" || exit 1\n"
            f"tmp=\"$d/.tmp-$$\"\n"
            f"printf '%s' {self.q(b64(data))} | base64 -d > \"$tmp\" || exit 1\n"
            f"chmod 640 \"$tmp\"\n"
            f"mv \"$tmp\" \"$f\" || {{ rm -f \"$tmp\"; exit 1; }}\n")
        self.run(script)

    def claim(self, src, dst) -> bool:
        d = os.path.dirname(dst)
        script = (
            f"s={self.q(src)}; t={self.q(dst)}; mkdir -p {self.q(d)} || exit 1\n"
            f"if ln \"$s\" \"$t\" 2>/dev/null; then rm -f \"$s\"; echo MOVED; "
            f"else echo NOT_FOUND; fi\n")
        return "MOVED" in self.run(script)

    def move_tree(self, src, dst) -> bool:
        d = os.path.dirname(dst)
        script = (
            f"s={self.q(src)}; t={self.q(dst)}; mkdir -p {self.q(d)} || exit 1\n"
            f"if [ -d \"$s\" ] && [ ! -e \"$t\" ] && mv \"$s\" \"$t\" 2>/dev/null; "
            f"then echo MOVED; else echo NOT_FOUND; fi\n")
        return "MOVED" in self.run(script)

    def exists(self, path):
        return "YES" in self.run(f"test -e {self.q(path)} && echo YES || echo NO\n")

    def is_dir(self, path):
        return "YES" in self.run(f"test -d {self.q(path)} && echo YES || echo NO\n")

    def size(self, path):
        out = self.run(f"f={self.q(path)}; [ -f \"$f\" ] && stat -c %s \"$f\" || echo 0\n")
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    def mtime(self, path):
        out = self.run(f"f={self.q(path)}; [ -f \"$f\" ] && stat -c %Y \"$f\" || echo 0\n")
        try:
            return float(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0.0

    def describe(self):
        return f"ssh:{self.active} (fallback={self.fallback or 'none'})"


class HttpBackend:
    """Rendezvous reached over OUTBOUND HTTPS only.

    Pairs with scripts/serve.py: behind a Cloudflare Tunnel on the WorkBuddy
    machine, on a free container host, or any other HTTPS implementation of the
    same narrow file API. The token is read from a 0600 file — never argv,
    never the config, never a log line.
    """

    def __init__(self, cfg: Config):
        h = cfg.get("http") or {}
        self.base = str(h.get("base_url") or "").rstrip("/")
        self.timeout = int(h.get("timeout", 20))
        tp = Path(os.path.expanduser(str(h.get("token_file") or "")))
        if not tp.is_file():
            raise BridgeError(f"http token file missing: {tp}")
        try:
            self.token = tp.read_text("utf-8").strip()
        except OSError as e:
            raise BridgeError(f"cannot read http token file: {e}")
        if len(self.token) < 16:
            raise BridgeError("http token file is empty or too short")

    def _req(self, method, route, params=None, data=None):
        url = self.base + route
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise BridgeError("missing: " + route)
            raise BridgeError(f"http {e.code} on {route}: {e.reason}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise BridgeError(f"http rendezvous unreachable ({self.base}): {e}")

    def _json(self, method, route, params=None, data=None):
        _, body = self._req(method, route, params, data)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise BridgeError(f"bad JSON from rendezvous: {e}")

    def mkdir(self, path):
        self._json("POST", "/v2/mkdir", {"p": path})

    def list_dir(self, path):
        return self._json("GET", "/v2/list", {"p": path}).get("items", [])

    def read_bytes(self, path):
        try:
            _, data = self._req("GET", "/v2/read", {"p": path})
        except BridgeError as e:
            if str(e).startswith("missing:"):
                raise BridgeError(f"missing: {path}")
            raise
        return data

    def write_atomic(self, path, data: bytes):
        self._json("PUT", "/v2/write", {"p": path}, data)

    def claim(self, src, dst) -> bool:
        return bool(self._json("POST", "/v2/claim", {"src": src, "dst": dst}).get("ok"))

    def move_tree(self, src, dst) -> bool:
        return bool(self._json("POST", "/v2/move-tree",
                               {"src": src, "dst": dst}).get("ok"))

    def _stat(self, path):
        return self._json("GET", "/v2/stat", {"p": path})

    def exists(self, path):
        return bool(self._stat(path).get("exists"))

    def is_dir(self, path):
        return bool(self._stat(path).get("is_dir"))

    def size(self, path):
        return int(self._stat(path).get("size", 0))

    def mtime(self, path):
        return float(self._stat(path).get("mtime", 0.0))

    def describe(self):
        return "http:" + urllib.parse.urlparse(self.base).netloc


def make_backend(cfg: Config):
    kind = cfg.get("backend")
    if kind == "local":
        return LocalBackend(cfg)
    if kind == "http":
        return HttpBackend(cfg)
    return SSHBackend(cfg)


# --------------------------------------------------------------------------
# Message format — v1 headers first, v2 extensions as X-* (v1 readers ignore)
# --------------------------------------------------------------------------

def new_id(me: str) -> str:
    return f"{int(time.time() * 1000)}-{me}-{os.urandom(4).hex()}"


def parse_message(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    head, sep, body = text.partition("\n\n")
    headers = {}
    for line in (head if sep else "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    return {"headers": headers, "body": body if sep else text, "raw": text}


def serialize_message(headers: dict, body: str) -> bytes:
    order = ["Id", "From", "To", "Ts"]
    lines = [f"{k}: {headers[k]}" for k in order if k in headers]
    lines += [f"{k}: {v}" for k, v in headers.items()
              if k not in order and not k.lower().startswith("x-internal")]
    return ("\n".join(lines) + "\n\n" + body.lstrip("\n")).encode("utf-8")


def summary_of(parsed: dict) -> str:
    body = parsed["body"].strip().splitlines()
    return (body[0][:100] if body else "(empty)")


# --------------------------------------------------------------------------
# Bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: Config, backend=None):
        self.cfg = cfg
        self.be = backend or make_backend(cfg)
        self.root = cfg.mail_root
        self.state = cfg.state_dir
        self.local_inbox = self.state / "inbox"
        self.local_inbox.mkdir(parents=True, exist_ok=True)
        self.log_path = self.state / "bridge.log"

    # -- paths -------------------------------------------------------------
    def box(self, who):
        return f"{self.root}/{who}"

    def msg_path(self, who, mid):
        return f"{self.box(who)}/{mid}.md"

    def done_path(self, who, mid):
        return f"{self.box(who)}/done/{mid}.md"

    def attach_dir(self, who, mid):
        return f"{self.box(who)}/attachments/{mid}"

    def done_attach_dir(self, who, mid):
        return f"{self.box(who)}/done/attachments/{mid}"

    # -- logging (self-rotating, never grows without bound) ----------------
    def log(self, msg):
        try:
            mx = int(self.cfg.get("log_max_bytes", 1048576))
            if self.log_path.exists() and self.log_path.stat().st_size > mx:
                for i in (2, 1):
                    src = self.log_path.with_suffix(f".log.{i}")
                    if src.exists():
                        if i == 2:
                            src.unlink()
                        else:
                            src.rename(self.log_path.with_suffix(".log.2"))
                self.log_path.rename(self.log_path.with_suffix(".log.1"))
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] {msg}\n")
        except OSError:
            pass

    # -- setup -------------------------------------------------------------
    def init_rendezvous(self):
        for who in (self.cfg.me, self.cfg.peer):
            self.be.mkdir(self.box(who))
            self.be.mkdir(f"{self.box(who)}/done")
            self.be.mkdir(f"{self.box(who)}/attachments")
        meta = {
            "protocol": "muse-bridge/2",
            "mail_root": self.root,
            "participants": [self.cfg.me, self.cfg.peer],
            "created": now_iso(),
            "note": "Both sides MUST use this exact mail_root. Path drift makes mail invisible.",
        }
        self.be.write_atomic(f"{self.root}/BRIDGE.json",
                             json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        return meta

    def read_rendezvous_meta(self):
        p = f"{self.root}/BRIDGE.json"
        if not self.be.exists(p):
            return None
        try:
            return json.loads(self.be.read_bytes(p).decode("utf-8", "replace"))
        except ValueError:
            return None

    # -- listing -----------------------------------------------------------
    def list_inbox(self, who=None):
        who = who or self.cfg.me
        return [n[:-3] for n in self.be.list_dir(self.box(who))
                if n.endswith(".md") and valid_id(n[:-3])]

    def list_done(self, who=None):
        who = who or self.cfg.me
        return [n[:-3] for n in self.be.list_dir(f"{self.box(who)}/done")
                if n.endswith(".md") and valid_id(n[:-3])]

    def read(self, mid, who=None):
        if not valid_id(mid):
            raise BridgeError(f"invalid message id: {mid!r}")
        return parse_message(self.be.read_bytes(self.msg_path(who or self.cfg.me, mid)))

    # -- send --------------------------------------------------------------
    def send(self, to, body, attachments=None, kind="task", reply_to=None,
             requires_approval=False, extra_headers=None):
        if not valid_box(to):
            raise BridgeError(f"invalid recipient: {to!r}")
        if kind not in KINDS:
            raise BridgeError(f"kind must be one of {KINDS}")
        if reply_to and not valid_id(reply_to):
            raise BridgeError("reply_to is not a valid message id")
        mid = new_id(self.cfg.me)
        headers = {
            "Id": mid,
            "From": self.cfg.me,
            "To": to,
            "Ts": now_iso(),
            "X-Bridge-Version": "2",
            "X-Kind": kind,
        }
        if reply_to:
            headers["X-Reply-To"] = reply_to
        if requires_approval:
            headers["X-Requires-Approval"] = "true"
        if extra_headers:
            headers.update(extra_headers)

        # Attachments are written FIRST. The .md file is the commit point:
        # a reader can never observe a message whose files are still arriving.
        manifest = []
        for path in (attachments or []):
            p = Path(os.path.expanduser(path))
            name = p.name
            if not valid_attachment_name(name):
                raise BridgeError(f"unsafe attachment name: {name!r}")
            data = p.read_bytes()
            self.be.write_atomic(f"{self.attach_dir(to, mid)}/{name}", data)
            manifest.append({"name": name, "sha256": sha256_bytes(data),
                             "size": len(data)})
        if manifest:
            headers["X-Attachments"] = json.dumps(manifest, ensure_ascii=False,
                                                  separators=(",", ":"))
        self.be.write_atomic(self.msg_path(to, mid), serialize_message(headers, body))
        bell_ok = self.ring_bell(to, mid, summary_of(parse_message(
            serialize_message(headers, body))))
        self.log(f"send {mid} -> {to} attach={len(manifest)} bell={bell_ok}")
        return mid, bell_ok

    def ring_bell(self, to, mid, summary):
        bell = self.cfg.get("bell") or {}
        if not bell.get("enabled"):
            return None
        host, port = bell.get("host"), int(bell.get("port", 18765))
        if not host:
            return False
        tp = self.cfg.token_path
        token = tp.read_text("utf-8").strip() if tp.exists() else ""
        if not token:
            eprint(f"[bridge] bell disabled: no token at {tp} (message is safely on disk)")
            return False
        try:
            s = socket.create_connection((host, port), timeout=8)
            s.sendall((json.dumps({"type": "register", "name": self.cfg.me,
                                   "token": token}) + "\n").encode())
            s.settimeout(5)
            s.recv(65536)
            s.sendall((json.dumps({"type": "send", "to": to,
                                   "body": f"[MAIL] {mid} {summary}"}) + "\n").encode())
            s.close()
            return True
        except (OSError, ValueError) as e:
            eprint(f"[bridge] bell failed ({e}); message {mid} is on disk, poll will find it")
            return False

    # -- receive (NEVER acks) ---------------------------------------------
    def sync_inbox(self):
        """Mirror unacked messages into a local queue. Returns list of meta dicts.

        This is deliberately read-only at the rendezvous. The watcher calls it;
        nothing here moves anything into done/.
        """
        found = []
        for mid in self.list_inbox():
            try:
                parsed = self.read(mid)
            except BridgeError:
                continue
            h = parsed["headers"]
            local = self.local_inbox / f"{mid}.md"
            if not local.exists():
                local.write_bytes(parsed["raw"].encode("utf-8", "replace"))
            found.append({
                "id": mid,
                "from": h.get("From", "?"),
                "ts": h.get("Ts", ""),
                "kind": h.get("X-Kind", "task"),
                "summary": summary_of(parsed),
                "requires_approval": h.get("X-Requires-Approval", "").lower() == "true",
                "attachments": h.get("X-Attachments", ""),
            })
        return found

    def download_attachments(self, mid, out_dir=None):
        parsed = self.read(mid)
        raw = parsed["headers"].get("X-Attachments", "")
        if not raw:
            return []
        try:
            manifest = json.loads(raw)
        except ValueError:
            raise BridgeError(f"{mid}: corrupt X-Attachments manifest")
        dest = Path(os.path.expanduser(out_dir)) if out_dir else self.state / "downloads" / mid
        dest.mkdir(parents=True, exist_ok=True)
        result = []
        for item in manifest:
            name = item.get("name", "")
            if not valid_attachment_name(name):
                raise BridgeError(f"unsafe attachment name in manifest: {name!r}")
            data = self.be.read_bytes(f"{self.attach_dir(self.cfg.me, mid)}/{name}")
            got = sha256_bytes(data)
            if got != item.get("sha256"):
                raise BridgeError(f"{mid}/{name}: sha256 mismatch (expected {item.get('sha256')}, got {got})")
            target = dest / name
            target.write_bytes(data)
            result.append(str(target))
        return result

    # -- ack (the ONLY consuming operation) -------------------------------
    def ack(self, mid):
        if not valid_id(mid):
            raise BridgeError(f"invalid message id: {mid!r}")
        who = self.cfg.me
        if not self.be.claim(self.msg_path(who, mid), self.done_path(who, mid)):
            return False, "not in inbox (already acked or missing)"
        notes = []
        if self.be.is_dir(self.attach_dir(who, mid)):
            self.be.mkdir(f"{self.box(who)}/done/attachments")
            if self.be.move_tree(self.attach_dir(who, mid), self.done_attach_dir(who, mid)):
                notes.append("attachments archived")
            else:
                notes.append("WARN: attachment dir left behind; run doctor")
        local = self.local_inbox / f"{mid}.md"
        try:
            local.unlink()
        except FileNotFoundError:
            pass
        self.log(f"ack {mid} {'; '.join(notes)}")
        return True, "; ".join(notes) or "acked"

    def peer_acked(self, mid):
        if not valid_id(mid):
            raise BridgeError(f"invalid message id: {mid!r}")
        return self.be.exists(self.done_path(self.cfg.peer, mid))

    # -- watcher -----------------------------------------------------------
    def watch(self, once=False):
        # Persist the notification cursor: --once is designed to be launched
        # repeatedly by launchd/systemd, so "seen" must survive process exit.
        seen_path = self.state / "seen.json"
        try:
            seen = set(json.loads(seen_path.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            seen = set()
        print(f"[bridge] watching {self.be.describe()} every {self.cfg.get('poll_sec')}s; "
              f"NEVER auto-acks. Ctrl-C to stop.", flush=True)
        while True:
            try:
                items = self.sync_inbox()
                cur = {x["id"] for x in items}
                for x in items:
                    if x["id"] not in seen:
                        flag = " [NEEDS APPROVAL]" if x["requires_approval"] else ""
                        print(f"[NEW] {x['id']} from={x['from']} kind={x['kind']}"
                              f"{flag} :: {x['summary']}", flush=True)
                        self.log(f"new {x['id']} from={x['from']}")
                seen = cur
                try:
                    seen_path.write_text(json.dumps(sorted(cur)), "utf-8")
                except OSError:
                    pass
                if items:
                    print(f"[bridge] {len(items)} unacked message(s). "
                          f"Read with 'inbox', then 'ack <id>' after handling.", flush=True)
            except BridgeError as e:
                print(f"[bridge] poll failed: {e}", flush=True)
                self.log(f"poll failed: {e}")
            if once:
                return
            time.sleep(max(5, int(self.cfg.get("poll_sec", 60))))


# --------------------------------------------------------------------------
# Doctor — read-only health checks distilled from real production incidents
# --------------------------------------------------------------------------

def cmd_doctor(bridge: Bridge, args):
    cfg, be = bridge.cfg, bridge.be
    results = []

    def add(level, name, detail, fix=""):
        results.append({"level": level, "name": name, "detail": detail, "fix": fix})

    # 1. config file permission
    if cfg.path.exists():
        mode = cfg.path.stat().st_mode & 0o077
        add("WARN" if mode else "PASS", "config permissions",
            f"{cfg.path} mode={oct(cfg.path.stat().st_mode & 0o777)}",
            f"chmod 600 {cfg.path}" if mode else "")
    else:
        add("WARN", "config permissions", "no config file (defaults in use)",
            "run: bridge.py init")

    # 2. hardcoded credential scan
    leaks = []
    scan_roots = [cfg.path.parent, Path(os.path.expanduser("~/.workbuddy/skills/muse-bridge/scripts"))]
    if args.legacy_script:
        scan_roots.append(Path(os.path.expanduser(args.legacy_script)).parent)
    for root in scan_roots:
        if not root.is_dir():
            continue
        for f in list(root.glob("*.py")) + list(root.glob("*.json")) + list(root.glob("*.sh")):
            try:
                txt = f.read_text("utf-8", "replace")
            except OSError:
                continue
            for m in re.finditer(r"(?:BUS_TOKEN|TOKEN|token)\s*=\s*[\"']([A-Za-z0-9_\-]{24,})[\"']", txt):
                leaks.append(f"{f.name}:{txt[:m.start()].count(chr(10)) + 1}")
    add("FAIL" if leaks else "PASS", "hardcoded credentials",
        "; ".join(leaks[:5]) if leaks else "no literal secrets found in scanned scripts",
        "move the token to a 0600 file and read it at runtime" if leaks else "")

    # 3. token file permission
    bell = cfg.get("bell") or {}
    if bell.get("enabled"):
        tp = cfg.token_path
        if not tp.exists():
            add("WARN", "bell token", f"missing {tp}", "create with chmod 600, or disable bell")
        elif tp.stat().st_mode & 0o077:
            add("FAIL", "bell token", f"{tp} is group/world readable",
                f"chmod 600 {tp}")
        else:
            add("PASS", "bell token", f"{tp} mode 0600")
        if bell.get("host"):
            add("WARN", "bell transport",
                f"cleartext TCP {bell.get('host')}:{bell.get('port')} with a static shared token",
                "prefer a private interface (tailscale0) or disable bell and rely on polling")
    else:
        add("PASS", "bell transport", "disabled (polling only, zero extra exposure)")

    # 4. rendezvous reachability + canonical path
    try:
        meta = bridge.read_rendezvous_meta()
        if not meta:
            add("WARN", "rendezvous marker", "no BRIDGE.json at mail_root",
                "run: bridge.py rendezvous-init (do this ONCE, on one side only)")
        elif meta.get("mail_root") != cfg.mail_root:
            add("FAIL", "path drift",
                f"marker says {meta.get('mail_root')}, config says {cfg.mail_root}",
                "both sides must use the identical mail_root — path drift makes mail invisible")
        else:
            add("PASS", "rendezvous marker", f"canonical mail_root={cfg.mail_root}")
        add("PASS", "rendezvous reachable", be.describe())
    except BridgeError as e:
        add("FAIL", "rendezvous reachable", str(e)[:160],
            "check ssh_host / ssh_host_fallback and outbound connectivity")
        meta = None

    # 5. fallback host
    if cfg.get("backend") == "ssh":
        add("PASS" if cfg.get("ssh_host_fallback") else "WARN", "ssh failover",
            f"fallback={cfg.get('ssh_host_fallback') or 'none'}",
            "set ssh_host_fallback to a second reachable host" if not cfg.get("ssh_host_fallback") else "")

    # 5b. HTTP (VPS-free) rendezvous specifics
    if cfg.get("backend") == "http":
        h = cfg.get("http") or {}
        tp = Path(os.path.expanduser(str(h.get("token_file") or "")))
        if not tp.is_file():
            add("FAIL", "http token", f"missing {tp}",
                "create it once: serve.py --init-token (never inline the token)")
        elif tp.stat().st_mode & 0o077:
            add("FAIL", "http token", f"{tp} is group/world readable", f"chmod 600 {tp}")
        else:
            add("PASS", "http token", f"{tp} mode 0600")
        base = str(h.get("base_url") or "").rstrip("/")
        try:
            with urllib.request.urlopen(base + "/v2/health",
                                        timeout=int(h.get("timeout", 20))) as r:
                code = r.status
            add("PASS" if code == 200 else "WARN", "http health",
                f"{base}/v2/health -> {code}", "")
        except (urllib.error.URLError, OSError) as e:
            add("WARN", "http health", f"{base}/v2/health unreachable: {str(e)[:80]}",
                "check tunnel / container / hostname; mail already on disk is safe")
        add("WARN", "always-on rendezvous",
            "a hostname terminating on a local machine sleeps with that machine",
            "while it sleeps the peer cannot WRITE new mail; use a free always-on "
            "container or a VPS for true async delivery")

    # 6. inbox backlog / oldest message
    try:
        ids = bridge.list_inbox()
        oldest = min((be.mtime(bridge.msg_path(cfg.me, m)) for m in ids), default=0)
        age_h = (time.time() - oldest) / 3600 if oldest else 0
        if len(ids) > 20 or age_h > 24:
            add("WARN", "inbox backlog",
                f"{len(ids)} unacked, oldest {age_h:.1f}h",
                "read with 'inbox', handle, then 'ack <id>' explicitly")
        else:
            add("PASS", "inbox backlog", f"{len(ids)} unacked, oldest {age_h:.1f}h")
        approvals = [m for m in ids
                     if parse_message(be.read_bytes(bridge.msg_path(cfg.me, m)))
                     ["headers"].get("X-Requires-Approval", "").lower() == "true"]
        if approvals:
            add("WARN", "pending approvals",
                f"{len(approvals)} message(s) request a consequential action",
                "require the human owner's explicit go-ahead before acting")
    except BridgeError as e:
        add("WARN", "inbox backlog", f"could not inspect: {e}")

    # 7. orphan attachments
    try:
        live = set(bridge.list_inbox())
        orphans = [d for d in be.list_dir(f"{bridge.box(cfg.me)}/attachments")
                   if d not in live and not d.startswith(".")]
        add("FAIL" if orphans else "PASS", "orphan attachments",
            f"{len(orphans)} dir(s) with no matching message" + (f": {orphans[:3]}" if orphans else ""),
            "download them, then archive/remove manually after human confirmation" if orphans else "")
    except BridgeError:
        pass

    # 7b. loose files in inbox root (legacy clients drop attachments here)
    # A real incident: 3 PNGs sat in the inbox root while the .md notice was
    # already acked, because the old fetch loop only globbed *.md.
    try:
        box = bridge.box(cfg.me)
        loose = []
        for n in be.list_dir(box):
            if n.endswith(".md") or n.startswith(".") or n in ("done", "attachments"):
                continue
            p = f"{box}/{n}"
            if not be.is_dir(p):
                loose.append(n)
        add("WARN" if loose else "PASS", "loose files in inbox",
            f"{len(loose)} non-.md file(s) invisible to legacy fetch loops"
            + (f": {loose[:3]}" if loose else ""),
            "download these manually, then use 'send --attach' or the v2 manifest"
            if loose else "")
    except BridgeError:
        pass

    # 8. done retention
    try:
        cutoff = time.time() - int(cfg.get("done_retention_days", 30)) * 86400
        old = [d for d in bridge.list_done()
               if 0 < be.mtime(bridge.done_path(cfg.me, d)) < cutoff]
        add("WARN" if old else "PASS", "done retention",
            f"{len(old)} archived message(s) older than {cfg.get('done_retention_days')}d",
            "archive/remove only after human confirmation" if old else "")
    except BridgeError:
        pass

    # 9. watcher must not ack
    if args.legacy_script:
        lp = Path(os.path.expanduser(args.legacy_script))
        if lp.is_file():
            txt = lp.read_text("utf-8", "replace")
            bad = ("auto_ack" in txt and "cmd_fetch()" in txt and "listen" in txt)
            add("FAIL" if bad else "PASS", "legacy auto-ack",
                "legacy watcher acks on fetch — messages disappear before a session reads them"
                if bad else "no auto-ack-on-fetch pattern detected",
                "replace the watcher with 'bridge.py watch' (never acks)" if bad else "")
        else:
            add("WARN", "legacy auto-ack", f"script not found: {lp}")
    else:
        add("INFO", "legacy auto-ack", "skipped (pass --legacy-script <old client>)")

    # 10. self-rotating log
    size = bridge.log_path.stat().st_size if bridge.log_path.exists() else 0
    add("PASS" if size < int(cfg.get("log_max_bytes", 1048576)) else "WARN",
        "log rotation", f"bridge.log={size} bytes (auto-rotates at {cfg.get('log_max_bytes')})",
        "rotation is built in; check why volume is high" if size >= int(cfg.get("log_max_bytes", 1048576)) else "")

    # 11. local inbox queue (wake-up path)
    nlocal = len(list(bridge.local_inbox.glob("*.md")))
    add("INFO", "local queue", f"{nlocal} mirrored message(s) in {bridge.local_inbox}")

    widths = (7, 24)
    print(f"\n{'LEVEL':<7} {'CHECK':<24} DETAIL")
    print("-" * 100)
    for r in results:
        print(f"{r['level']:<7} {r['name']:<24} {r['detail']}")
        if r["fix"]:
            print(f"{'':<7} {'':<24} -> {r['fix']}")
    counts = {k: sum(1 for r in results if r["level"] == k)
              for k in ("PASS", "WARN", "FAIL", "INFO")}
    print("-" * 100)
    print(f"PASS={counts['PASS']}  WARN={counts['WARN']}  FAIL={counts['FAIL']}  INFO={counts['INFO']}")
    return 1 if counts["FAIL"] else 0


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args, cfg):
    try:
        cfg.save_template()
        print(f"config written: {cfg.path} (mode 0600)")
    except BridgeError as e:
        print(f"[bridge] {e}")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "inbox").mkdir(exist_ok=True)
    print(f"local state:    {cfg.state_dir}")
    print(f"backend:        {cfg.get('backend')}")
    print(f"rendezvous:     {cfg.mail_root}")
    print("\nNext: edit the config, run 'rendezvous-init' once, then 'selftest', then 'doctor'.")
    print("Give 'muse-prompt' output to Muse so both sides use the SAME mail_root.")
    return 0


def cmd_muse_prompt(cfg):
    is_http = cfg.get("backend") == "http"
    name = "muse-side-http.md" if is_http else "muse-side.md"
    ref = Path(__file__).parent.parent / "references" / name
    tmpl = ref.read_text("utf-8") if ref.exists() else f"({name} missing)"
    repl = {
        "{{MAIL_ROOT}}": cfg.mail_root,
        "{{PEER}}": cfg.peer,
        "{{ME}}": cfg.me,
        "{{BACKEND}}": cfg.get("backend"),
        "{{SSH_HOST}}": cfg.get("ssh_host") or "(your rendezvous host)",
        "{{BASE_URL}}": str((cfg.get("http") or {}).get("base_url") or ""),
    }
    # Deliberately NEVER substitute the token: it must travel over a separate
    # secure channel, not in a paste-ready chat message.
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    print(tmpl)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="bridge.py",
        description="WorkBuddy <-> Muse rendezvous mailbox (read != consume)")
    p.add_argument("--config", help="config path (default ~/.config/muse-bridge/config.json)")
    p.add_argument("--backend", choices=("ssh", "local", "http"))
    p.add_argument("--local-root")
    p.add_argument("--base-url", help="http backend: https origin of the rendezvous")
    p.add_argument("--http-token-file", help="http backend: 0600 file with the bearer token")
    p.add_argument("--mail-root")
    p.add_argument("--me")
    p.add_argument("--peer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create a 0600 config and local state dirs")
    sub.add_parser("rendezvous-init", help="create mailbox dirs + BRIDGE.json (run once)")
    sub.add_parser("muse-prompt", help="print the paste-ready Muse-side onboarding text")

    s = sub.add_parser("send", help="atomically deliver a message (+optional attachments)")
    s.add_argument("to")
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--body-file", help="read body from file ('-' for stdin)")
    s.add_argument("--attach", action="append", default=[], help="attachment path (repeatable)")
    s.add_argument("--kind", choices=KINDS, default="task")
    s.add_argument("--reply-to")
    s.add_argument("--requires-approval", action="store_true")

    s = sub.add_parser("inbox", help="list/mirror unacked messages; NEVER acks")
    s.add_argument("--json", action="store_true")
    s.add_argument("--body", action="store_true", help="print full bodies")

    s = sub.add_parser("read", help="print one raw message; NEVER acks")
    s.add_argument("id")

    s = sub.add_parser("attachments", help="download + sha256-verify attachments")
    s.add_argument("id")
    s.add_argument("--out")

    s = sub.add_parser("ack", help="confirm handled messages (the only consuming op)")
    s.add_argument("ids", nargs="+")

    s = sub.add_parser("acked", help="check whether the peer has acked a sent message")
    s.add_argument("id")

    sub.add_parser("ls", help="show mailbox overview")

    s = sub.add_parser("watch", help="long-running watcher; NEVER acks")
    s.add_argument("--once", action="store_true", help="one poll then exit (for launchd/cron)")

    s = sub.add_parser("doctor", help="read-only health check")
    s.add_argument("--legacy-script", help="also scan an older client for auto-ack-on-fetch")

    s = sub.add_parser("selftest", help="run the bundled end-to-end regression")
    s.add_argument("--verbose", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    overrides = {}
    for key in ("backend", "local_root", "mail_root", "me", "peer"):
        v = getattr(args, key, None)
        if v:
            overrides[key] = v
    if getattr(args, "base_url", None) or getattr(args, "http_token_file", None):
        h = {}
        if args.base_url:
            h["base_url"] = args.base_url
        if args.http_token_file:
            h["token_file"] = args.http_token_file
        overrides["http"] = h
    try:
        cfg = Config(args.config, overrides)
    except BridgeError as e:
        eprint(f"[bridge] config error: {e}")
        return 2

    if args.cmd == "init":
        return cmd_init(args, cfg)
    if args.cmd == "muse-prompt":
        return cmd_muse_prompt(cfg)
    if args.cmd == "selftest":
        st = Path(__file__).with_name("selftest.py")
        return subprocess.call([sys.executable, str(st)] +
                              (["--verbose"] if args.verbose else []))

    try:
        br = Bridge(cfg)
    except BridgeError as e:
        eprint(f"[bridge] {e}")
        return 2

    try:
        c = args.cmd
        if c == "rendezvous-init":
            meta = br.init_rendezvous()
            print(json.dumps(meta, ensure_ascii=False, indent=2))
            print(f"\nCreated on {br.be.describe()}. Run this ONCE; the other side only needs the config.")
            return 0
        if c == "send":
            body = args.text
            if args.body_file:
                body = (sys.stdin.read() if args.body_file == "-" else
                        Path(os.path.expanduser(args.body_file)).read_text("utf-8"))
            if not body.strip():
                eprint("[bridge] empty body; use 'text' or --body-file")
                return 2
            mid, bell = br.send(args.to, body, attachments=args.attach, kind=args.kind,
                                reply_to=args.reply_to,
                                requires_approval=args.requires_approval)
            print(f"delivered {mid} -> {args.to}  (bell={bell})")
            print(f"not consumed until peer acks. check: bridge.py acked {mid}")
            return 0
        if c == "inbox":
            items = br.sync_inbox()
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            elif not items:
                print("no unacked messages.")
            else:
                for x in items:
                    flag = " [NEEDS APPROVAL]" if x["requires_approval"] else ""
                    print(f"{x['id']}  from={x['from']}  kind={x['kind']}{flag}")
                    print(f"    {x['summary']}")
                print(f"\n{len(items)} unacked. Nothing was consumed. "
                      f"After handling: bridge.py ack <id>")
            if args.body:
                for x in items:
                    print("\n" + "=" * 68 + f"\n{x['id']}\n" + "=" * 68)
                    print(br.read(x["id"])["raw"].rstrip())
            return 0
        if c == "read":
            print(br.read(args.id)["raw"].rstrip())
            return 0
        if c == "attachments":
            files = br.download_attachments(args.id, args.out)
            for f in files:
                print(f"verified {f}")
            if not files:
                print("no attachments on this message")
            return 0
        if c == "ack":
            rc = 0
            for mid in args.ids:
                ok, note = br.ack(mid)
                print(f"{mid}: {note}")
                if not ok:
                    rc = 1
            return rc
        if c == "acked":
            print(f"{args.id}: {'ACKED by peer' if br.peer_acked(args.id) else 'NOT ACKED yet'}")
            return 0
        if c == "ls":
            me, peer = cfg.me, cfg.peer
            print(f"rendezvous : {br.be.describe()}")
            print(f"mail_root  : {cfg.mail_root}")
            print(f"{me}/ unacked      : {len(br.list_inbox(me))}")
            print(f"{me}/ done         : {len(br.list_done(me))}")
            print(f"{peer}/ awaiting peer: {len(br.list_inbox(peer))}")
            print(f"{peer}/ peer acked   : {len(br.list_done(peer))}")
            return 0
        if c == "watch":
            br.watch(once=args.once)
            return 0
        if c == "doctor":
            return cmd_doctor(br, args)
    except BridgeError as e:
        eprint(f"[bridge] {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
