"""Small evidence primitives; raw provider/process output is never a report field."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_report(path: Path, report: dict) -> None:
    """Create a private report without overwriting or following an existing link.

    Only pass structured, sanitized metadata here. This is not a generic log
    redactor and must never receive prompts, credentials, or raw tool output.
    """
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
