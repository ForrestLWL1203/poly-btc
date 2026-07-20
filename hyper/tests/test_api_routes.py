import unittest
from unittest.mock import patch

from dashboard.api import routes as api_routes


class ApiRouteTests(unittest.TestCase):
    def test_get_routes_are_declared_in_tables(self):
        self.assertIn("/api/overview", api_routes.GET_ROUTES)
        self.assertIn("/api/equity", api_routes.GET_ROUTES)
        self.assertIn("/api/positions", api_routes.GET_ROUTES)
        self.assertIn("/api/wallets", api_routes.GET_ROUTES)
        self.assertIn("/api/discovery", api_routes.GET_ROUTES)
        self.assertIn("/api/scan-runs", api_routes.GET_ROUTES)
        self.assertIn("/api/params", api_routes.GET_ROUTES)
        self.assertIn("/api/scan-status", api_routes.GET_ROUTES)
        self.assertIn("/api/score-dist", api_routes.GET_ROUTES)
        self.assertIn("/api/risk-radar", api_routes.GET_ROUTES)
        self.assertIn("/api/risk-radar/intents", api_routes.GET_ROUTES)
        self.assertIn("/api/risk-radar/thresholds", api_routes.GET_ROUTES)
        self.assertIn("/api/connections", api_routes.GET_ROUTES)
        self.assertIn("/api/credential-wrap-key", api_routes.GET_ROUTES)
        self.assertNotIn("/api/shadow", api_routes.GET_ROUTES)

        prefixes = [prefix for prefix, _handler in api_routes.GET_PREFIX_ROUTES]
        self.assertIn("/api/positions/", prefixes)
        self.assertIn("/api/wallets/", prefixes)
        self.assertIn("/api/commands/", prefixes)

    def test_write_routes_are_declared_in_tables(self):
        self.assertIn("/api/auth/login", api_routes.POST_ROUTES)
        self.assertIn("/api/commands", api_routes.POST_ROUTES)
        post_prefixes = [prefix for prefix, _handler in api_routes.POST_PREFIX_ROUTES]
        patch_prefixes = [prefix for prefix, _handler in api_routes.PATCH_PREFIX_ROUTES]

        self.assertIn("/api/params/", post_prefixes)
        self.assertIn("/api/params/", patch_prefixes)

    def test_dispatch_get_calls_exact_route_with_query_params(self):
        with patch.object(api_routes, "ep_equity", return_value={"ok": "7d"}) as ep:
            handled, data = api_routes.dispatch_get(object(), "/api/equity", {"range": ["7d"]})

        self.assertTrue(handled)
        self.assertEqual(data, {"ok": "7d"})
        ep.assert_called_once()
        self.assertEqual(ep.call_args.args[1], "7d")

    def test_dispatch_get_params_can_include_score_distribution(self):
        db = object()
        with patch.object(api_routes, "ep_params", return_value={"ok": True}) as ep:
            handled, data = api_routes.dispatch_get(db, "/api/params", {"includeScoreDist": ["1"]})

        self.assertTrue(handled)
        self.assertEqual(data, {"ok": True})
        ep.assert_called_once_with(db, include_score_dist=True)

    def test_dispatch_get_calls_dynamic_detail_route(self):
        with patch.object(api_routes, "ep_position_detail", return_value={"id": 42}) as ep:
            handled, data = api_routes.dispatch_get(object(), "/api/positions/42", {})

        self.assertTrue(handled)
        self.assertEqual(data, {"id": 42})
        ep.assert_called_once()
        self.assertEqual(ep.call_args.args[1], 42)

    def test_dispatch_get_unknown_route_is_not_handled(self):
        handled, data = api_routes.dispatch_get(object(), "/api/nope", {})

        self.assertFalse(handled)
        self.assertIsNone(data)

    def test_dispatch_post_rejects_unauthorized_command_before_body_validation(self):
        handled, code, payload = api_routes.dispatch_post("db", object(), "/api/commands", {}, False)

        self.assertTrue(handled)
        self.assertEqual(code, 401)
        self.assertEqual(payload, {"error": "unauthorized"})

    def test_credential_command_rejects_plaintext_before_database_insert(self):
        body = {"type": "set_provider_credential", "payload": {"provider": "deepseek", "apiKey": "plaintext"}}
        handled, code, payload = api_routes.dispatch_post("db", object(), "/api/commands", body, True)
        self.assertTrue(handled)
        self.assertEqual(code, 422)
        self.assertEqual(payload["error"], "invalid_payload")

    def test_dispatch_patch_updates_params_payload(self):
        with patch.object(api_routes, "patch_params", return_value={"X": 1}) as patch_params:
            handled, code, payload = api_routes.dispatch_patch("db", "/api/params/follow", {"X": 1})

        self.assertTrue(handled)
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"updated": {"X": 1}})
        patch_params.assert_called_once_with("db", "follow", {"X": 1})

    def test_dispatch_patch_rejects_bad_category(self):
        handled, code, payload = api_routes.dispatch_patch("db", "/api/params/nope", {})

        self.assertTrue(handled)
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"error": "bad_category"})


if __name__ == "__main__":
    unittest.main()
