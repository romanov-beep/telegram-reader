#!/usr/bin/env python3
"""Export only reviewed source components; never sweep up sessions or config."""
import json
from pathlib import Path
import sys
import zipfile


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 scripts/package-source.py OUTPUT.zip")
    root = Path(__file__).resolve().parent.parent
    output = Path(sys.argv[1]).absolute()
    if output.is_relative_to(root):
        raise SystemExit("Write the archive outside the source directory")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    package_name = manifest["name"]
    files = [root / path for path in ("plugin.json", "mcp.json", "README.md", "LICENSE", "run.py", ".gitignore", "requirements.txt", "constraints.txt")]
    for directory in ("telegram_reader", "tests", "scripts", "skills", "docs", "deploy"):
        files.extend(path for path in (root / directory).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_symlink():
                raise SystemExit("Refusing a symlink")
            archive.write(path, str(Path(package_name) / path.relative_to(root)))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise SystemExit("Archive checksum failed")
        for name in archive.namelist():
            if name.endswith(".json"):
                json.loads(archive.read(name))
        assert len({name.split("/")[0] for name in archive.namelist()}) == 1
        assert not any(name.endswith(("config.json", ".sqlite", ".pyc")) for name in archive.namelist())
    print(str(output))


if __name__ == "__main__":
    main()
