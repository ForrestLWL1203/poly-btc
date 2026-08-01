import sqlite3
import tempfile
import unittest
from pathlib import Path

from hyper import config, params, storage


class ScannerSettingsParamTests(unittest.TestCase):
    def test_product_defaults_use_cheap_recall_before_official_perp_return(self):
        self.assertEqual(config.HARVEST_WEEK_VLM_MIN, 250_000.0)
        self.assertEqual(config.HARVEST_MIN_ACCT, 20_000.0)
        self.assertFalse(hasattr(config, "HARVEST_WEEK_ROI_MIN"))
        self.assertFalse(hasattr(config, "HARVEST_MONTH_ROI_MIN"))
        self.assertFalse(hasattr(config, "HARVEST_ALL_ROI_MIN"))
        self.assertEqual((config.HARVEST_WEEK_PNL_MIN, config.HARVEST_MONTH_PNL_MIN,
                          config.HARVEST_ALL_PNL_MIN), (0.0, 0.0, 0.0))
        self.assertEqual(config.HARVEST_PERP_PNL_SHARE_MIN, 0.60)
        self.assertEqual(config.WALLET_MARGIN_CAP_PCT, 1.0)
        self.assertEqual(config.WALLET_CRYPTO_STABLE_SIDE_CAP_PCT, 1.0)
        self.assertEqual(config.WALLET_CRYPTO_MID_SIDE_CAP_PCT, 1.0)
        self.assertEqual(config.WALLET_CRYPTO_HIGH_SIDE_CAP_PCT, 1.0)
        self.assertEqual(config.WALLET_STOCK_SIDE_CAP_PCT, 1.0)
        self.assertEqual(config.WALLET_MAX_OPEN_POSITIONS, 15)
        self.assertEqual(config.WALLET_STOCK_SIDE_MAX_POSITIONS, 15)
        self.assertFalse(hasattr(config, "MAX_TOTAL_MARGIN_PCT"))
        self.assertFalse(hasattr(config, "STOCK_MAX_LEV"))
        self.assertFalse(hasattr(config, "PORTFOLIO_DRAWDOWN_STOP_ENABLE"))
        self.assertFalse(hasattr(config, "PORTFOLIO_DRAWDOWN_STOP_PCT"))
        self.assertFalse(hasattr(config, "HARVEST_WEEK_VLM_MAX"))
        self.assertFalse(hasattr(config, "HARVEST_PNL_VOL_MIN"))

        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)

            scanner = params.load_category(db, "scanner")
            follow = params.load_follow(db)
            self.assertEqual(scanner["HARVEST_WEEK_VLM_MIN"], 250_000.0)
            self.assertEqual(scanner["HARVEST_MIN_ACCT"], 20_000.0)
            self.assertNotIn("HARVEST_WEEK_ROI_MIN", scanner)
            self.assertNotIn("HARVEST_MONTH_ROI_MIN", scanner)
            self.assertNotIn("HARVEST_ALL_ROI_MIN", scanner)
            self.assertEqual(scanner["HARVEST_WEEK_PNL_MIN"], 0.0)
            self.assertEqual(scanner["HARVEST_MONTH_PNL_MIN"], 0.0)
            self.assertEqual(scanner["HARVEST_ALL_PNL_MIN"], 0.0)
            self.assertEqual(scanner["HARVEST_PERP_PNL_SHARE_MIN"], 0.60)
            self.assertEqual(scanner["inactive_days"], 3)
            self.assertNotIn("COPY_STOP_ENABLE", follow)
            self.assertNotIn("STOP_MARGIN_PCT", follow)
            self.assertEqual(follow["MARGIN_EQUITY_PCT"], 1.0)
            self.assertNotIn("WALLET_MARGIN_CAP_PCT", follow)
            self.assertNotIn("WALLET_SECTOR_SIDE_CAP_PCT", follow)
            self.assertNotIn("WALLET_CRYPTO_STABLE_SIDE_CAP_PCT", follow)
            self.assertNotIn("WALLET_CRYPTO_MID_SIDE_CAP_PCT", follow)
            self.assertNotIn("WALLET_CRYPTO_HIGH_SIDE_CAP_PCT", follow)
            self.assertNotIn("WALLET_STOCK_SIDE_CAP_PCT", follow)
            self.assertNotIn("WALLET_MAX_OPEN_POSITIONS", follow)
            self.assertNotIn("MAX_TOTAL_MARGIN_PCT", follow)
            self.assertNotIn("STOCK_MAX_LEV", follow)
            self.assertNotIn("PORTFOLIO_DRAWDOWN_STOP_ENABLE", follow)
            self.assertNotIn("PORTFOLIO_DRAWDOWN_STOP_PCT", follow)
            self.assertFalse(follow["SMART_TP_ENABLE"])
            self.assertEqual(follow["SMART_TP_GIVEBACK_1_PCT"], 0.20)
            self.assertEqual(follow["SMART_TP_CLOSE_3_PCT"], 0.25)
            self.assertEqual(follow["SMART_TP_TAIL_REMAIN_PCT"], 0.30)
            self.assertEqual(follow["SMART_TP_TARGET_REDUCE_EXIT_PCT"], 0.30)
            self.assertEqual(follow["STABLE_MARGIN_PCT"], 0.05)
            self.assertEqual(follow["MID_MARGIN_PCT"], 0.03)
            self.assertEqual(follow["HIGH_MARGIN_PCT"], 0.03)
            self.assertEqual(follow["STABLE_LEV_CAP"], 30.0)
            self.assertEqual(follow["MID_LEV_CAP"], 12.0)
            self.assertEqual(follow["HIGH_LEV_CAP"], 5.0)
            self.assertEqual(follow["STABLE_MIN_NOTIONAL"], 5000.0)
            self.assertEqual(follow["MID_MIN_NOTIONAL"], 1500.0)
            self.assertEqual(follow["HIGH_MIN_NOTIONAL"], 600.0)
            self.assertEqual(follow["MID_COIN_CAP_PCT"], 0.20)
            self.assertEqual(follow["ADD_GAP_K"], 0.05)
            self.assertEqual(follow["ADD_GAP_SHRINK_G"], 1.3)

            visible_follow = {p["key"]: p for p in params.get_all(db)["follow"]}
            self.assertEqual(visible_follow["MARGIN_EQUITY_PCT"]["value"], 100.0)
            self.assertEqual(visible_follow["MARGIN_EQUITY_PCT"]["level"], "yellow")
            self.assertNotIn("WALLET_MARGIN_CAP_PCT", visible_follow)
            self.assertNotIn("WALLET_SECTOR_SIDE_CAP_PCT", visible_follow)
            self.assertNotIn("WALLET_CRYPTO_STABLE_SIDE_CAP_PCT", visible_follow)
            self.assertNotIn("WALLET_STOCK_SIDE_CAP_PCT", visible_follow)
            self.assertNotIn("STOCK_MAX_LEV", visible_follow)
            self.assertNotIn("PORTFOLIO_DRAWDOWN_STOP_ENABLE", visible_follow)
            self.assertNotIn("PORTFOLIO_DRAWDOWN_STOP_PCT", visible_follow)
            self.assertNotIn("TAIL_CLOSE_ENABLE", visible_follow)
            self.assertNotIn("TAIL_CLOSE_HARD_REMAIN_PCT", visible_follow)
            self.assertNotIn("TAIL_CLOSE_RISK_REMAIN_PCT", visible_follow)
            self.assertNotIn("TAIL_CLOSE_PROFIT_GIVEBACK_PCT", visible_follow)
            self.assertFalse(visible_follow["SMART_TP_ENABLE"]["value"])
            self.assertEqual(visible_follow["SMART_TP_ENABLE"]["level"], "green")
            self.assertEqual(visible_follow["STABLE_MARGIN_PCT"]["value"], 5.0)
            self.assertEqual(visible_follow["HIGH_MARGIN_PCT"]["value"], 3.0)
            self.assertEqual(visible_follow["STABLE_LEV_CAP"]["value"], 30.0)
            self.assertEqual(visible_follow["STABLE_MIN_NOTIONAL"]["value"], 5000.0)
            self.assertNotIn("SMART_TP_GIVEBACK_1_PCT", visible_follow)

    def test_scanner_settings_expose_extreme_quality_contract(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            params.seed_params(db)

            scanner_params = params.get_all(db)["scanner"]
            scanner_keys = [p["key"] for p in scanner_params]
            levels = {p["key"]: p["level"] for p in scanner_params}

            self.assertEqual(scanner_keys[:4], [
                "HARVEST_WEEK_VLM_MIN",
                "EXCLUDE_HFT",
                "CORE_INITIAL_MAX_N",
                "PRE_STRICT_QUEUE_MAX_N",
            ])
            self.assertNotIn("HARVEST_WEEK_PNL_MIN", scanner_keys)
            self.assertNotIn("HARVEST_MONTH_PNL_MIN", scanner_keys)
            self.assertNotIn("HARVEST_ALL_PNL_MIN", scanner_keys)
            self.assertNotIn("HARVEST_PERP_PNL_SHARE_MIN", scanner_keys)
            self.assertNotIn("HARVEST_WEEK_ROI_MIN", scanner_keys)
            self.assertNotIn("HARVEST_MONTH_ROI_MIN", scanner_keys)
            self.assertNotIn("HARVEST_ALL_ROI_MIN", scanner_keys)
            self.assertNotIn("HARVEST_WEEK_VLM_MAX", scanner_keys)
            self.assertNotIn("HARVEST_PNL_VOL_MIN", scanner_keys)
            self.assertNotIn("PORTFOLIO_MAX_TURNOVER", scanner_keys)
            self.assertNotIn("PORTFOLIO_MIN_EDGE_BPS", scanner_keys)
            self.assertIn("MAX_CONCURRENT_POS", scanner_keys)
            self.assertNotIn("MIN_ACTIVE_SCORE", scanner_keys)
            self.assertNotIn("EVIDENCE_MIN_DAYS", scanner_keys)
            self.assertNotIn("EVIDENCE_MIN_TRADES", scanner_keys)
            self.assertIn("SOURCE_MIN_EPISODES_30D", scanner_keys)
            self.assertNotIn("SOURCE_MIN_EPISODE_WIN_RATE", scanner_keys)
            self.assertNotIn("SOURCE_LOW_FREQ_MIN_EPISODES_30D", scanner_keys)
            self.assertNotIn("SOURCE_LOW_FREQ_MAX_EPISODES_30D", scanner_keys)
            self.assertNotIn("SOURCE_LOW_FREQ_MIN_EPISODE_WIN_RATE", scanner_keys)
            self.assertNotIn("SOURCE_LOW_FREQ_MIN_OFFICIAL_RETURN", scanner_keys)
            self.assertIn("ROUGH_COPY_MIN_CLOSED_30D", scanner_keys)
            self.assertNotIn("ROUGH_COPY_MIN_WIN_RATE", scanner_keys)
            self.assertNotIn("ROUGH_COPY_MIN_RETURN_30D", scanner_keys)
            self.assertNotIn("ROUGH_COPY_MIN_RETURN_7D", scanner_keys)
            self.assertNotIn("COPY_BT_MIN_NET_PNL", scanner_keys)
            self.assertNotIn("CORE_COPY_CAMPAIGN_FLOOR", scanner_keys)
            self.assertIn("CORE_PROFITABILITY_CONTRACT", scanner_keys)
            self.assertNotIn("CORE_COPY_WIN_RATE_FLOORS", scanner_keys)
            self.assertNotIn("CORE_COPY_WIN_RATE_LCB", scanner_keys)
            self.assertIn("CORE_COPY_MAX_LIQUIDATIONS_30D", scanner_keys)
            self.assertNotIn(
                "COPY_CATASTROPHIC_LIQUIDATION_LOSS_PCT", scanner_keys,
            )
            self.assertEqual(levels["CORE_PROFITABILITY_CONTRACT"], "black")
            self.assertIn("CORE_INITIAL_MAX_N", scanner_keys)
            self.assertEqual(levels["CORE_INITIAL_MAX_N"], "green")
            initial_limit = next(p for p in scanner_params if p["key"] == "CORE_INITIAL_MAX_N")
            self.assertEqual(initial_limit["value"], 16)
            self.assertNotIn("AUTO_TUNE_RISK_PROFILE", scanner_keys)
            self.assertEqual(levels["SOURCE_MIN_EPISODES_30D"], "black")
            self.assertFalse(any(k.startswith("SCORE_") for k in scanner_keys))

    def test_seed_params_refreshes_metadata_without_overwriting_operator_value(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.row_factory = sqlite3.Row
            db.execute(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("MAX_CONCURRENT_POS", "42", "scanner", "hidden", "int", "rescan", "15", "old"),
            )
            db.commit()

            params.seed_params(db)

            row = db.execute(
                "SELECT value,category,level,type,effect FROM params WHERE key='MAX_CONCURRENT_POS'"
            ).fetchone()
            self.assertEqual(row["value"], "42")
            self.assertEqual(row["category"], "scanner")
            self.assertEqual(row["level"], "blue")
            self.assertEqual(row["type"], "int")
            self.assertEqual(row["effect"], "rescan")

    def test_seed_params_migrates_previous_approved_harvest_defaults_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            old = {
                "HARVEST_MIN_ACCT": "30000",
                "HARVEST_WEEK_VLM_MIN": "150000",
                "HARVEST_WEEK_PNL_MIN": "5000",
                "HARVEST_MONTH_PNL_MIN": "15000",
                "HARVEST_ALL_PNL_MIN": "20000",
            }
            for key, value in old.items():
                db.execute("UPDATE params SET value=?,default_value=? WHERE key=?", (value, value, key))
            db.execute("UPDATE params SET value='12' WHERE key='HARVEST_PERP_PNL_SHARE_MIN'")
            db.commit()

            params.seed_params(db)

            values = dict(db.execute(
                "SELECT key,value FROM params WHERE key LIKE 'HARVEST_%'"
            ).fetchall())
            self.assertEqual(float(values["HARVEST_MIN_ACCT"]), 20_000.0)
            self.assertEqual(float(values["HARVEST_WEEK_VLM_MIN"]), 250_000.0)
            self.assertEqual(float(values["HARVEST_WEEK_PNL_MIN"]), 0.0)
            self.assertEqual(float(values["HARVEST_MONTH_PNL_MIN"]), 0.0)
            self.assertEqual(float(values["HARVEST_ALL_PNL_MIN"]), 0.0)
            self.assertEqual(float(values["HARVEST_PERP_PNL_SHARE_MIN"]), 12.0)

            # After this migration has installed the new default metadata, an operator may still
            # intentionally choose a former value without it being rewritten on every restart.
            db.execute("UPDATE params SET value='8000' WHERE key='HARVEST_MONTH_PNL_MIN'")
            db.commit()
            params.seed_params(db)
            self.assertEqual(float(db.execute(
                "SELECT value FROM params WHERE key='HARVEST_MONTH_PNL_MIN'"
            ).fetchone()[0]), 8_000.0)

            # A custom value can legitimately equal some *other* historical default. Migration is allowed
            # only when value still equals that row's own default, not merely when both appear in the known
            # predecessor set.
            db.execute(
                "UPDATE params SET value='8000',default_value='5000' WHERE key='HARVEST_MONTH_PNL_MIN'"
            )
            db.commit()
            params.seed_params(db)
            self.assertEqual(float(db.execute(
                "SELECT value FROM params WHERE key='HARVEST_MONTH_PNL_MIN'"
            ).fetchone()[0]), 8_000.0)

    def test_seed_params_migrates_previous_follow_sizing_defaults_only(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            previous = {
                "STABLE_MARGIN_PCT": "3.5000000000000004",
                "HIGH_MARGIN_PCT": "2.0",
                "STABLE_LEV_CAP": "25.0",
                "MID_LEV_CAP": "10.0",
                "HIGH_LEV_CAP": "4.0",
                "STABLE_MIN_NOTIONAL": "2500.0",
                "HIGH_MIN_NOTIONAL": "250.0",
                "MID_COIN_CAP_PCT": "22.0",
                "ADD_GAP_K": "0.12",
                "ADD_GAP_SHRINK_G": "1.2",
            }
            for key, value in previous.items():
                db.execute("UPDATE params SET value=?,default_value=? WHERE key=?", (value, value, key))
            db.execute("UPDATE params SET value='777',default_value='1000.0' WHERE key='MID_MIN_NOTIONAL'")
            db.commit()

            params.seed_params(db)

            follow = params.load_follow(db)
            self.assertEqual(follow["STABLE_MARGIN_PCT"], 0.05)
            self.assertEqual(follow["HIGH_MARGIN_PCT"], 0.03)
            self.assertEqual(follow["STABLE_LEV_CAP"], 30.0)
            self.assertEqual(follow["MID_LEV_CAP"], 12.0)
            self.assertEqual(follow["HIGH_LEV_CAP"], 5.0)
            self.assertEqual(follow["STABLE_MIN_NOTIONAL"], 5000.0)
            self.assertEqual(follow["HIGH_MIN_NOTIONAL"], 600.0)
            self.assertEqual(follow["MID_COIN_CAP_PCT"], 0.20)
            self.assertEqual(follow["ADD_GAP_K"], 0.05)
            self.assertEqual(follow["ADD_GAP_SHRINK_G"], 1.3)
            self.assertEqual(follow["MID_MIN_NOTIONAL"], 777.0)

    def test_seed_params_migrates_immediately_previous_harvest_surface(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            previous = {
                "HARVEST_MONTH_PNL_MIN": "8000",
                "HARVEST_PERP_PNL_SHARE_MIN": "60",
            }
            for key, value in previous.items():
                db.execute("UPDATE params SET value=?,default_value=? WHERE key=?", (value, value, key))
            db.commit()

            params.seed_params(db)

            values = dict(db.execute(
                "SELECT key,value FROM params WHERE key IN "
                "('HARVEST_MONTH_PNL_MIN','HARVEST_PERP_PNL_SHARE_MIN')"
            ).fetchall())
            self.assertEqual(float(values["HARVEST_MONTH_PNL_MIN"]), 0.0)
            self.assertEqual(float(values["HARVEST_PERP_PNL_SHARE_MIN"]), 60.0)

    def test_seed_params_purges_retired_portfolio_controls(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            db.execute(
                "UPDATE params SET value='15',default_value='15' WHERE key='MAX_CONCURRENT_POS'"
            )
            for key, value in (
                ("WALLET_SECTOR_SIDE_CAP_PCT", "60"),
                ("WALLET_MARGIN_CAP_PCT", "25"),
                ("WALLET_MAX_OPEN_POSITIONS", "3"),
                ("MAX_TOTAL_MARGIN_PCT", "85"),
                ("STOCK_MAX_LEV", "10"),
                ("PORTFOLIO_DRAWDOWN_STOP_ENABLE", "true"),
                ("PORTFOLIO_DRAWDOWN_STOP_PCT", "15"),
            ):
                db.execute(
                    "INSERT INTO params "
                    "(key,value,category,level,type,effect,default_value,updated_at) "
                    "VALUES (?,?, 'follow','hidden','pct','immediate',?,'2026-01-01')",
                    (key, value, value),
                )
            db.commit()

            params.seed_params(db)

            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM params WHERE key IN "
                "('WALLET_SECTOR_SIDE_CAP_PCT','WALLET_MARGIN_CAP_PCT',"
                "'WALLET_MAX_OPEN_POSITIONS','MAX_TOTAL_MARGIN_PCT','STOCK_MAX_LEV',"
                "'PORTFOLIO_DRAWDOWN_STOP_ENABLE','PORTFOLIO_DRAWDOWN_STOP_PCT')"
            ).fetchone()[0], 0)
            self.assertEqual(float(db.execute(
                "SELECT value FROM params WHERE key='MAX_CONCURRENT_POS'"
            ).fetchone()[0]), 15.0)

    def test_seed_params_deletes_retired_copy_7d_floor(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            params.seed_params(db)
            db.execute(
                "INSERT INTO params "
                "(key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES ('CORE_MIN_COPY_RETURN_7D','5','scanner','blue','pct','rescan','5','old')"
            )
            db.commit()

            params.seed_params(db)

            self.assertIsNone(db.execute(
                "SELECT value FROM params WHERE key='CORE_MIN_COPY_RETURN_7D'"
            ).fetchone())
            self.assertEqual(float(db.execute(
                "SELECT value FROM params WHERE key='CORE_MIN_DYNAMIC_COPY_RETURN_7D'"
            ).fetchone()[0]), 3.0)

    def test_seed_params_removes_obsolete_rows_and_seeds_dynamic_return_gate(self):
        with tempfile.TemporaryDirectory() as td:
            db = storage.connect(str(Path(td) / "hl.db"), storage.DISCOVERY_SCHEMA, storage.OBSERVE_SCHEMA)
            db.executemany(
                "INSERT INTO params (key,value,category,level,type,effect,default_value,updated_at) "
                "VALUES (?,?,'scanner','blue','float','rescan',?,'old')",
                [
                    ("MIN_ACTIVE_SCORE", "0.6", "0.6"),
                    ("CORE_COPY_WIN_RATE_30D_MIN", "60", "60"),
                    ("COPY_MIN_TAIL_RETURN_30D", "3", "3"),
                    ("CORE_MIN_COPY_RETURN_30D", "10", "10"),
                    ("CORE_RETENTION_MIN_COPY_RETURN_30D", "8", "8"),
                    ("HARVEST_WEEK_ROI_MIN", "10", "10"),
                    ("HARVEST_MONTH_ROI_MIN", "20", "20"),
                    ("HARVEST_ALL_ROI_MIN", "10", "10"),
                    ("HARVEST_ROI_WINDOWS_MIN_PASS", "2", "2"),
                ],
            )
            db.commit()

            params.seed_params(db)

            for key in (
                "MIN_ACTIVE_SCORE",
                "CORE_COPY_WIN_RATE_30D_MIN",
                "COPY_MIN_TAIL_RETURN_30D",
                "CORE_RETENTION_MIN_COPY_RETURN_30D",
                "CORE_MIN_COPY_RETURN_30D",
                "HARVEST_WEEK_ROI_MIN",
                "HARVEST_MONTH_ROI_MIN",
                "HARVEST_ALL_ROI_MIN",
                "HARVEST_ROI_WINDOWS_MIN_PASS",
            ):
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM params WHERE key=?", (key,)
                ).fetchone())
            self.assertEqual(float(db.execute(
                "SELECT value FROM params WHERE key='CORE_MIN_DYNAMIC_COPY_RETURN_30D'"
            ).fetchone()[0]), 10.0)

if __name__ == "__main__":
    unittest.main()
