
#!/usr/bin/env python3
"""Peekaboo MCP/Bridge soak test. Python 3 stdlib only; no HTTP gateway.

Two modes:
  captures: sustained ScreenCaptureKit capture of the desktop (default)
  ui: Finder About dialog, capture, focus a NEW Terminal window,
      type a harmless echo command, press Return, capture, repeat.

Run with an already-running, permission-granted Peekaboo.app Bridge host.
The UI mode intentionally takes foreground control of the Mac. Do not work
at the keyboard while it runs. English Finder menu names are assumed.
"""
import argparse
import base64
import datetime as dt
import json
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROTO = "2024-11-05"

class ReproFailure(Exception):
    pass

class MCP:
    def __init__(self, argv, folder, timeout):
        self.folder, self.timeout = folder, timeout
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0,
                                     env={**os.environ, "PEEKABOO_LOG_LEVEL": "debug"})
        self.pending = bytearray()
        self.next_id = 0
        self.stderr_path = folder / "peekaboo.stderr.log"
        self.stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        self.stderr_thread.start()

    def _stderr_reader(self):
        with self.stderr_path.open("ab", buffering=0) as out:
            while True:
                data = self.proc.stderr.read(16384)
                if not data:
                    break
                out.write(data)

    def send(self, obj):
        encoded = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        try:
            self.proc.stdin.write(encoded)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ReproFailure("Peekaboo stdio closed: %s" % e)

    def receive(self, rpc_id):
        deadline = time.monotonic() + self.timeout
        while True:
            end = self.pending.find(b"\n")
            if end >= 0:
                raw = bytes(self.pending[:end]).rstrip(b"\r")
                del self.pending[:end+1]
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    raise ReproFailure("Non-JSON stdout from MCP server: %r" % raw[:300])
                if msg.get("id") == rpc_id:
                    return msg
                # Out-of-band MCP notifications are allowed and ignored.
                if "id" in msg:
                    raise ReproFailure("Unexpected RPC response ID %r, wanted %r" %
                                       (msg["id"], rpc_id))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReproFailure("stdio timeout after %.1fs waiting for RPC id=%s" %
                                   (self.timeout, rpc_id))
            readable, _, _ = select.select([self.proc.stdout.fileno()], [], [], remaining)
            if not readable:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise ReproFailure("MCP stdout closed (exit=%s)" % self.proc.poll())
            self.pending.extend(chunk)
            if len(self.pending) > 100 * 1024 * 1024:
                raise ReproFailure("MCP returned >100MB without newline")

    def request(self, method, params=None):
        self.next_id += 1
        rpc_id = self.next_id
        req = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params is not None:
            req["params"] = params
        start = time.monotonic()
        self.send(req)
        try:
            result = self.receive(rpc_id)
            return result, round(time.monotonic() - start, 3)
        except Exception as e:
            elapsed = round(time.monotonic() - start, 3)
            raise ReproFailure("%s id=%d failed at %.3fs: %s" %
                               (method, rpc_id, elapsed, e))

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.stderr_thread.join(timeout=2)


def summarize_result(msg):
    result = msg.get("result") or {}
    meta = result.get("_meta") or {}
    content = result.get("content") or []
    texts = [str(c.get("text", ""))[:400] for c in content if c.get("type") == "text"]
    return {
        "rpc_error": msg.get("error"),
        "isError": bool(result.get("isError")),
        "meta": meta,
        "text_excerpt": " | ".join(texts)[:600],
        "image_count": sum(c.get("type") == "image" for c in content),
    }


def last_image_bytes(msg):
    for content in reversed((msg.get("result") or {}).get("content") or []):
        if content.get("type") == "image" and content.get("data"):
            kind = content.get("mimeType", "image/png")
            extension = ".jpg" if kind == "image/jpeg" else ".webp" if kind == "image/webp" else ".png"
            try:
                return base64.b64decode(content["data"], validate=True), extension
            except Exception:
                return None
    return None


def diagnostic(folder):
    out = []
    commands = [
        ["sw_vers"], ["uname", "-a"], ["peekaboo", "--version"],
        ["peekaboo", "permissions", "status"],
        ["ps", "-ax", "-o", "pid,ppid,stat,%cpu,command"],
        ["lsof", "-n", "-U"],
    ]
    for command in commands:
        out.append("\n$ %s\n" % " ".join(command))
        try:
            p = subprocess.run(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=7, text=True,
                               errors="replace")
            lines = p.stdout.splitlines()
            if command[0] in ("ps", "lsof"):
                lines = [line for line in lines if "peekaboo" in line.lower() or "bridge.sock" in line.lower()]
            out.append("\n".join(lines)[:20000] + "\n")
        except Exception as e:
            out.append("FAILED %s\n" % e)
    (folder / "diagnostics.txt").write_text("".join(out))


