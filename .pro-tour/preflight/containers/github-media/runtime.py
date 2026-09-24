"""Trusted media client; key arrives only on private stdin."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/media")
from pro_tour.github_media import run_cli  # noqa: E402
from pro_tour.sandbox import SandboxError  # noqa: E402

if __name__ == "__main__":
    if sys.argv[1] == "idle":
        os.execve(
            "/usr/bin/socat",
            [
                "socat",
                "TCP-LISTEN:8184,bind=127.0.0.1,reuseaddr,fork",
                "UNIX-CONNECT:/run/pro-tour/media/proxy.sock",
            ],
            {"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    data = json.loads(sys.stdin.read(1024 * 1024))
    try:
        result = run_cli(Path("/opt/gh"), data["token"], data["argv"], data.get("input"))
    except SandboxError as error:
        result = {"exit_code": 125, "stdout": "", "stderr": str(error)}
    print(json.dumps(result))
