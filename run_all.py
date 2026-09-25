"""Start the whole of Craftly, wired together, from one terminal.

    python run_all.py            # B2, A2+A1, C1, C2 — prices from C1's placeholder
    python run_all.py --b1       # ...and B1's price engine (needs engines/models/)
    python run_all.py --reseed   # start from a fresh B2 database

Standard library only, so it runs before anything is installed. Each
service is started with `uv run` when uv is on PATH (installing that
service's dependencies on first run), otherwise with this Python.

What it wires up, which running the services by hand does not:

* C1 reads the catalogue, passports and orders from B2 (`CRAFTLY_ADAPTERS=http`),
  and prices from B1 with `--b1`, else from its own placeholder engine.
* B2 and C2 share a service token, so C2 reads orders from B2 and a
  courier delivery settles the payout there.
* capture (which serves the Studio app) publishes to B2.

Ctrl+C stops everything.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Service-to-service addresses use 127.0.0.1: on Windows "localhost" tries IPv6
# first and every call between services waits ~2s for that to fail.
B2 = "http://127.0.0.1:8200"
B1 = "http://127.0.0.1:8010"
C1 = "http://127.0.0.1:8100"


def python_cmd(service_dir: Path, *args: str) -> list[str]:
    """uv if it is on PATH; else the service's own .venv (made by `uv sync`);
    else this Python, which only works if it has every service's packages."""
    if shutil.which("uv"):
        return ["uv", "run", "--directory", str(service_dir), "python", *args]
    venv = service_dir / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.exists():
        return [str(venv), *args]
    return [sys.executable, *args]


def port_busy(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for(url: str, seconds: int = 180) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--b1", action="store_true", help="also run B1's price engine (needs engines/models/)")
    parser.add_argument("--reseed", action="store_true", help="wipe and re-seed B2's database first")
    args = parser.parse_args()

    token = os.environ.get("CRAFTLY_SERVICE_TOKEN") or secrets.token_urlsafe(24)
    common = dict(os.environ, PYTHONIOENCODING="utf-8", CRAFTLY_SERVICE_TOKEN=token, CRAFTLY_PLATFORM_URL=B2)

    if args.b1 and not (ROOT / "engines/models/price_model/model.pkl").exists():
        sys.exit("--b1 needs B1's trained model in engines/models/price_model/ (see engines/README.md). "
                 "Run without --b1 to use placeholder prices.")

    ports = [8200, 8000, 8100, 8300] + ([8010] if args.b1 else [])
    busy = [port for port in ports if port_busy(port)]
    if busy:
        sys.exit(
            f"Port(s) {', '.join(map(str, busy))} already in use - Craftly is probably running "
            "in another terminal. Stop it there (Ctrl+C) first, or the old copy keeps answering."
        )

    platform = ROOT / "platform"
    db = platform / "craftly.db"
    if args.reseed and db.exists():
        for f in platform.glob("craftly.db*"):
            f.unlink()
    if not db.exists():
        print("Seeding B2's database...", flush=True)
        subprocess.run(python_cmd(platform, "-m", "app.seed"), cwd=platform, env=common, check=True)

    services = [
        ("B2 platform", "platform", ["-m", "uvicorn", "app.main:app", "--port", "8200"], {}, f"{B2}/health"),
        ("A2 capture + A1 Studio", "capture", ["-m", "uvicorn", "app.main:app", "--port", "8000"], {}, "http://127.0.0.1:8000/health"),
        ("C1 market", "market", ["-m", "uvicorn", "app.main:app", "--port", "8100"],
         {"CRAFTLY_ADAPTERS": "http", "CRAFTLY_PRICE_ADAPTERS": "http" if args.b1 else "stub",
          "CRAFTLY_ENGINES_URL": B1}, f"{C1}/health"),
        ("C2 integrations", "integrations", ["-m", "uvicorn", "c2.app:app", "--port", "8300"],
         {"CRAFTLY_C1_URL": C1}, "http://127.0.0.1:8300/api/health"),
    ]
    if args.b1:
        engines_python = ROOT / "engines" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cmd = [str(engines_python) if engines_python.exists() else sys.executable, "scripts/service.py", "serve", "--port", "8010"]
        services.insert(0, ("B1 engines", "engines", cmd, {}, f"{B1}/api/health"))

    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    procs: list[subprocess.Popen] = []
    try:
        for name, folder, cmd, env, _ in services:
            service_dir = ROOT / folder
            full = cmd if folder == "engines" else python_cmd(service_dir, *cmd)
            log = open(logs / f"{folder}.log", "w", encoding="utf-8")
            procs.append(subprocess.Popen(full, cwd=service_dir, env={**common, **env}, stdout=log, stderr=subprocess.STDOUT))
            print(f"  starting {name:<24} log: logs/{folder}.log", flush=True)
        for name, folder, _, _, health in services:
            status = "up" if wait_for(health) else f"NOT UP - see logs/{folder}.log"
            print(f"  {name:<24} {status}", flush=True)

        print(f"""
Craftly is running.
  Artisan app (Studio)   http://localhost:8000/studio/     demo phone 9000000001
  Shop (buyer app)       http://localhost:8100/
  B2B portal             http://localhost:8100/b2b
  Integrations console   http://localhost:8300/
  Platform API docs      http://localhost:8200/docs
Prices: {"B1's model" if args.b1 else "C1's placeholder engine (run with --b1 for B1's model)"}
Ctrl+C to stop.""", flush=True)
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("A service stopped; see logs/. Shutting the rest down.")
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
