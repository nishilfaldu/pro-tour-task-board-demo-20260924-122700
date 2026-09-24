"""Pinned, trusted GitHub media preflight clients; never run inside the PR app."""

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from pro_tour.sandbox import SandboxError, SandboxSession

GH_VERSION = "2.101.0"
ARCHIVES = {
    "macOS_arm64": ("zip", "e4303e39d8f07141c4bad4b99b01079f05029c59b27076e8fbc825c985ecdd8b"),
    "linux_arm64": ("tar.gz", "b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f"),
    "linux_amd64": ("tar.gz", "9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8"),
}
MEDIA_HOSTS = frozenset({"api.github.com", "uploads.github.com"})
LABELS = ("App tour · upload check", "Feature tour · upload check")
FIXTURE_BODY = (
    LABELS[0]
    + "\n\n![](/opt/fixtures/app.mp4)\n\n"
    + LABELS[1]
    + ("\n\n![](/opt/fixtures/feature.mp4)\n")
)


def install_gh(archive: Path, destination: Path, platform: str) -> dict:
    """Extract only the executable from a verified release asset into a new path."""
    if platform not in ARCHIVES or destination.exists():
        raise SandboxError("Use a supported GitHub CLI platform and a new destination")
    extension, expected = ARCHIVES[platform]
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != expected:
        raise SandboxError("GitHub CLI release archive checksum mismatch")
    prefix = f"gh_{GH_VERSION}_{platform}"
    member = prefix + "/bin/gh"
    if extension == "zip":
        with zipfile.ZipFile(archive) as source:
            binary = source.read(member)
    else:
        with tarfile.open(archive) as source:
            entry = source.getmember(member)
            if not entry.isfile():
                raise SandboxError("GitHub CLI executable is not a regular file")
            binary = source.extractfile(entry).read()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(binary)
    destination.chmod(0o755)
    return {
        "version": GH_VERSION,
        "platform": platform,
        "archive_sha256": digest,
        "binary_sha256": hashlib.sha256(binary).hexdigest(),
        "url": f"https://github.com/cli/cli/releases/download/v{GH_VERSION}/{prefix}.{extension}",
    }


def read_media_token(path: Path) -> str:
    with path.open("rb") as stream:
        data = stream.read(16385)
    if len(data) > 16384:
        raise SandboxError("Credential file is too large")
    matches = re.findall(r"^\s*(?:export\s+)?GH_MEDIA_TOKEN\s*=(.*)$", data.decode(), re.MULTILINE)
    if len(matches) != 1:
        raise SandboxError("Expected one literal media token assignment")
    token = matches[0].strip()
    if token.startswith(("'", '"')):
        if len(token) < 3 or token[-1] != token[0]:
            raise SandboxError("Malformed media token assignment")
        token = token[1:-1]
    if not token or len(token) > 8192 or any(c in token for c in "$`\r\n\0"):
        raise SandboxError("Invalid literal media token")
    return token


def run_cli(binary: Path, token: str, argv: list[str], input_text: str | None = None) -> dict:
    """Trusted argv only; no ambient config, login, credentials, or executable lookup.

    The container's namespace and fixed gateway enforce the network boundary.
    This helper alone is not isolation for PR-controlled code.
    """
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise SandboxError("Invalid GitHub CLI argument vector")
    with tempfile.TemporaryDirectory(prefix="pro-tour-gh-") as temporary:
        environment = {
            "HOME": temporary,
            "GH_CONFIG_DIR": temporary + "/config",
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "GH_TOKEN": token,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "HTTPS_PROXY": "http://127.0.0.1:8184",
        }
        try:
            result = subprocess.run(
                [str(binary), *argv],
                cwd=temporary,
                env=environment,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SandboxError("GitHub CLI failed or exceeded its deadline") from None
    if len(result.stdout.encode()) > 4 * 1024 * 1024 or len(result.stderr.encode()) > 32768:
        raise SandboxError("GitHub CLI output budget exceeded")
    if token and (token in result.stdout or token in result.stderr):
        raise SandboxError("GitHub CLI echoed a credential; output withheld")
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "credential_names": ["GH_TOKEN"],
        "ambient_config_excluded": True,
        "helper_settings": [],
    }


def validate_comment(body: str) -> list[str]:
    if not isinstance(body, str) or len(body) > 2048:
        raise SandboxError("Invalid media comment body")
    paragraphs = body.strip().split("\n\n")
    if len(paragraphs) != 4 or (paragraphs[0], paragraphs[2]) != LABELS:
        raise SandboxError("Media comment must contain only two labels and two videos")
    urls = [paragraphs[1], paragraphs[3]]
    if len(set(urls)) != 2 or any(
        not re.fullmatch(
            r"https://github\.com/user-attachments/assets/"
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            url,
        )
        for url in urls
    ):
        raise SandboxError("Media comment contains an unexpected or duplicate attachment URL")
    return urls


class MediaSession(SandboxSession):
    CHANNELS = ("media",)
    MOUNTS = {"gateway": (("media", False),), "uploader": (("media", True),)}
    NETWORKS = {"gateway": "bridge"}
    MEMORY = {"uploader": "256m"}

    def start_clients(self):
        self.start("gateway", ["python", "/opt/media/gateway.py"])
        self.start("uploader", ["python", "/opt/media/runtime.py", "idle"])

    def invoke(self, token: str, argv: list[str], input_text: str | None = None) -> dict:
        payload = json.dumps({"token": token, "argv": argv, "input": input_text})
        if len(payload.encode()) > 1024 * 1024:
            raise SandboxError("GitHub media request exceeds its size limit")
        return json.loads(
            self.docker.run(
                "exec",
                "-i",
                self.containers["uploader"],
                "/usr/bin/env",
                "-i",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "python",
                "/opt/media/runtime.py",
                "invoke",
                input_text=payload,
                timeout=130,
            )
        )
