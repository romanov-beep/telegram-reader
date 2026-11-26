import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


class SourcePackageTests(unittest.TestCase):
    def test_archive_uses_only_committed_files(self):
        source_script = Path(__file__).resolve().parents[1] / "scripts" / "package-source.py"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            checkout = workspace / "reader"
            checkout.mkdir()
            script = checkout / "scripts" / source_script.name
            script.parent.mkdir()
            shutil.copyfile(source_script, script)

            def git(*args):
                subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)

            git("init", "-q")
            committed = {
                "plugin.json": json.dumps({"name": "unofficial-telegram-reader"}),
                "README.md": "Public readme",
                "TODO.md": "Public tasks",
                ".github/workflows/tdlib.yml": "name: TDLib",
                ".gitignore": "*.pem\n.env\n*.sqlite*\n",
            }
            for name, contents in committed.items():
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")
            git("add", "--", *committed, "scripts/package-source.py")
            git("-c", "user.name=Reader Tests", "-c", "user.email=tests@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "-qm", "Public files")

            for name in ("docs/sample.pem", "docs/sample.sqlite-wal", "deploy/.env"):
                path = checkout / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("Temporary test data", encoding="utf-8")
            (checkout / "README.md").write_text("Local edit", encoding="utf-8")
            git("add", "--", "README.md")

            output = workspace / "source.zip"
            subprocess.run([sys.executable, str(script), str(output)], check=True, capture_output=True)
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                prefix = "unofficial-telegram-reader/"
                archived_files = {item.filename for item in archive.infolist() if not item.is_dir()}
                expected = set(committed) | {"scripts/package-source.py"}
                self.assertEqual(archived_files, {prefix + path for path in expected})
                self.assertEqual(archive.read(prefix + "README.md"), b"Public readme")
