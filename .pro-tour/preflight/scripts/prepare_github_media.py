"""Build the trusted media preflight image from a verified task-local CLI archive."""

import argparse
import shutil
import tempfile
from pathlib import Path

from pro_tour.evidence import sha256_file, timestamp, write_report
from pro_tour.github_media import FIXTURE_BODY, install_gh
from pro_tour.sandbox import Docker, SandboxError

ROOT = Path(__file__).resolve().parents[1]


def prepare(archive: Path, platform: str, output: Path):
    if output.exists() or platform not in {"linux_arm64", "linux_amd64"}:
        raise SandboxError("Use a new output path and a supported Linux platform")
    context = Path(tempfile.mkdtemp(prefix="pro-tour-media-image-"))
    release = install_gh(archive, context / "gh", platform)
    package = context / "pro_tour"
    package.mkdir()
    for name in ("__init__.py", "github_media.py", "sandbox.py"):
        shutil.copyfile(ROOT / "src/pro_tour" / name, package / name)
    for name in ("Dockerfile", "runtime.py", "gateway.py"):
        shutil.copyfile(ROOT / "containers/github-media" / name, context / name)
    shutil.copyfile(ROOT / "src/pro_tour/egress_proxy.py", context / "egress_proxy.py")
    (context / "body.md").write_text(FIXTURE_BODY)
    print(f"Building GitHub media preflight for {platform}", flush=True)
    Docker().run("build", "--iidfile", str(context / "image-id"), str(context), timeout=600)
    image = (context / "image-id").read_text().strip()
    write_report(
        output,
        {
            "kind": "github-media-image",
            "built_at": timestamp(),
            "image_id": image,
            "release": release,
            "external_context": str(context),
            "support_sha256": {
                str(p.relative_to(context)): sha256_file(p)
                for p in context.rglob("*")
                if p.is_file() and p.name != "gh"
            },
        },
    )
    print(image, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux_arm64", "linux_amd64"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.archive, args.platform, args.output)
