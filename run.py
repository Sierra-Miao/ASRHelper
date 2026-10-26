"""Run both the local API and its build-free frontend."""
import argparse
import hashlib
import json
import socket
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    instance = hashlib.sha256(str(Path(__file__).resolve().parent).encode()).hexdigest()[:12]
    for port in range(args.port, min(args.port + 10, 65536)):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            break
        except OSError:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                    health = json.load(response)
                if health.get("app_id") == "funasr-local-studio" and health.get("instance") == instance:
                    print(f"Already running: http://127.0.0.1:{port}", flush=True)
                    if not args.no_browser:
                        webbrowser.open(f"http://127.0.0.1:{port}")
                    return
            except Exception:
                continue
    else:
        raise SystemExit("No available port. Close another local server and try again.")
    url = f"http://127.0.0.1:{port}"
    if not args.no_browser:
        def open_when_ready():
            for _ in range(100):
                try:
                    with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except Exception:
                    time.sleep(0.3)
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Local speech studio: {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
    import uvicorn
    uvicorn.run("backend.app:app", host="127.0.0.1", port=port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
