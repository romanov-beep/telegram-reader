#!/usr/bin/env python3
"""Export the committed source tree from the standalone Reader checkout."""
import json
from pathlib import Path
import re
import subprocess
import sys


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 scripts/package-source.py OUTPUT.zip")
    root = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).resolve()
    if output.is_relative_to(root):
        raise SystemExit("Write the archive outside the source directory")

    try:
        checkout = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root)
    except (FileNotFoundError, subprocess.CalledProcessError):
        raise SystemExit("Run this script from the standalone Reader Git checkout") from None
    if Path(checkout.decode("utf-8").strip()).resolve() != root:
        raise SystemExit("Run this script from the standalone Reader Git checkout")

    commit = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD"], cwd=root)
    commit_id = commit.decode("ascii").strip()
    manifest = subprocess.check_output(["git", "show", f"{commit_id}:plugin.json"], cwd=root)
    package_name = json.loads(manifest)["name"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", package_name):
        raise SystemExit("Invalid plugin name in the committed manifest")

    package = subprocess.check_output(
        ["git", "archive", "--format=zip", f"--prefix={package_name}/", commit_id], cwd=root
    )
    with output.open("xb") as archive:
        archive.write(package)
    print(str(output))


if __name__ == "__main__":
    main()
