"""SSH tunnel for dev: local 127.0.0.1:54322 -> remote 127.0.0.1:5432 (PostgreSQL).

Loads .env from repo root (QP_HOST/QP_USER/QP_PASS). Run in background:
    python scripts/dev_pg_tunnel.py
Then point QP_PG_DSN at 127.0.0.1:54322 (see .env.example).
"""

from __future__ import annotations

import os
import select
import socketserver
import sys
import threading
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "contracts" / "src"))
from quantpilot_contracts.envfile import load_env_file  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PORT = int(os.environ.get("QP_TUNNEL_PORT", "54322"))
REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = 5432


def main() -> None:
    load_env_file(ROOT / ".env")
    host = os.environ["QP_HOST"]
    user = os.environ["QP_USER"]
    password = os.environ["QP_PASS"]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=15, banner_timeout=15)
    transport = client.get_transport()
    assert transport is not None

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                chan = transport.open_channel(
                    "direct-tcpip", (REMOTE_HOST, REMOTE_PORT), self.request.getpeername()
                )
            except Exception:
                return
            if chan is None:
                return
            try:
                while True:
                    rl, _, _ = select.select([self.request, chan], [], [], 60)
                    if not rl:
                        # idle: keep the forwarded connection open (pooled
                        # PG connections must survive idle periods)
                        continue
                    if self.request in rl:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in rl:
                        data = chan.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
            finally:
                chan.close()
                self.request.close()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(("127.0.0.1", LOCAL_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[tunnel] 127.0.0.1:{LOCAL_PORT} -> {host}:{REMOTE_HOST}:{REMOTE_PORT} UP", flush=True)
    try:
        threading.Event().wait()  # run forever
    finally:
        server.shutdown()
        client.close()
        print("[tunnel] down")


if __name__ == "__main__":
    main()
