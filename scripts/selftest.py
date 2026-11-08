#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end regression for muse-bridge. Runs entirely in a temp directory.

Uses the local backend, so it exercises the SAME Bridge/ack/attachment code as
production without touching any real rendezvous host, config or mailbox.

    python3 selftest.py [--verbose]

Exit 0 = all PASS, 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bridge as B  # noqa: E402

RESULTS = []
VERBOSE = False


def check(name, fn):
    try:
        detail = fn() or ""
        RESULTS.append(("PASS", name, str(detail)))
    except Exception as e:
        RESULTS.append(("FAIL", name, f"{type(e).__name__}: {e}"))
        if VERBOSE:
            import traceback
            traceback.print_exc()


def assert_that(cond, msg):
    if not cond:
        raise AssertionError(msg)


def make_fixture():
    tmp = Path(tempfile.mkdtemp(prefix="muse-bridge-selftest-"))
    os.environ["MUSE_BRIDGE_STATE"] = str(tmp / "state")
    cfg = B.Config(path=str(tmp / "no-config.json"), overrides={
        "backend": "local",
        "local_root": str(tmp / "rendezvous"),
        "mail_root": "/srv/muse-bridge/mail",
        "me": "workbuddy",
        "peer": "muse",
        "bell": {"enabled": False, "host": "", "port": 18765, "token_file": ""},
        "log_max_bytes": 1048576,
    })
    br = B.Bridge(cfg)
    br.init_rendezvous()
    return tmp, cfg, br


