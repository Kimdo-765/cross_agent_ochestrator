"""Start the web UI: pick a free local port, optionally open a Cloudflare tunnel, run uvicorn."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Optional

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def free_port(host: str = "127.0.0.1", preferred: int = 0) -> int:
    """Return ``preferred`` if it is free, otherwise an OS-assigned free port."""
    for candidate in ([preferred] if preferred else []) + [0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port available")


class Tunnel:
    """Cloudflare tunnel in front of the local server.

    - With ``CLOUDFLARE_TUNNEL_TOKEN`` set: runs a *named* tunnel (stable hostname you configured
      in the Cloudflare dashboard, pointed at http://localhost:<port>).
    - Otherwise: a *quick tunnel* (``cloudflared tunnel --url``) that prints a random
      ``*.trycloudflare.com`` URL -- no account needed, but the URL changes every start.
    """

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self.proc: Optional[subprocess.Popen] = None
        self.url: Optional[str] = None
        self._ready = threading.Event()

    def start(self, timeout: float = 30.0) -> Optional[str]:
        exe = shutil.which("cloudflared")
        if exe is None:
            print("cloudflared not found -- install it (https://github.com/cloudflare/cloudflared) or run without --tunnel",
                  file=sys.stderr)
            return None
        token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
        if token:
            argv = [exe, "tunnel", "--no-autoupdate", "run", "--token", token, "--url", f"http://{self.host}:{self.port}"]
        else:
            argv = [exe, "tunnel", "--no-autoupdate", "--url", f"http://{self.host}:{self.port}"]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump, daemon=True).start()
        if token:
            self.url = os.environ.get("CLOUDFLARE_TUNNEL_HOSTNAME") or "(named tunnel: see your Cloudflare dashboard)"
            self._ready.set()
        self._ready.wait(timeout)
        return self.url

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        log_path = Path(os.environ.get("CAO_DATA_DIR") or Path.home() / ".cao") / "cloudflared.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            for line in self.proc.stdout:
                log.write(line)
                log.flush()
                m = TUNNEL_URL_RE.search(line)
                if m and not self.url:
                    self.url = m.group(0)
                    self._ready.set()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def serve(host: str = "127.0.0.1", port: int = 0, tunnel: bool = False, data_dir: Optional[str] = None,
          open_browser: bool = True) -> int:
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("web UI needs the 'web' extra:  pip install 'cross-agent-orchestrator[web]'", file=sys.stderr)
        return 1
    if data_dir:
        os.environ["CAO_DATA_DIR"] = data_dir
    port = free_port(host, port or int(os.environ.get("CAO_PORT", "0") or 0))
    local_url = f"http://{host}:{port}"
    print(f"cao web UI: {local_url}", flush=True)

    tun: Optional[Tunnel] = None
    want_tunnel = tunnel or os.environ.get("CAO_TUNNEL") == "1"
    exposed = want_tunnel or host not in ("127.0.0.1", "localhost")
    token = os.environ.get("CAO_AUTH_TOKEN")
    if exposed and not token and os.environ.get("CAO_NO_AUTH") != "1":
        token = secrets.token_urlsafe(24)
        os.environ["CAO_AUTH_TOKEN"] = token
    if token:
        print(f"access token: {token}   (sign in at {local_url}/login?token={token})", flush=True)

    if want_tunnel:
        tun = Tunnel(port, host)
        url = tun.start()
        if url:
            os.environ["CAO_TUNNEL_URL"] = url
            print(f"cloudflare tunnel: {url}", flush=True)
            if token:
                print(f"remote sign-in:    {url}/login?token={token}", flush=True)
        else:
            print("tunnel did not come up (see ~/.cao/cloudflared.log); continuing locally", file=sys.stderr)

    if open_browser and os.environ.get("CAO_NO_BROWSER") != "1" and host in ("127.0.0.1", "localhost"):
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    from .app import create_app

    app = create_app()
    try:
        uvicorn.run(app, host=host, port=port, log_level=os.environ.get("CAO_LOG_LEVEL", "info"))
    finally:
        if tun:
            tun.stop()
    return 0
