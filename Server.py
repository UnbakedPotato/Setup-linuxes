#!/usr/bin/env python3
"""Local UI server for llama.cpp.

- serves index.html
- stores chats in ./chats
- scans MODELS_DIR (recursively) for .gguf files
- starts/stops llama-server for the model you pick in the UI
- proxies the UI's requests to that llama-server

Run:  python server.py
"""
import atexit, difflib, json, os, re, subprocess, threading, time
import urllib.error, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ============================ CONFIG: EDIT THESE ============================
# Path to your llama-cli binary. llama-cli itself has no HTTP API, so the
# script uses the llama-server binary that sits in the same folder.
LLAMA_CLI = os.environ.get("LLAMA_CLI", "/home/potato/llama.cpp/build/bin/llama-cli")

# Folder with your models. Every subfolder is scanned for *.gguf files.
MODELS_DIR = os.environ.get("MODELS_DIR", "/home/potato/models")

# Extra flags passed to llama-server, e.g. ["--jinja", "-ngl", "99", "-c", "8192"]
EXTRA_ARGS = ["--jinja, -ngl 99"]

PORT = int(os.environ.get("PORT", "8000"))             # the page you open on the phone
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8081"))  # internal llama-server port
# Only set this if llama-server is NOT next to llama-cli:
LLAMA_SERVER = os.environ.get("LLAMA_SERVER", str(Path(LLAMA_CLI).expanduser().with_name("llama-server")))
# ===========================================================================

ROOT = Path(__file__).parent
CHATS = ROOT / "chats"
CHATS.mkdir(exist_ok=True)
LOG = ROOT / "llama-server.log"
ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHARD = re.compile(r"-(\d+)-of-\d+\.gguf$", re.I)
UPSTREAM = f"http://127.0.0.1:{LLAMA_PORT}"


# ------------------------------ model scanning ------------------------------
def find_mmproj(model: Path):
    cands = [f for f in model.parent.glob("*.gguf") if "mmproj" in f.name.lower()]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    return max(cands, key=lambda f: difflib.SequenceMatcher(None, f.stem.lower(), model.stem.lower()).ratio())


def scan_models():
    root = Path(MODELS_DIR).expanduser()
    found = {}
    if not root.is_dir():
        return found
    for dirpath, _dirs, files in os.walk(root, followlinks=True):
        for name in sorted(files):
            if not name.lower().endswith(".gguf") or "mmproj" in name.lower():
                continue
            m = SHARD.search(name)
            if m and int(m.group(1)) != 1:  # only list the first shard of split models
                continue
            f = Path(dirpath) / name
            rid = str(f.relative_to(root))
            found[rid] = {"id": rid, "name": rid[:-5], "path": f,
                          "size": f.stat().st_size, "mmproj": find_mmproj(f)}
    return dict(sorted(found.items()))


