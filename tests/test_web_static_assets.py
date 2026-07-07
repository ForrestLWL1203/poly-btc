import re
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from hl import api


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

    def test_index_response_cache_busts_compiled_assets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "index.html").write_text(
                '<link rel="stylesheet" href="/app.css" />'
                '<script src="/app.js"></script>',
                encoding="utf-8",
            )
            (root / "app.css").write_text("body{}", encoding="utf-8")
            (root / "app.js").write_text("window.__ok=1", encoding="utf-8")

            handler = api.make_handler(":memory:", auth="test", static_dir=str(root))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2).read().decode()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertRegex(body, r'/app\.js\?v=\d+')
        self.assertRegex(body, r'/app\.css\?v=\d+')

    def test_dashboard_repeating_refreshes_use_shared_polling_hook(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")

        self.assertIn("function usePolling(", jsx)
        self.assertIn("clearInterval", jsx)
        self.assertGreaterEqual(jsx.count("usePolling("), 6)

    def test_dashboard_refresh_layer_owns_stream_and_transition_polling(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")
        dashboard = jsx.split("function Dashboard(", 1)[1].split("/* ----------------------------------------------------------------- root */", 1)[0]

        self.assertIn("function useDashboardRefresh(", jsx)
        self.assertIn("function useDashboardStream(", jsx)
        self.assertIn("function useManualScanProgress(", jsx)
        self.assertIn("function useObserverTransition(", jsx)
        self.assertIn("useDashboardRefresh()", dashboard)
        self.assertNotIn("new EventSource", dashboard)
        self.assertNotIn("setInterval", dashboard)
        self.assertNotIn("/api/scan-status", dashboard)

    def test_dashboard_build_bundles_source_modules(self):
        build = (ROOT / "web" / "build.sh").read_text(encoding="utf-8")
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")

        self.assertIn('from "./lib/format.js"', jsx)
        self.assertIn("--bundle", build)
        self.assertIn("--format=iife", build)


if __name__ == "__main__":
    unittest.main()
