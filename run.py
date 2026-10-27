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


def service_is_ready(url, instance):
    try:
        # Local health checks must not use the system's HTTP proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + "/api/health", timeout=2) as response:
            health = json.load(response)
        return (isinstance(health, dict) and health.get("app_id") == "funasr-local-studio"
                and health.get("instance") == instance and health.get("status") == "ok")
    except (OSError, ValueError, TypeError):
        return False


def select_port(first, instance):
    for port in range(first, min(first + 10, 65536)):
        try:
            with socket.socket() as probe:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("127.0.0.1", port))
            return port, False
        except OSError:
            if service_is_ready(f"http://127.0.0.1:{port}", instance):
                return port, True
    raise SystemExit("No available port. Close another local server and try again.")


def monitor_existing(url, instance, port):
    """Stay alive while reusing a server; take over only after its port closes."""
    misses, warned = 0, False
    while True:
        time.sleep(1)
        if service_is_ready(url, instance):
            misses, warned = 0, False
            continue
        misses += 1
        if misses < 3:
            continue
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            return
        if not warned:
            print("Existing backend is not responding yet; waiting without stopping it.", flush=True)
            warned = True


def open_when_ready(url, instance):
    for _ in range(100):
        if service_is_ready(url, instance):
            webbrowser.open(url)
            return
        time.sleep(.3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    instance = hashlib.sha256(str(Path(__file__).resolve().parent).encode()).hexdigest()[:12]
    while True:
        port, existing = select_port(args.port, instance)
        url = f"http://127.0.0.1:{port}"
        if existing:
            print(f"Backend is running: {url}\nThis window will monitor it and take over if it stops. Keep this window open.", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                monitor_existing(url, instance, port)
            except KeyboardInterrupt:
                return
            print("Previous backend has stopped. Starting it in this window...", flush=True)
            continue
        if not args.no_browser:
            threading.Thread(target=open_when_ready, args=(url, instance), daemon=True).start()
        print(f"Local speech studio: {url}\nKeep this window open. Press Ctrl+C to stop.", flush=True)
        import uvicorn
        uvicorn.run("backend.app:app", host="127.0.0.1", port=port, log_level="info", access_log=False)
        return


if __name__ == "__main__":
    main()
