import base64
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dashboard.api.collection_source import (
    ep_collection_source,
    set_collection_source,
)
from hyper.market.collection_control import (
    CollectionSourceBusy,
    CollectionSourceUnavailable,
)
from dashboard.api.discovery import ep_scan_runs, ep_scan_status
from hyper import config, params, storage
from hyper.execution.credentials import (
    ENVELOPE_ALGORITHM,
    generate_wrap_keypair,
    public_wrap_key_payload,
)
from hyper.market import collection_control, collection_runtime, rest


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _endpoint_envelope(public_path, endpoint):
    public = serialization.load_pem_public_key(Path(public_path).read_bytes())
    aes = os.urandom(32)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes).encrypt(
        nonce, endpoint.encode(), collection_control.COLLECTION_ENDPOINT_AAD,
    )
    wrapped = public.encrypt(
        aes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    key = public_wrap_key_payload(public_path)
    return {
        "version": 1,
        "algorithm": ENVELOPE_ALGORITHM,
        "wrapKeyId": key["wrapKeyId"],
        "wrappedKey": base64.b64encode(wrapped).decode(),
        "iv": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }


class CollectionRestProviderTests(unittest.TestCase):
    def setUp(self):
        rest.configure_collection_source(selected="official")
        rest.configure_post_budget(weight_per_min=None)
        rest.reset_request_stats()

    def tearDown(self):
        rest.configure_collection_source(selected="official")
        rest.configure_post_budget(weight_per_min=None)

    def test_compatible_info_uses_quicknode_but_l2_stays_official(self):
        calls = []

        def urlopen(request, timeout=None):
            calls.append((request.full_url, timeout))
            return _Response({"ok": True})

        rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
            quicknode_rps=1000,
        )
        with patch.object(rest.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(rest.post({"type": "meta"}), {"ok": True})
            self.assertEqual(rest.post({"type": "l2Book", "coin": "BTC"}), {"ok": True})

        self.assertIn("quiknode.pro", calls[0][0])
        self.assertEqual(calls[1][0], config.INFO_URL)

    def test_400_disables_only_one_method_without_tripping_provider(self):
        calls = []

        def urlopen(request, timeout=None):
            calls.append(request.full_url)
            if "quiknode.pro" in request.full_url and len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
            return _Response({"ok": True})

        rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
            quicknode_rps=1000,
        )
        with patch.object(rest.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(rest.post({"type": "futureMethod"}), {"ok": True})
            self.assertEqual(rest.post({"type": "meta"}), {"ok": True})

        self.assertEqual(calls[1], config.INFO_URL)
        self.assertIn("quiknode.pro", calls[2])
        self.assertEqual(rest.collection_source_state()["effectiveSource"], "quicknode")

    def test_transient_errors_retry_then_lock_current_run_to_official(self):
        for failure in ("429", "timeout", "500"):
            with self.subTest(failure=failure):
                calls = []

                def urlopen(request, timeout=None):
                    calls.append(request.full_url)
                    if "quiknode.pro" in request.full_url:
                        if failure == "timeout":
                            raise TimeoutError("redacted")
                        code = int(failure)
                        raise urllib.error.HTTPError(request.full_url, code, "bad", {}, None)
                    return _Response({"official": True})

                rest.configure_collection_source(
                    selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
                    quicknode_rps=1000,
                )
                with patch.object(rest.urllib.request, "urlopen", side_effect=urlopen), \
                        patch.object(rest.time, "sleep", return_value=None):
                    self.assertEqual(rest.post({"type": "meta"}), {"official": True})
                    self.assertEqual(rest.post({"type": "allMids"}), {"official": True})

                self.assertEqual(sum("quiknode.pro" in value for value in calls), 3)
                self.assertEqual(calls[-1], config.INFO_URL)
                state = rest.collection_source_state()
                self.assertEqual(state["selectedSource"], "quicknode")
                self.assertEqual(state["effectiveSource"], "official")

    def test_new_job_retries_after_previous_job_tripped(self):
        inherited = {
            "selectedSource": "quicknode", "effectiveSource": "official",
            "fallbackReason": "quicknode_http_429", "fallbackAt": "2026-01-01T00:00:00Z",
        }
        state = rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
            inherited_state=inherited,
        )
        self.assertEqual(state["effectiveSource"], "official")
        state = rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
        )
        self.assertEqual(state["effectiveSource"], "quicknode")
        self.assertIsNone(state["fallbackReason"])

    def test_ten_rps_pacer_is_shared_across_threads(self):
        starts = []
        starts_lock = threading.Lock()

        def urlopen(_request, timeout=None):
            with starts_lock:
                starts.append(time.monotonic())
            return _Response({"ok": True})

        rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
            quicknode_rps=10,
        )
        with patch.object(rest.urllib.request, "urlopen", side_effect=urlopen):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: rest.post({"type": "meta"}), range(4)))

        gaps = [right - left for left, right in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 4)
        self.assertTrue(all(gap >= 0.085 for gap in gaps), gaps)

    def test_unconfigured_observer_style_realtime_call_stays_official(self):
        calls = []
        with patch.object(
            rest.urllib.request, "urlopen",
            side_effect=lambda request, timeout=None: calls.append(request.full_url) or _Response({}),
        ):
            rest.configure_collection_source(selected="official")
            self.assertEqual(rest.realtime_post_soft({"type": "allMids"}), {})
        self.assertEqual(calls, [config.INFO_URL])


class CollectionCredentialAndApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = str(root / "hl.db")
        self.endpoint_path = root / "quicknode"
        self.private = root / "private.pem"
        self.public = root / "public.pem"
        generate_wrap_keypair(self.private, self.public)
        self.db = storage.connect(
            self.db_path, storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA,
        )
        self.db.row_factory = sqlite3.Row
        params.seed_params(self.db)

    def tearDown(self):
        rest.configure_collection_source(selected="official")
        self.db.close()
        self.temp.cleanup()

    def test_endpoint_normalization_and_domain_restrictions(self):
        self.assertEqual(
            collection_control.normalize_quicknode_endpoint(
                "https://token.quiknode.pro/key/evm"
            ),
            "https://token.quiknode.pro/key/info",
        )
        self.assertEqual(
            collection_control.normalize_quicknode_endpoint(
                "https://token.quiknode.pro/key"
            ),
            "https://token.quiknode.pro/key/info",
        )
        for invalid in (
            "http://token.quiknode.pro/key", "https://quiknode.pro/key",
            "https://token.quiknode.pro.evil.test/key", "https://token.quiknode.pro/key?q=x",
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "quicknode_endpoint_invalid"
            ):
                collection_control.normalize_quicknode_endpoint(invalid)

    def test_encrypted_save_validates_then_atomically_writes_mode_0600(self):
        copied = "https://token.quiknode.pro/key/evm"
        envelope = _endpoint_envelope(self.public, copied)
        with patch.object(
            collection_control, "verify_quicknode_endpoint",
            side_effect=lambda value: {
                "endpoint": collection_control.normalize_quicknode_endpoint(value), "markets": 1,
            },
        ):
            result = collection_control.store_encrypted_quicknode_endpoint(
                self.db, envelope, private_key_path_value=str(self.private),
                endpoint_path_value=str(self.endpoint_path),
            )
        self.db.commit()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(stat.S_IMODE(self.endpoint_path.stat().st_mode), 0o600)
        self.assertTrue(self.endpoint_path.read_text().strip().endswith("/info"))

    def test_failed_validation_preserves_previous_endpoint(self):
        old = "https://old.quiknode.pro/key/info\n"
        self.endpoint_path.write_text(old)
        os.chmod(self.endpoint_path, 0o600)
        envelope = _endpoint_envelope(self.public, "https://new.quiknode.pro/key/evm")
        with patch.object(
            collection_control, "verify_quicknode_endpoint",
            side_effect=ValueError("quicknode_http_403"),
        ):
            with self.assertRaisesRegex(ValueError, "quicknode_http_403"):
                collection_control.store_encrypted_quicknode_endpoint(
                    self.db, envelope, private_key_path_value=str(self.private),
                    endpoint_path_value=str(self.endpoint_path),
                )
        self.assertEqual(self.endpoint_path.read_text(), old)

    def test_scanner_reads_only_a_private_regular_endpoint_file(self):
        self.endpoint_path.write_text("https://token.quiknode.pro/key/info\n")
        with patch.dict(
            os.environ,
            {collection_control.QUICKNODE_ENDPOINT_ENV: str(self.endpoint_path)},
        ):
            os.chmod(self.endpoint_path, 0o644)
            self.assertIsNone(collection_runtime.read_quicknode_endpoint())
            os.chmod(self.endpoint_path, 0o600)
            self.assertEqual(
                collection_runtime.read_quicknode_endpoint(),
                "https://token.quiknode.pro/key/info",
            )

    def test_source_switch_requires_verified_endpoint_and_locks_while_scanning(self):
        with self.assertRaises(CollectionSourceUnavailable):
            set_collection_source(self.db_path, "quicknode")
        self.db.execute(
            "INSERT INTO collection_source_control "
            "(id,quicknode_configured,quicknode_status,quicknode_verified_at,updated_at) "
            "VALUES (1,1,'verified','now','now')"
        )
        self.db.commit()
        self.assertEqual(
            set_collection_source(self.db_path, "quicknode")["selectedSource"], "quicknode",
        )
        self.db.execute(
            "INSERT OR REPLACE INTO scan_progress(id,state,updated_at) VALUES (1,'scanning','now')"
        )
        self.db.commit()
        with self.assertRaises(CollectionSourceBusy):
            set_collection_source(self.db_path, "official")

    def test_manual_new_run_can_reselect_quicknode_after_prior_fallback(self):
        self.db.execute(
            "INSERT INTO collection_source_control "
            "(id,quicknode_configured,quicknode_status,quicknode_verified_at,"
            "quicknode_error_code,quicknode_error_at,updated_at) "
            "VALUES (1,1,'fallback','2026-08-06T00:00:00Z','quicknode_http_429',"
            "'2026-08-06T01:00:00Z','2026-08-06T01:00:00Z')"
        )
        self.db.commit()
        set_collection_source(self.db_path, "quicknode")
        state = rest.configure_collection_source(
            selected="quicknode", quicknode_endpoint="https://token.quiknode.pro/key/info",
            inherited_state=None,
        )
        self.assertEqual(state["selectedSource"], "quicknode")
        self.assertEqual(state["effectiveSource"], "quicknode")
        self.assertIsNone(state["fallbackReason"])

    def test_status_api_exposes_source_and_never_endpoint(self):
        self.db.execute(
            "UPDATE params SET value='quicknode' WHERE key='COLLECTION_SOURCE'"
        )
        self.db.execute(
            "INSERT INTO collection_source_control "
            "(id,quicknode_configured,quicknode_status,quicknode_verified_at,updated_at) "
            "VALUES (1,1,'verified','now','now')"
        )
        self.db.execute(
            "INSERT OR REPLACE INTO scan_progress "
            "(id,state,selected_source,effective_source,source_fallback_reason,updated_at) "
            "VALUES (1,'scanning','quicknode','official','quicknode_http_429','now')"
        )
        self.db.commit()
        payload = ep_collection_source(self.db)
        encoded = json.dumps(payload, sort_keys=True)
        self.assertEqual(payload["selectedSource"], "quicknode")
        self.assertEqual(payload["effectiveSource"], "official")
        self.assertTrue(payload["fallback"])
        self.assertNotIn("quiknode.pro", encoded)
        self.assertNotIn("endpoint", encoded.lower())

    def test_scan_status_and_history_expose_source_audit(self):
        self.db.execute(
            "INSERT OR REPLACE INTO scan_progress "
            "(id,state,started_at,stage,candidates_scanned,candidates_total,eta_sec,manual,"
            "selected_source,effective_source,source_fallback_reason,source_fallback_at,updated_at) "
            "VALUES (1,'scanning','2026-08-07T00:00:00Z','fetch_history',2,10,1200,0,"
            "'quicknode','official','quicknode_http_429','2026-08-07T00:01:00Z','now')"
        )
        self.db.execute(
            "INSERT INTO scan_runs (started_at,finished_at,selected_source,effective_source,"
            "source_fallback_reason,source_fallback_at) VALUES (?,?,?,?,?,?)",
            (
                "2026-08-07T00:00:00Z", "2026-08-07T01:00:00Z", "quicknode", "official",
                "quicknode_http_429", "2026-08-07T00:01:00Z",
            ),
        )
        self.db.commit()
        live = ep_scan_status(self.db)
        history = ep_scan_runs(self.db, 1)["runs"][0]
        self.assertEqual(live["selectedSource"], "quicknode")
        self.assertEqual(live["effectiveSource"], "official")
        self.assertEqual(history["selectedSource"], "quicknode")
        self.assertEqual(history["sourceFallbackReason"], "quicknode_http_429")


if __name__ == "__main__":
    unittest.main()