def window_ids(msg):
    result = msg.get("result") or {}
    rows = (result.get("_meta") or {}).get("windows") or []
    ids = set()
    for row in rows:
        if isinstance(row, dict):
            for key in ("id", "window_id", "windowID", "windowId"):
                try:
                    n = int(row.get(key))
                    if n > 0:
                        ids.add(n)
                        break
                except (ValueError, TypeError):
                    pass
    # For older Peekaboo releases with text-only listings.
    if not ids:
        for c in result.get("content") or []:
            if c.get("type") == "text":
                ids.update(int(x) for x in re.findall(r"\[ID:\s*(\d+)", c.get("text", "")))
    return ids


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["captures", "ui"], default="captures")
    ap.add_argument("--peekaboo", default="peekaboo", help="Path to Peekaboo CLI")
    ap.add_argument("--bridge-socket", default="~/Library/Application Support/Peekaboo/bridge.sock")
    ap.add_argument("--local", action="store_true", help="Do not use external Bridge socket (control case)")
    ap.add_argument("--timeout", type=float, default=40.0, help="Max response wait, seconds")
    ap.add_argument("--slow", type=float, default=10.0, help="Stop if a tool takes this many seconds")
    ap.add_argument("--pause", type=float, default=0.5, help="Delay between loops")
    ap.add_argument("--iterations", type=int, default=0, help="0 means until first failure / Ctrl-C")
    ap.add_argument("--save-every", type=int, default=0, help="Save every Nth screenshot (0=last only)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    if args.timeout <= 0 or args.slow <= 0 or args.pause < 0 or args.iterations < 0:
        ap.error("timeout and slow must be positive, iterations and pause nonnegative")
    target = shutil.which(args.peekaboo)
    if not target:
        ap.error("Peekaboo executable not found: %s" % args.peekaboo)
    socket = str(Path(args.bridge_socket).expanduser())
    if not args.local and not Path(socket).exists():
        ap.error("Bridge socket does not exist: %s" % socket)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(args.out or "peekaboo-repro-%s" % stamp).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=False)
    argv = [target, "mcp", "--allow-foreground"]
    if not args.local:
        argv += ["--bridge-socket", socket]
    env = {"args": vars(args), "argv": argv, "platform": platform.platform(), "python": sys.version}
    (folder / "config.json").write_text(json.dumps(env, indent=2))
    log_file = (folder / "calls.jsonl").open("w", buffering=1)
    mcp = None
    latest_image = None
    loops = 0

    def record(stage, msg, elapsed, arguments, iteration):
        nonlocal latest_image
        summary = summarize_result(msg)
        row = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"),
               "iteration": iteration, "stage": stage, "elapsed_seconds": elapsed,
               "arguments": arguments, **summary}
        log_file.write(json.dumps(row, default=str) + "\n")
        img = last_image_bytes(msg)
        if img:
            latest_image = img
            if args.save_every and iteration % args.save_every == 0:
                (folder / ("capture-%05d-%s%s" % (iteration, stage, img[1]))).write_bytes(img[0])
        detail = summary["meta"].get("effect", "")
        print("%s #%d %-20s %6.2fs %s %s" %
              (time.strftime("%H:%M:%S"), iteration, stage, elapsed,
               "ERROR" if summary["isError"] or summary["rpc_error"] else "OK", detail), flush=True)
        if summary["rpc_error"]:
            raise ReproFailure("RPC error at %s: %s" % (stage, summary["rpc_error"]))
        # Peekaboo can deliver keyboard events and still say completion is unverified.
        # We do not retry such non-idempotent input. We capture a fresh observation next.
        if summary["isError"]:
            uncertain = (summary["meta"].get("mutation_dispatched") is True and
                         summary["meta"].get("effect") == "unverifiable")
            if not uncertain:
                raise ReproFailure("Tool error at %s: %s" % (stage, summary["text_excerpt"]))
            print("  NOTE: unverified dispatch; not retrying input", flush=True)
        if elapsed >= args.slow:
            raise ReproFailure("SLOW call at %s took %.2fs (threshold %.2fs)" %
                               (stage, elapsed, args.slow))
        return msg

    def call(name, arguments, iteration=0):
        msg, elapsed = mcp.request("tools/call", {"name": name, "arguments": arguments})
        return record(name, msg, elapsed, arguments, iteration)

    try:
        print("Results: %s" % folder, flush=True)
        mcp = MCP(argv, folder, args.timeout)
        msg, elapsed = mcp.request("initialize", {
            "protocolVersion": PROTO,
            "capabilities": {},
            "clientInfo": {"name": "peekaboo-stdio-repro", "version": "1.0"}})
        if msg.get("error"):
            raise ReproFailure("Initialize failed: %s" % msg["error"])
        (folder / "server-info.json").write_text(json.dumps(msg, indent=2))
        mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        catalog, elapsed = mcp.request("tools/list")
        if catalog.get("error"):
            raise ReproFailure("tools/list failed: %s" % catalog["error"])
        tools = {x["name"]: x for x in catalog.get("result", {}).get("tools", [])}
        (folder / "tool-schemas.json").write_text(json.dumps(tools, indent=2))
        needed = {"see"} if args.mode == "captures" else {"app", "window", "menu", "see", "type", "press"}
        if not needed.issubset(tools):
            raise ReproFailure("Missing MCP tools: %s" % sorted(needed - set(tools)))
        print("MCP ready in %.2fs, %d tools, mode=%s" % (elapsed, len(tools), args.mode))

        terminal_id = None

        if args.mode == "ui":
            inventory = call("window", {
                "action": "list",
                "app": "Terminal"
            })

            eligible = []
            for item in inventory.get("result", {}).get("content", []):
                if item.get("type") != "text":
                    continue
                text = item.get("text", "")
                matches = re.findall(
                    r"\[ID:\s*(\d+)[^\]\n]*"
                    r"Observation:\s*combined_eligible",
                    text
                )
                eligible.extend(int(x) for x in matches)

            eligible = sorted(set(eligible))
            if len(eligible) != 1:
                raise ReproFailure(
                    f"Expected one eligible Terminal window, got {eligible}"
                )

            terminal_id = eligible[0]
            print(f"Using Terminal window {terminal_id}")

            call("window", {
                "action": "focus",
                "app": "Terminal",
                "window_id": terminal_id
            })

        while not args.iterations or loops < args.iterations:
            loops += 1
            if args.mode == "captures":
                call("see", {"app_target": "screen"}, loops)
            else:
                call("app", {"action": "focus", "name": "Finder", "foreground": True}, loops)
                # About Finder is accessible via an application menu on macOS.
                # The Apple logo menu is system-owned and less portable in AX.
                call("menu", {
                    "action": "click",
                    "app": "Finder",
                    "path": "Finder > About Finder"
                }, loops)

                result = call("verify_state", {
                    "app": "Finder",
                    "window_title": "About Finder",
                    "predicates": [
                        {"kind": "window_exists", "expected": True}
                    ],
                    "stable_samples": 2,
                    "timeout_ms": 5000
                }, loops)

                call("see", {"app_target": "frontmost"}, loops)
                call("press", {"key": "w", "modifiers": ["cmd"],
                               "app": "Finder", "foreground": True}, loops)
                call("window", {
                    "action": "focus",
                    "app": "Terminal",
                    "window_id": terminal_id
                }, loops)
                call("see", {"app_target": "Terminal", "window_id": terminal_id,
                             }, loops)
                call("type", {"text": "echo PEEKABOO_REPRO_%05d" % loops,
                              "app": "Terminal", "window_id": terminal_id,
                              "foreground": True}, loops)
                call("press", {"key": "Return", "app": "Terminal",
                               "window_id": terminal_id, "foreground": True}, loops)
                call("see", {"app_target": "Terminal", "window_id": terminal_id,
                             }, loops)
            if args.pause:
                time.sleep(args.pause)
        print("Completed %d iterations with no detected failure" % loops)
    except KeyboardInterrupt:
        print("Interrupted by user", flush=True)
    except Exception as e:
        print("REPRO TRIGGERED after %d completed iteration(s): %s" % (loops - 1, e),
              file=sys.stderr, flush=True)
        (folder / "failure.txt").write_text("%s\n" % e)
        diagnostic(folder)  # Collect while original MCP/Bridge process is still running.
        return_code = 1
    else:
        return_code = 0
    finally:
        if latest_image:
            (folder / ("last-capture" + latest_image[1])).write_bytes(latest_image[0])
        if mcp:
            mcp.close()
        log_file.close()
        print("Logs: %s" % folder, flush=True)
    return locals().get("return_code", 130)

if __name__ == "__main__":
    sys.exit(main())