# ------------------------------ llama-server -------------------------------
class Llama:
    def __init__(self):
        self.proc = None
        self.loaded = None
        self.state = "idle"  # idle | loading | ready | error
        self.error = ""
        self.lock = threading.Lock()

    def snapshot(self):
        if self.state in ("ready", "loading") and self.proc and self.proc.poll() is not None:
            self.state, self.error = "error", "llama-server exited.\n" + self.log_tail()
        return self.state

    def log_tail(self, n=8):
        try:
            return "\n".join(LOG.read_text(errors="ignore").strip().splitlines()[-n:])
        except Exception:
            return ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.loaded, self.state = None, None, "idle"

    def healthy(self):
        try:
            with urllib.request.urlopen(UPSTREAM + "/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def load(self, rid):
        model = scan_models().get(rid)
        if not model:
            return False, "Model not found in MODELS_DIR."
        if not Path(LLAMA_SERVER).expanduser().exists():
            return False, f"llama-server not found at {LLAMA_SERVER}. Fix LLAMA_CLI / LLAMA_SERVER."
        with self.lock:
            if self.loaded == rid and self.snapshot() == "ready":
                return True, ""
            self.stop()
            self.state, self.error = "loading", ""
            cmd = [str(Path(LLAMA_SERVER).expanduser()), "-m", str(model["path"]),
                   "--host", "127.0.0.1", "--port", str(LLAMA_PORT)]
            if model["mmproj"]:
                cmd += ["--mmproj", str(model["mmproj"])]
            cmd += EXTRA_ARGS
            with open(LOG, "w") as lf:
                lf.write("$ " + " ".join(cmd) + "\n")
                self.proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
            self.loaded = rid
            deadline = time.time() + 900
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    self.state, self.error = "error", "llama-server exited.\n" + self.log_tail()
                    self.loaded = None
                    return False, self.error
                if self.healthy():
                    self.state = "ready"
                    return True, ""
                time.sleep(1)
            self.stop()
            self.state, self.error = "error", "Timed out waiting for the model to load."
            return False, self.error


llama = Llama()
atexit.register(llama.stop)


# ---------------------------------- HTTP ------------------------------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def send_bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", code)

    def proxy(self, method):
        url = UPSTREAM + self.path[len("/llama"):]
        data = self.body() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            up = urllib.request.urlopen(req, timeout=900)
            code = up.status
        except urllib.error.HTTPError as e:
            up, code = e, e.code
        except Exception as e:
            return self.send_json({"error": f"llama-server not reachable: {e}"}, 502)
        self.send_response(code)
        self.send_header("Content-Type", up.headers.get("Content-Type", "application/json"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        read = getattr(up, "read1", up.read)
        try:
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # Stop pressed on the phone: closing upstream stops generation
        finally:
            up.close()
            self.close_connection = True

    def handle_any(self, method):
        p = self.path.split("?")[0]
        if p.startswith("/llama/"):
            return self.proxy(method)
        if p in ("/", "/index.html") and method == "GET":
            return self.send_bytes((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")

        if p == "/api/models" and method == "GET":
            models = [{"id": m["id"], "name": m["name"], "vision": bool(m["mmproj"]),
                       "size_gb": round(m["size"] / 1e9, 1)} for m in scan_models().values()]
            return self.send_json({"models": models, "loaded": llama.loaded,
                                   "state": llama.snapshot(), "error": llama.error})
        if p == "/api/status" and method == "GET":
            return self.send_json({"loaded": llama.loaded, "state": llama.snapshot(), "error": llama.error})
        if p == "/api/load" and method == "POST":
            try:
                rid = json.loads(self.body() or b"{}").get("id", "")
            except Exception:
                rid = ""
            ok, err = llama.load(rid)
            return self.send_json({"ok": ok, "error": err}, 200 if ok else 500)

        if p == "/chats" and method == "GET":
            items = []
            for f in CHATS.glob("*.json"):
                try:
                    c = json.loads(f.read_text())
                    items.append({"id": c["id"], "title": c.get("title", "New chat"), "updated": c.get("updated", 0)})
                except Exception:
                    pass
            items.sort(key=lambda x: x["updated"], reverse=True)
            return self.send_json(items)
        m = re.match(r"^/chats/([^/]+)$", p)
        if m and ID.match(m.group(1)):
            f = CHATS / (m.group(1) + ".json")
            if method == "GET":
                return self.send_bytes(f.read_bytes(), "application/json") if f.exists() else self.send_json({}, 404)
            if method == "PUT":
                f.write_bytes(self.body())
                return self.send_json({"ok": True})
            if method == "DELETE":
                f.unlink(missing_ok=True)
                return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_DELETE(self): self.handle_any("DELETE")


if __name__ == "__main__":
    print(f"llama-server binary : {LLAMA_SERVER}  ({'ok' if Path(LLAMA_SERVER).expanduser().exists() else 'NOT FOUND'})")
    print(f"models folder       : {MODELS_DIR}  ({len(scan_models())} gguf models found)")
    print(f"open on your phone  : http://<laptop-ip>:{PORT}")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except KeyboardInterrupt:
        pass
