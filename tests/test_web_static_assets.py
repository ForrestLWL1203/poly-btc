import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WebStaticAssetsTests(unittest.TestCase):
    def test_index_script_assets_are_tracked(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        scripts = re.findall(r'<script\s+src="([^"]+)"', html)

        missing = []
        untracked = []
        for src in scripts:
            rel = src.split("?", 1)[0].lstrip("/")
            path = ROOT / "web" / rel if not rel.startswith("web/") else ROOT / rel
            if not path.exists():
                missing.append(rel)
                continue
            res = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(path.relative_to(ROOT))],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode != 0:
                untracked.append(rel)

        self.assertEqual([], missing, f"missing static script assets: {missing}")
        self.assertEqual([], untracked, f"static script assets must be tracked for VPS deploy: {untracked}")


if __name__ == "__main__":
    unittest.main()
