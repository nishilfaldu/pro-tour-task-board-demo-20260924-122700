"""Create a synthetic demo PR or upload two preflight clips to an existing one.

This performs real GitHub writes. It is not the automatic product workflow.
The returned media comment still needs independent browser playback verification.
"""

import argparse
import base64
import json
import re
from pathlib import Path

from pro_tour.evidence import timestamp, write_report
from pro_tour.github_media import GH_VERSION, MediaSession, read_media_token, validate_comment
from pro_tour.sandbox import Docker, SandboxError

DEMO_NAME = "pro-tour-task-board-demo-20260924-122700"
DESCRIPTION = "Disposable synthetic task-board demonstration for Pro Tour run 20260924_122700"


def require(condition, message):
    if not condition:
        raise SandboxError(message)


class GitHub:
    def __init__(self, session, token, report):
        self.session, self.token, self.report = session, token, report

    def call(self, argv, operation, data=None):
        result = self.session.invoke(self.token, argv, data)
        require(
            result.get("credential_names") == ["GH_TOKEN"]
            and result.get("ambient_config_excluded") is True
            and result.get("helper_settings") == [],
            "GitHub credential boundary failed",
        )
        self.report["operations"].append(
            {
                "operation": operation,
                "exit_code": result["exit_code"],
                "credential_names": result["credential_names"],
                "ambient_config_excluded": True,
            }
        )
        return result

    def api(self, endpoint, operation, *, method="GET", data=None, missing_ok=False):
        args = ["api", "--include", "--method", method, endpoint]
        if data is not None:
            args += ["--input", "-"]
        result = self.call(args, operation, json.dumps(data) if data is not None else None)
        header, body = result["stdout"].replace("\r\n", "\n").split("\n\n", 1)
        status = int(header.splitlines()[0].split()[1])
        self.report["operations"][-1]["http_status"] = status
        if status == 404 and missing_ok:
            return None
        require(
            result["exit_code"] == 0 and 200 <= status < 300,
            f"GitHub {operation} failed (HTTP {status})",
        )
        return json.loads(body)


def bootstrap(github, owner):
    repo = owner + "/" + DEMO_NAME
    github.report["repo"] = repo
    existing = github.api("repos/" + repo, "find_demo", missing_ok=True)
    # Never mutate a preexisting repository by assuming it belongs to this run.
    require(existing is None, "Demo repository already exists; supply its repo/PR explicitly")
    metadata = github.api(
        "user/repos",
        "create_demo",
        method="POST",
        data={
            "name": DEMO_NAME,
            "description": DESCRIPTION,
            "private": False,
            "auto_init": True,
            "has_projects": False,
        },
    )
    github.report["created_repository"] = metadata["html_url"]
    default = metadata["default_branch"]
    baseline = github.api(f"repos/{repo}/git/ref/heads/{default}", "baseline_ref")["object"]["sha"]
    github.api(
        f"repos/{repo}/git/refs",
        "create_preflight_branch",
        method="POST",
        data={
            "ref": "refs/heads/media-preflight",
            "sha": baseline,
        },
    )
    content = (
        "# Media preflight\n\nThese synthetic clips verify token-authenticated GitHub attachments. "
        "They are not app/feature acceptance tours. The task-board baseline and its real "
        "status-filter feature PR will be installed separately.\n"
    )
    github.api(
        f"repos/{repo}/contents/MEDIA-PREFLIGHT.md",
        "commit_preflight_marker",
        method="PUT",
        data={
            "message": "Add synthetic media preflight marker",
            "branch": "media-preflight",
            "content": base64.b64encode(content.encode()).decode(),
        },
    )
    pr = github.api(
        f"repos/{repo}/pulls",
        "create_preflight_pr",
        method="POST",
        data={
            "title": "Pro Tour media upload preflight",
            "head": "media-preflight",
            "base": default,
            "body": "Synthetic upload and inline-playback check. These are not acceptance tours. "
            "The automatic task-board workflow and feature change remain separate work.",
        },
    )
    return repo, pr["number"]


