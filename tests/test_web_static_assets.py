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

    def test_dashboard_repeating_refreshes_use_shared_resource_hook(self):
        jsx = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (ROOT / "web").glob("**/*.jsx")
        )
        refresh = (ROOT / "web" / "lib" / "refresh.js").read_text(encoding="utf-8")

        self.assertIn("function usePolling(", refresh)
        self.assertIn("function useApiResource(", refresh)
        self.assertIn("requestId", refresh)
        self.assertIn("clearInterval", refresh)
        self.assertGreaterEqual((jsx + refresh).count("useApiResource("), 7)
        self.assertNotIn('import { usePolling }', jsx)

    def test_dashboard_refresh_layer_owns_stream_and_transition_polling(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")
        refresh = (ROOT / "web" / "lib" / "refresh.js").read_text(encoding="utf-8")
        dashboard = jsx.split("function Dashboard(", 1)[1].split("/* ----------------------------------------------------------------- root */", 1)[0]

        self.assertIn("function useDashboardRefresh(", refresh)
        self.assertIn("function useDashboardStream(", refresh)
        self.assertIn("function useManualScanProgress(", refresh)
        self.assertIn("function useObserverTransition(", refresh)
        self.assertIn("useApiResource(loadOverview", refresh)
        self.assertIn("useDashboardRefresh(api)", dashboard)
        self.assertNotIn("new EventSource", dashboard)
        self.assertNotIn("setInterval", dashboard)
        self.assertNotIn("/api/scan-status", dashboard)

    def test_dashboard_build_bundles_source_modules(self):
        build = (ROOT / "web" / "build.sh").read_text(encoding="utf-8")
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")

        self.assertIn('from "./lib/api.js"', jsx)
        self.assertIn('from "./lib/format.js"', jsx)
        self.assertIn('from "./lib/refresh.js"', jsx)
        self.assertIn("--bundle", build)
        self.assertIn("--format=iife", build)

    def test_positions_page_is_split_from_dashboard_shell(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")
        positions = ROOT / "web" / "components" / "Positions.jsx"

        self.assertTrue(positions.exists(), "Positions page should live in web/components/Positions.jsx")
        body = positions.read_text(encoding="utf-8") if positions.exists() else ""
        self.assertIn('from "../lib/api.js"', body)
        self.assertIn("export function Positions(", body)
        self.assertNotIn("function Positions(", jsx)
        self.assertIn('from "./components/Positions.jsx"', jsx)

    def test_dashboard_shell_imports_observer_mask_component(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")
        obs_mask = ROOT / "web" / "components" / "ObsMask.jsx"

        self.assertTrue(obs_mask.exists(), "Observer transition mask should be an explicit component")
        self.assertIn("export function ObsMask(", obs_mask.read_text(encoding="utf-8"))
        self.assertIn('from "./components/ObsMask.jsx"', jsx)
        self.assertIn("<ObsMask", jsx)

    def test_settings_page_is_split_from_dashboard_shell(self):
        jsx = (ROOT / "web" / "app.jsx").read_text(encoding="utf-8")
        settings = ROOT / "web" / "components" / "Settings.jsx"

        self.assertTrue(settings.exists(), "Settings page should live in web/components/Settings.jsx")
        body = settings.read_text(encoding="utf-8") if settings.exists() else ""
        self.assertIn('from "../lib/api.js"', body)
        self.assertIn("export function Settings(", body)
        self.assertNotIn("function Settings(", jsx)
        self.assertIn('from "./components/Settings.jsx"', jsx)

    def test_settings_internals_are_split(self):
        settings = (ROOT / "web" / "components" / "Settings.jsx").read_text(encoding="utf-8")
        top_level_parts = {
            "settings/useSettingsParams.js": "export function useSettingsParams(",
            "settings/validation.js": "export function validateFollowParams(",
            "settings/AddSettingsPanel.jsx": "export function AddSettingsPanel(",
            "settings/FollowSettingsPanel.jsx": "export function FollowSettingsPanel(",
            "settings/ScannerSettingsPanel.jsx": "export function ScannerSettingsPanel(",
            "settings/SizingPreview.jsx": "export function SizingPreview(",
        }
        internal_parts = {
            "settings/paramMeta.js": "export const PARAM_META",
            "settings/ParamRow.jsx": "export function ParamRow(",
            "settings/EditableValue.jsx": "export function EditableValue(",
            "settings/CoinBlacklistEditor.jsx": "export function CoinBlacklistEditor(",
        }

        for rel, marker in {**top_level_parts, **internal_parts}.items():
            body = (ROOT / "web" / "components" / rel).read_text(encoding="utf-8")
            self.assertIn(marker, body)

        for rel in top_level_parts:
            self.assertIn(f'from "./{rel}"', settings)

        self.assertIn('from "./EditableValue.jsx"', (ROOT / "web" / "components" / "settings/ParamRow.jsx").read_text(encoding="utf-8"))
        self.assertIn('from "./CoinBlacklistEditor.jsx"', (ROOT / "web" / "components" / "settings/FollowSettingsPanel.jsx").read_text(encoding="utf-8"))

        self.assertNotIn("const PARAM_META = {", settings)

    def test_wallets_internals_are_split(self):
        wallets = (ROOT / "web" / "components" / "Wallets.jsx").read_text(encoding="utf-8")
        drawer = ROOT / "web" / "components" / "wallets" / "WalletDrawer.jsx"
        audit = ROOT / "web" / "components" / "wallets" / "WalletAudit.jsx"

        self.assertTrue(drawer.exists(), "Wallet drawer should live in web/components/wallets/WalletDrawer.jsx")
        self.assertTrue(audit.exists(), "Wallet audit UI should live in web/components/wallets/WalletAudit.jsx")
        drawer_body = drawer.read_text(encoding="utf-8")
        audit_body = audit.read_text(encoding="utf-8")
        self.assertIn("export function WalletDrawer(", drawer_body)
        self.assertIn("export function useWalletAudit(", audit_body)
        self.assertIn("/api/pipeline-audit", audit_body)
        self.assertIn('from "./wallets/WalletDrawer.jsx"', wallets)
        self.assertIn('from "./wallets/WalletAudit.jsx"', wallets)
        self.assertNotIn("function WalletDrawer(", wallets)
        self.assertNotIn("/api/pipeline-audit", wallets)


if __name__ == "__main__":
    unittest.main()
