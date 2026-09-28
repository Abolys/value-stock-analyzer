"""Share the app with a link, for feedback from a friend.

    python scripts/share.py

1. Makes sure .env has APP_OWNER_PASSWORD and APP_VIEWER_PASSWORD (generates strong ones if missing
   and prints them). Give your friend the viewer password: read-only, no LLM calls, nothing saved.
2. Downloads Cloudflare's cloudflared into .cache/cloudflared (gitignored) if needed.
3. Starts the app on localhost:APP_PORT if it isn't already running.
4. Opens a Cloudflare quick tunnel (free, no account) and prints the public https link.

The link works while this script runs; Ctrl-C closes it (the app keeps running locally). Each run
gets a new random link. The app itself still listens on localhost only: visitors reach it only through
the tunnel, and must sign in.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

ENV = ROOT / ".env"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STARTUP_WAIT_SECONDS = 60  # for the app to answer and the tunnel to report its link


def ensure_passwords() -> dict[str, str]:
    pw = config.app_passwords()
    missing = [k for k, v in pw.items() if not v]
    if missing:
        lines = ENV.read_text().splitlines() if ENV.exists() else []
        lines = [line for line in lines if not any(line.startswith(f"{k}=") for k in missing)]
        for k in missing:
            pw[k] = secrets.token_urlsafe(12)
            lines.append(f"{k}={pw[k]}")
            os.environ[k] = pw[k]
        ENV.write_text("\n".join(lines) + "\n")
        print(f"Generated {', '.join(missing)} in .env")
    return pw


def ensure_cloudflared() -> Path:
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    exe = config.CLOUDFLARED_DIR / "cloudflared"
    if not exe.exists():
        config.CLOUDFLARED_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Downloading cloudflared from {config.CLOUDFLARED_URL} …")
        with requests.get(config.CLOUDFLARED_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(exe, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    print(subprocess.run([str(exe), "--version"], capture_output=True, text=True).stdout.strip())
    return exe


def app_up() -> bool:
    try:
        return requests.get(f"http://localhost:{config.APP_PORT}/_stcore/health", timeout=3).ok
    except requests.RequestException:
        return False


def ensure_app() -> None:
    if app_up():
        print(f"App already running on localhost:{config.APP_PORT}")
        return
    log = config.SCREEN_LOG_DIR / "app_share.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "main.py"), "--server.headless",
                      "true", "--server.port", str(config.APP_PORT)], cwd=ROOT, start_new_session=True,
                     stdout=open(log, "a"), stderr=subprocess.STDOUT, env=os.environ.copy())
    for _ in range(STARTUP_WAIT_SECONDS):
        if app_up():
            print(f"Started the app on localhost:{config.APP_PORT} (log: {log})")
            return
        time.sleep(1)
    raise SystemExit(f"The app didn't start within {STARTUP_WAIT_SECONDS}s; see {log}")


def main() -> int:
    pw = ensure_passwords()
    exe = ensure_cloudflared()
    ensure_app()
    tunnel = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{config.APP_PORT}"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    deadline = time.time() + STARTUP_WAIT_SECONDS
    for line in tunnel.stdout:
        m = URL_RE.search(line)
        if m:
            url = m.group(0)
            break
        if time.time() > deadline:
            break
    if not url:
        tunnel.terminate()
        raise SystemExit("cloudflared didn't report a link; try again in a minute")
    (config.CLOUDFLARED_DIR / "share_url.txt").write_text(url + "\n")
    print(f"\nShare link: {url}\n"
          f"  your friend's password (read-only): {pw['APP_VIEWER_PASSWORD']}\n"
          f"  your password (full access):        {pw['APP_OWNER_PASSWORD']}\n"
          "The link works while this runs; press Ctrl-C to close it.", flush=True)
    try:
        for _ in tunnel.stdout:  # keep draining the tunnel's log so it never blocks
            pass
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.terminate()
        print("Tunnel closed; the app keeps running locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