def check(image: str, output: Path, secrets: Path, repo: str | None, pr: int | None) -> int:
    require(not output.exists(), "Use a new evidence output path")
    require(
        (repo is None and pr is None)
        or (
            isinstance(repo, str)
            and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo)
            and isinstance(pr, int)
            and pr > 0
        ),
        "Supply both a valid repo and PR, or neither",
    )
    session = MediaSession(Docker(), image)
    report = {
        "kind": "github-media-upload-preflight",
        "started_at": timestamp(),
        "image_id": image,
        "status": "failed",
        "full_pipeline_verified": False,
        "inline_playback_verified": False,
        "operations": [],
        "phase": "startup",
        "real_media_credential_used": False,
    }
    try:
        with session:
            session.start_clients()
            host = session.inspect("uploader")["HostConfig"]
            require(
                host["NetworkMode"] == "none"
                and host["ReadonlyRootfs"]
                and not host["Privileged"]
                and not host["PidMode"]
                and not host["PortBindings"],
                "Unexpected uploader boundary",
            )
            require(
                all(
                    m["Type"] == "volume" and m["RW"] is False
                    for m in session.inspect("uploader")["Mounts"]
                ),
                "Unexpected uploader mount",
            )
            report["runtime"] = {
                k: host[k]
                for k in ("NetworkMode", "ReadonlyRootfs", "CapDrop", "SecurityOpt", "Memory")
            }
            version = session.execute("uploader", ["/opt/gh", "--version"]).strip()
            require(
                version.startswith("gh version " + GH_VERSION + " "), "Wrong GitHub CLI version"
            )
            require(
                "--attach" in session.execute("uploader", ["/opt/gh", "pr", "comment", "--help"]),
                "Pinned CLI does not support attachments",
            )
            report["cli_version"] = version
            # Real default-deny gateway probes precede reading the actual token.
            probe = """import json,socket
results={}
hosts=('api.github.com','uploads.github.com','api.typesafe.ai','api2.cursor.sh','127.0.0.1','[::1]')
for host in hosts:
 with socket.create_connection(('127.0.0.1',8184),timeout=10) as s:
  s.sendall(('CONNECT '+host+':443 HTTP/1.1\\r\\n\\r\\n').encode())
  results[host]=s.recv(4096).decode().split('\\r\\n')[0]
print(json.dumps(results))
"""
            probes = json.loads(session.execute("uploader", ["python", "-c", probe], timeout=60))
            require(
                all("200" in probes[h] for h in ("api.github.com", "uploads.github.com"))
                and all(
                    "403" in probes[h]
                    for h in ("api.typesafe.ai", "api2.cursor.sh", "127.0.0.1", "[::1]")
                ),
                "GitHub gateway policy probe failed",
            )
            report["precredential_egress"] = probes
            fixtures = """import hashlib,json,subprocess
from pathlib import Path
result={}
for name in ('app','feature'):
 p=Path('/opt/fixtures')/(name+'.mp4')
 meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(p)]))
 subprocess.run(['ffmpeg','-v','error','-i',str(p),'-f','null','-'],check=True)
 s=meta['streams'][0]
 result[name]={k:s[k] for k in ('codec_name','pix_fmt','width','height','nb_frames','duration')}
 result[name].update(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),bytes=p.stat().st_size)
print(json.dumps(result))
"""
            clips = json.loads(session.execute("uploader", ["python", "-c", fixtures], timeout=60))
            require(
                all(
                    c["codec_name"] == "h264"
                    and c["pix_fmt"] == "yuv420p"
                    and int(c["nb_frames"]) == 96
                    and float(c["duration"]) == 4
                    and 0 < c["bytes"] < 10_000_000
                    for c in clips.values()
                )
                and clips["app"]["sha256"] != clips["feature"]["sha256"],
                "Invalid preflight clips",
            )
            report["clips"] = clips
            token = read_media_token(secrets)
            report["real_media_credential_used"] = True
            github = GitHub(session, token, report)
            account = github.api("user", "authenticate_media_token")
            report["owner"] = account["login"]
            if repo is None:
                report["phase"] = "bootstrap"
                repo, pr = bootstrap(github, account["login"])
            report.update(repo=repo, pr=pr)
            metadata = github.api("repos/" + repo, "read_repository")
            require(metadata["permissions"]["push"], "Media token lacks repository write access")
            report["repository"] = {
                k: metadata[k] for k in ("id", "full_name", "html_url", "visibility", "permissions")
            }
            pull = github.api(f"repos/{repo}/pulls/{pr}", "read_pull_request")
            report["pull_request"] = {
                "url": pull["html_url"],
                "head_sha": pull["head"]["sha"],
                "base_sha": pull["base"]["sha"],
            }
            report["phase"] = "upload"
            print(f"Uploading two preflight clips to {repo} PR {pr}", flush=True)
            result = github.call(
                [
                    "pr",
                    "comment",
                    str(pr),
                    "--repo",
                    repo,
                    "--body-file",
                    "/opt/fixtures/body.md",
                    "--attach",
                    "/opt/fixtures/app.mp4",
                    "--attach",
                    "/opt/fixtures/feature.mp4",
                ],
                "upload_two_attachments",
            )
            if result["exit_code"]:
                report["upload_failure"] = {
                    "stderr": result["stderr"][:4096],
                    "stdout": result["stdout"][:4096],
                    "partial_publication_possible": True,
                }
            require(
                result["exit_code"] == 0,
                "GitHub attachment command failed; partial publication is possible",
            )
            url = result["stdout"].strip()
            match = re.fullmatch(
                re.escape(f"https://github.com/{repo}/pull/{pr}#issuecomment-") + r"([0-9]+)", url
            )
            require(match is not None, "Unexpected media comment URL")
            report["phase"] = "readback"
            comment = github.api(f"repos/{repo}/issues/comments/{match[1]}", "read_media_comment")
            require(
                comment["user"]["login"] == account["login"] and comment["html_url"] == url,
                "Unexpected comment author or URL",
            )
            urls = validate_comment(comment["body"])
            report["comment"] = {
                "id": comment["id"],
                "url": url,
                "body": comment["body"],
                "media_urls": urls,
            }
            egress = session.execute("gateway", ["cat", "/tmp/media-egress.jsonl"])
            report["egress"] = sorted(
                {json.loads(line)["destination"] for line in egress.splitlines()}
            )
            require(
                all(
                    h in {"api.github.com", "uploads.github.com"} or h.startswith("denied")
                    for h in report["egress"]
                ),
                "Unexpected GitHub client egress",
            )
            report["status"] = "passed"
    except (SandboxError, OSError, ValueError, KeyError, TypeError) as error:
        report["error"] = (
            str(error) if isinstance(error, SandboxError) else "Invalid media preflight data"
        )
    finally:
        report["cleanup"] = (
            "verified" if not session.containers and not session.volumes else "failed"
        )
        if report["cleanup"] != "verified":
            report["status"] = "failed"
        report["finished_at"] = timestamp()
        write_report(output, report)
    return int(report["status"] != "passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--repo")
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    raise SystemExit(check(args.image, args.output, args.secrets_file, args.repo, args.pr))