def v1_read(raw: bytes):
    """Simulate a legacy v1 reader: 4 known headers, blank line, then body."""
    lines = raw.decode("utf-8").split("\n")
    heads = {}
    i = 0
    for i, line in enumerate(lines):
        if not line.strip():
            break
        if ":" in line:
            k, v = line.split(":", 1)
            heads[k.strip()] = v.strip()
    body = "\n".join(lines[i + 1:])
    return {k: heads.get(k) for k in ("Id", "From", "To", "Ts")}, body


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    VERBOSE = ap.parse_args().verbose
    tmp, cfg, br = make_fixture()
    sent = []

    try:
        # 1 ------------------------------------------------------------------
        def t_layout():
            assert_that(br.be.is_dir(br.box("workbuddy")), "workbuddy box missing")
            assert_that(br.be.is_dir(br.box("muse")), "muse box missing")
            meta = br.read_rendezvous_meta()
            assert_that(meta and meta["mail_root"] == cfg.mail_root, "BRIDGE.json drift")
            assert_that(sorted(meta["participants"]) == ["muse", "workbuddy"], "participants")
            return "dirs + canonical BRIDGE.json"
        check("rendezvous layout and canonical path", t_layout)

        # 2 ------------------------------------------------------------------
        def t_send():
            mid, bell = br.send("muse", "第一封：请复核协议\n正文第二行", kind="ask")
            sent.append(mid)
            assert_that(B.valid_id(mid), "id format not v1-compatible")
            assert_that(mid in br.list_inbox("muse"), "message not in peer inbox")
            assert_that(bell is None, "bell should be off by default")
            parsed = B.parse_message(br.be.read_bytes(br.msg_path("muse", mid)))
            for k in ("Id", "From", "To", "Ts"):
                assert_that(k in parsed["headers"], f"missing v1 header {k}")
            return f"{mid} atomically committed"
        check("atomic send and v1-shaped headers", t_send)

        # 3 ------------------------------------------------------------------
        def t_read_not_consume():
            before = len(br.list_inbox("muse"))
            for _ in range(3):
                br.sync_inbox()
                br.list_inbox("muse")
            assert_that(len(br.list_inbox("muse")) == before, "reading consumed a message")
            assert_that(br.read(sent[0], who="muse")["body"].startswith("第一封"),
                        "body unreadable")
            return "3 reads, 0 consumed"
        check("read never consumes", t_read_not_consume)

        # 4 ------------------------------------------------------------------
        def t_v1_compat():
            raw = br.be.read_bytes(br.msg_path("muse", sent[0]))
            heads, body = v1_read(raw)
            assert_that(all(heads.values()), "v1 header parse failed")
            assert_that(heads["Id"] == sent[0], "v1 id mismatch")
            assert_that("第一封" in body, "v1 body parse failed")
            assert_that("X-Bridge-Version" not in body.split("\n")[0], "extension leaked into body")
            return "legacy reader sees 4 headers + intact body"
        check("backward compatibility with v1 readers", t_v1_compat)

        # 5 ------------------------------------------------------------------
        def t_headers():
            mid, _ = br.send("muse", "回复：确认收到", kind="reply",
                             reply_to=sent[0], requires_approval=True)
            sent.append(mid)
            h = B.parse_message(br.be.read_bytes(br.msg_path("muse", mid)))["headers"]
            assert_that(h["X-Kind"] == "reply", "kind lost")
            assert_that(h["X-Reply-To"] == sent[0], "reply-to lost")
            assert_that(h["X-Requires-Approval"] == "true", "approval flag lost")
            return "kind / reply-to / approval round-trip"
        check("structured v2 headers", t_headers)

        # 6 ------------------------------------------------------------------
        payload = tmp / "cover.png"
        payload.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload" * 37)
        expected_hash = B.sha256_bytes(payload.read_bytes())

        def t_attach_commit_order():
            mid, _ = br.send("muse", "附件测试：封面", attachments=[str(payload)])
            sent.append(mid)
            h = B.parse_message(br.be.read_bytes(br.msg_path("muse", mid)))["headers"]
            manifest = json.loads(h["X-Attachments"])
            assert_that(manifest[0]["sha256"] == expected_hash, "manifest hash wrong")
            assert_that(br.be.exists(f"{br.attach_dir('muse', mid)}/cover.png"),
                        "attachment missing at commit point")
            return "files exist before the .md commit point"
        check("attachment commit ordering + manifest", t_attach_commit_order)

        def t_attach_fetch():
            mid = sent[-1]
            # download_attachments reads from the local (cfg.me) box; mirror the
            # peer-bound message there to exercise the receive-side path.
            src = br.be.read_bytes(f"{br.attach_dir('muse', mid)}/cover.png")
            br.be.write_atomic(f"{br.attach_dir('workbuddy', mid)}/cover.png", src)
            msg = br.be.read_bytes(br.msg_path("muse", mid))
            br.be.write_atomic(br.msg_path("workbuddy", mid), msg)
            files = br.download_attachments(mid)
            assert_that(len(files) == 1, "no verified file returned")
            assert_that(Path(files[0]).read_bytes() == payload.read_bytes(), "bytes differ")
            return "sha256 verified, bytes intact"
        check("attachment download + sha256 verification", t_attach_fetch)

        def t_attach_tamper():
            mid = sent[-1]
            br.be.write_atomic(f"{br.attach_dir('workbuddy', mid)}/cover.png", b"corrupted")
            try:
                br.download_attachments(mid)
            except B.BridgeError as e:
                assert_that("sha256 mismatch" in str(e), "wrong error")
                return "tampering detected"
            raise AssertionError("tampered attachment was accepted")
        check("corrupt attachment rejected", t_attach_tamper)

        # 7 ------------------------------------------------------------------
        def t_ack():
            mid = sent[-1]
            br.be.write_atomic(f"{br.attach_dir('workbuddy', mid)}/cover.png",
                               payload.read_bytes())
            assert_that(mid in br.list_inbox(), "test message missing")
            ok, note = br.ack(mid)
            assert_that(ok, f"ack failed: {note}")
            assert_that(mid not in br.list_inbox(), "message still in inbox after ack")
            assert_that(mid in br.list_done(), "message not archived in done/")
            assert_that(br.be.is_dir(br.done_attach_dir("workbuddy", mid)),
                        "attachments not archived with the message")
            assert_that(not br.be.is_dir(br.attach_dir("workbuddy", mid)),
                        "orphan attachment dir left behind")
            return "message + attachments archived together"
        check("explicit ack is the only consuming operation", t_ack)

        def t_double_ack():
            ok, _ = br.ack(sent[-1])
            assert_that(not ok, "second ack should fail")
            return "idempotent"
        check("double ack is safe", t_double_ack)

        # 8 ------------------------------------------------------------------
        def t_concurrent_ack():
            mid, _ = br.send("workbuddy", "并发领取测试")
            outcomes = []

            def worker():
                b2 = B.Bridge(cfg)
                try:
                    outcomes.append(b2.ack(mid)[0])
                except Exception as e:
                    outcomes.append(f"ERR:{e}")

            ts = [threading.Thread(target=worker) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert_that(outcomes.count(True) == 1, f"expected exactly 1 winner: {outcomes}")
            return f"6 racers, exactly 1 winner ({outcomes.count(False)} no-ops)"
        check("concurrent ack cannot double-consume", t_concurrent_ack)

        # 9 ------------------------------------------------------------------
        def t_bell_loss():
            cfg.data["bell"] = {"enabled": True, "host": "127.0.0.1", "port": 1,
                                "token_file": str(tmp / "no-such-token")}
            mid, bell = br.send("muse", "门铃不可达也不许丢信")
            sent.append(mid)
            assert_that(bell is False, "bell should report failure")
            assert_that(mid in br.list_inbox("muse"), "message lost when bell failed")
            cfg.data["bell"]["enabled"] = False
            return "bell down, message durable"
        check("bell failure never loses a message", t_bell_loss)

        # 10 -----------------------------------------------------------------
        def t_watch_no_ack():
            mid, _ = br.send("workbuddy", "看门人不得代签")
            before = set(br.list_inbox())
            assert_that(mid in before, "watch test message missing")
            buf = io.StringIO()
            with redirect_stdout(buf):
                B.Bridge(cfg).watch(once=True)
            after = set(br.list_inbox())
            assert_that(before == after, "watch consumed a message")
            assert_that("NEVER auto-acks" in buf.getvalue(), "watcher contract not stated")
            br.ack(mid)
            return "watcher mirrored without consuming"
        check("watch never acks", t_watch_no_ack)

        # 11 -----------------------------------------------------------------
        def t_approval_flag():
            mid, _ = br.send("workbuddy", "请删除生产数据", requires_approval=True)
            items = {x["id"]: x for x in br.sync_inbox()}
            assert_that(items[mid]["requires_approval"], "approval flag not surfaced")
            br.ack(mid)
            return "consequential request flagged for human approval"
        check("consequential request surfaced", t_approval_flag)

        # 12 -----------------------------------------------------------------
        def t_peer_acked():
            mid, _ = br.send("muse", "请签收")
            assert_that(not br.peer_acked(mid), "phantom ack")
            br.be.mkdir(f"{br.box('muse')}/done")
            data = br.be.read_bytes(br.msg_path("muse", mid))
            br.be.write_atomic(br.done_path("muse", mid), data)
            assert_that(br.peer_acked(mid), "peer ack not detected")
            return "outbound delivery is verifiable"
        check("peer ack visibility", t_peer_acked)

        # 13 -----------------------------------------------------------------
        def t_no_secrets():
            src = Path(B.__file__).read_text("utf-8")
            import re
            hits = re.findall(r"(?:BUS_TOKEN|TOKEN|token)\s*=\s*[\"']([A-Za-z0-9_\-]{24,})[\"']", src)
            assert_that(not hits, f"literal credential found: {hits}")
            ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", src)
            bad = [x for x in ips if x not in ("127.0.0.1", "0.0.0.0")]
            assert_that(not bad, f"hardcoded IP literal found in client source: {bad}")
            return "no literal token or host in client source"
        check("no hardcoded credentials or hosts", t_no_secrets)

        # 14 -----------------------------------------------------------------
        def t_interop():
            import subprocess
            bash = shutil.which("bash")
            if not bash:
                return "SKIPPED (bash unavailable on this host)"
            init_sh = Path(B.__file__).parent.parent / "assets" / "rendezvous-init.sh"
            assert_that(init_sh.is_file(), "rendezvous-init.sh missing from assets")
            t2 = Path(tempfile.mkdtemp(prefix="muse-bridge-interop-"))
            try:
                root = t2 / "mail"
                r = subprocess.run([bash, str(init_sh), str(root), "workbuddy", "muse"],
                                   capture_output=True, text=True)
                assert_that(r.returncode == 0, f"rendezvous-init failed: {r.stderr}")
                mb = str(root / "mb.sh")
                adir = t2 / "files"
                adir.mkdir()
                (adir / "cover.png").write_bytes(b"PNGDATA-1234567890")
                r = subprocess.run([mb, "send", "workbuddy", "muse", str(adir)],
                                   input="cross-client attachment test\n",
                                   capture_output=True, text=True)
                mid = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
                assert_that(B.valid_id(mid),
                            f"bad id from bash client: {r.stdout!r} {r.stderr!r}")
                cfg2 = B.Config(path=str(t2 / "none.json"), overrides={
                    "backend": "local", "local_root": str(root),
                    "mail_root": str(root), "me": "workbuddy", "peer": "muse"})
                br2 = B.Bridge(cfg2)
                assert_that(mid in br2.list_inbox(), "python cannot see bash-delivered mail")
                files = br2.download_attachments(mid)
                assert_that(Path(files[0]).read_bytes() == b"PNGDATA-1234567890",
                            "attachment bytes differ across clients")
                assert_that(br2.ack(mid)[0], "python ack failed")
                r = subprocess.run([mb, "inbox", "workbuddy"], capture_output=True, text=True)
                assert_that(mid not in r.stdout, "bash still sees the acked message")
                r = subprocess.run([mb, "doctor"], capture_output=True, text=True,
                                   env={**os.environ, "MB_ME": "workbuddy"})
                assert_that("no orphan attachments" in r.stdout,
                            "orphan attachments left after ack")
                return "bash send -> python verify/ack -> bash sees it consumed"
            finally:
                assert_that(t2.name.startswith("muse-bridge-interop-"), "unsafe cleanup")
                shutil.rmtree(t2, ignore_errors=True)
        check("bash / python cross-client interop", t_interop)

        # 15 -----------------------------------------------------------------
        def t_doctor():
            class A:
                legacy_script = None
            stray = tmp / "rendezvous" / "workbuddy" / "stray-cover.png"
            stray.write_bytes(b"orphan")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                rc = B.cmd_doctor(br, A())
            out = buf.getvalue()
            assert_that(rc == 0, f"doctor reported FAIL:\n{out}")
            assert_that("loose files in inbox" in out and "stray-cover.png" in out,
                        "doctor missed a loose attachment in the inbox root")
            stray.unlink()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                assert_that(B.cmd_doctor(br, A()) == 0, "doctor FAIL after cleanup")
            return "detects stray attachments, then reports 0 FAIL"
        check("doctor detects loose attachments", t_doctor)

        # 16-20 --------------------------------------------------------------
        # VPS-free transport: the same protocol over outbound HTTP, served by
        # serve.py. Everything runs against a throwaway in-process server.
        import urllib.error
        import urllib.parse
        import urllib.request
        import serve as S
        from concurrent.futures import ThreadPoolExecutor

        S.RendezvousHandler.log_message = lambda self, *a: None
        saved_state = os.environ.get("MUSE_BRIDGE_STATE")
        htmp = Path(tempfile.mkdtemp(prefix="muse-bridge-http-"))
        htok = htmp / "http-token"
        htok.write_text("selftest-token-0123456789abcdef\n", "utf-8")
        os.chmod(htok, 0o600)
        os.environ["MUSE_BRIDGE_STATE"] = str(htmp / "state")
        hsrv = S.RendezvousServer(("127.0.0.1", 0), str(htmp / "mail"),
                                  "/srv/muse-bridge/mail",
                                  "selftest-token-0123456789abcdef",
                                  8 * 1024 * 1024, ["workbuddy", "muse"])
        hth = threading.Thread(target=hsrv.serve_forever,
                               kwargs={"poll_interval": 0.05}, daemon=True)
        hth.start()
        hbase = f"http://127.0.0.1:{hsrv.server_address[1]}"

        def http_cfg(me, peer, token_file=htok, base_url=hbase):
            return B.Config(path=str(htmp / "no-config.json"), overrides={
                "backend": "http", "mail_root": "/srv/muse-bridge/mail",
                "me": me, "peer": peer,
                "http": {"base_url": base_url, "token_file": str(token_file),
                         "timeout": 5},
                "bell": {"enabled": False, "host": "", "port": 18765,
                         "token_file": ""},
            })

        try:
            # 16
            def t_http_auth():
                with urllib.request.urlopen(hbase + "/v2/health", timeout=5) as r:
                    assert_that(r.status == 200, "health endpoint unavailable")
                q = urllib.parse.quote("/srv/muse-bridge/mail", safe="")
                try:
                    urllib.request.urlopen(hbase + "/v2/list?p=" + q, timeout=5)
                    raise AssertionError("unauthenticated list was accepted")
                except urllib.error.HTTPError as e:
                    assert_that(e.code == 401, f"expected 401, got {e.code}")
                assert_that("BRIDGE.json" in B.Bridge(http_cfg("workbuddy", "muse")).be.list_dir(
                    "/srv/muse-bridge/mail"), "authenticated list failed")
                try:
                    http_cfg("workbuddy", "muse", base_url="http://example.com")
                    raise AssertionError("plaintext remote http was accepted")
                except B.BridgeError:
                    pass
                return "health open, data needs bearer token, plaintext remote refused"
            check("http: auth + transport safety", t_http_auth)

            # 17
            def t_http_roundtrip():
                sender = B.Bridge(http_cfg("muse", "workbuddy"))
                recv = B.Bridge(http_cfg("workbuddy", "muse"))
                mid, _ = sender.send("workbuddy", "hello over http")
                items = recv.sync_inbox()
                assert_that(len(items) == 1 and items[0]["id"] == mid,
                            "recipient did not see the http-delivered message")
                assert_that(recv.read(mid)["body"].startswith("hello over http"),
                            "body corrupt over http")
                assert_that(mid in recv.list_inbox(), "read consumed a message")
                assert_that(recv.ack(mid)[0], "http ack failed")
                assert_that(sender.peer_acked(mid), "sender cannot see peer ack")
                return "send -> read (no consume) -> ack -> peer sees done/"
            check("http: full round trip", t_http_roundtrip)

            # 18
            def t_http_attachments():
                f = htmp / "cover.png"
                f.write_bytes(b"PNGDATA-OVER-HTTP-123456")
                sender = B.Bridge(http_cfg("muse", "workbuddy"))
                recv = B.Bridge(http_cfg("workbuddy", "muse"))
                mid, _ = sender.send("workbuddy", "cover attached",
                                     attachments=[str(f)])
                files = recv.download_attachments(mid)
                assert_that(len(files) == 1, "attachment not listed over http")
                assert_that(Path(files[0]).read_bytes() == b"PNGDATA-OVER-HTTP-123456",
                            "attachment bytes differ over http")
                assert_that(recv.ack(mid)[0], "ack with attachments failed")
                assert_that((htmp / "mail" / "workbuddy" / "done" / "attachments" / mid
                             / "cover.png").is_file(),
                            "attachments not archived together with the message")
                return "binary attachment + sha256 + archived on ack"
            check("http: attachments over the wire", t_http_attachments)

            # 19
            def t_http_sandbox():
                hdr = {"Authorization": "Bearer selftest-token-0123456789abcdef"}
                for bad in ("/etc/passwd", "/srv/muse-bridge/mail/../../etc/passwd"):
                    u = hbase + "/v2/list?p=" + urllib.parse.quote(bad, safe="")
                    try:
                        urllib.request.urlopen(urllib.request.Request(
                            u, headers=hdr), timeout=5)
                        raise AssertionError(f"path escaped sandbox: {bad}")
                    except urllib.error.HTTPError as e:
                        assert_that(e.code == 400, f"expected 400, got {e.code}")
                wrong = htmp / "wrong-token"
                wrong.write_text("definitely-wrong-token-000000\n", "utf-8")
                try:
                    B.Bridge(http_cfg("workbuddy", "muse", token_file=wrong)).list_inbox()
                    raise AssertionError("wrong token was accepted")
                except B.BridgeError:
                    pass
                return "traversal rejected, wrong token rejected"
            check("http: path sandbox + token enforcement", t_http_sandbox)

            # 20
            def t_http_concurrent_ack():
                sender = B.Bridge(http_cfg("muse", "workbuddy"))
                mid, _ = sender.send("workbuddy", "one winner only")
                cfg = http_cfg("workbuddy", "muse")
                with ThreadPoolExecutor(max_workers=4) as ex:
                    wins = list(ex.map(lambda _: B.Bridge(cfg).ack(mid)[0], range(4)))
                assert_that(sum(wins) == 1,
                            f"expected exactly 1 winning ack, got {sum(wins)}")
                return "4 concurrent acks -> exactly 1 winner"
            check("http: concurrent ack exactly one winner", t_http_concurrent_ack)
        finally:
            hsrv.shutdown()
            hsrv.server_close()
            if saved_state is None:
                os.environ.pop("MUSE_BRIDGE_STATE", None)
            else:
                os.environ["MUSE_BRIDGE_STATE"] = saved_state
            assert_that(htmp.name.startswith("muse-bridge-http-"), "unsafe cleanup")
            shutil.rmtree(htmp, ignore_errors=True)

    finally:
        # Only remove the temp dir this test created itself.
        assert_that(str(tmp).startswith(tempfile.gettempdir()), "refusing unsafe cleanup")
        assert_that(tmp.name.startswith("muse-bridge-selftest-"), "refusing unsafe cleanup")
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nmuse-bridge selftest v{B.VERSION}")
    print("=" * 88)
    for level, name, detail in RESULTS:
        print(f"{level:<5} {name:<48} {detail}")
    print("=" * 88)
    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    failed = len(RESULTS) - passed
    print(f"PASS={passed}  FAIL={failed}  TOTAL={len(RESULTS)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
