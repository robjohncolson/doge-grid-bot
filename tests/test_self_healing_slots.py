import unittest
from unittest import mock

import bot
import config
import state_machine as sm


class SelfHealingSlotsTests(unittest.TestCase):
    def _build_open_position_runtime(
        self,
        *,
        now_ts: float,
        entry_time: float,
        entry_price: float = 0.1000,
        exit_price: float = 0.1020,
        volume: float = 20.0,
        trade_id: str = "B",
        side: str = "sell",
        txid: str = "TX-EXIT-1",
        target_profit_pct: float = 2.0,
    ) -> tuple[bot.BotRuntime, int, sm.OrderState]:
        rt = bot.BotRuntime()
        exit_order = sm.OrderState(
            local_id=1,
            side=side,
            role="exit",
            price=float(exit_price),
            volume=float(volume),
            trade_id=str(trade_id),
            cycle=1,
            txid=str(txid),
            placed_at=float(entry_time + 5.0),
            entry_price=float(entry_price),
            entry_fee=0.0100,
            entry_filled_at=float(entry_time),
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=float(entry_price),
                    now=float(now_ts),
                    orders=(exit_order,),
                ),
            ),
        }
        position_id = rt._position_ledger.open_position(
            slot_id=0,
            trade_id=str(trade_id),
            slot_mode="sticky",
            cycle=1,
            entry_data={
                "entry_price": float(entry_price),
                "entry_cost": float(entry_price * volume),
                "entry_fee": 0.0100,
                "entry_volume": float(volume),
                "entry_time": float(entry_time),
                "entry_regime": "ranging",
                "entry_volatility": 0.0,
            },
            exit_data={
                "current_exit_price": float(exit_price),
                "original_exit_price": float(exit_price),
                "target_profit_pct": float(target_profit_pct),
                "exit_txid": str(txid),
            },
        )
        rt._position_ledger.journal_event(
            position_id,
            "created",
            {
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "regime": "ranging",
                "slot_mode": "sticky",
            },
            timestamp=float(entry_time),
        )
        rt._bind_position_for_exit(0, exit_order, position_id)
        return rt, position_id, exit_order

    def test_over_performance_journaled_on_better_exit_fill(self):
        rt = bot.BotRuntime()
        exit_order = sm.OrderState(
            local_id=1,
            side="sell",
            role="exit",
            price=0.1010,
            volume=20.0,
            trade_id="B",
            cycle=1,
            txid="TX-EXIT-1",
            placed_at=1005.0,
            entry_price=0.1000,
            entry_fee=0.0100,
            entry_filled_at=1000.0,
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1010,
                    now=1100.0,
                    orders=(exit_order,),
                ),
            ),
        }

        position_id = rt._position_ledger.open_position(
            slot_id=0,
            trade_id="B",
            slot_mode="sticky",
            cycle=1,
            entry_data={
                "entry_price": 0.1000,
                "entry_cost": 2.0,
                "entry_fee": 0.0100,
                "entry_volume": 20.0,
                "entry_time": 1000.0,
                "entry_regime": "ranging",
                "entry_volatility": 0.0,
            },
            exit_data={
                "current_exit_price": 0.1010,
                "original_exit_price": 0.1010,
                "target_profit_pct": 1.0,
                "exit_txid": "TX-EXIT-1",
            },
        )
        rt._position_ledger.journal_event(
            position_id,
            "created",
            {"entry_price": 0.1000, "exit_price": 0.1010, "regime": "ranging", "slot_mode": "sticky"},
            timestamp=1000.0,
        )
        rt._bind_position_for_exit(0, exit_order, position_id)

        rt._record_position_close_for_exit_fill(
            slot_id=0,
            exit_order=exit_order,
            fill_price=0.1020,  # Better than target for sell exit.
            fill_fee=0.0100,
            fill_cost=2.04,
            fill_timestamp=1100.0,
            txid="TX-EXIT-1",
        )

        pos = rt._position_ledger.get_position(position_id)
        self.assertEqual(pos["status"], "closed")

        journal = rt._position_ledger.get_journal(position_id)
        event_types = [row["event_type"] for row in journal]
        self.assertIn("filled", event_types)
        self.assertIn("over_performance", event_types)

        over = next(row for row in journal if row["event_type"] == "over_performance")
        # Excess = (0.1020 - 0.1010) * 20 = 0.02
        self.assertAlmostEqual(float(over["details"]["excess"]), 0.02, places=8)

    def test_effective_age_distance_weighting_hits_aging_boundary(self):
        rt = bot.BotRuntime()
        effective = rt._effective_age_seconds(2 * 3600.0, 10.0)  # 2h * (1 + 10/5) = 6h
        self.assertAlmostEqual(effective, 21600.0, places=8)
        self.assertEqual(rt._age_band_for_effective_age(effective), "aging")

    def test_collect_throughput_cycles_routes_churner_to_ranging_bucket(self):
        rt = bot.BotRuntime()
        cycle = sm.CycleRecord(
            trade_id="B",
            cycle=1,
            entry_price=0.1000,
            exit_price=0.1010,
            volume=20.0,
            gross_profit=0.02,
            fees=0.01,
            net_profit=0.01,
            entry_time=1000.0,
            exit_time=1100.0,
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1010,
                    now=1100.0,
                    completed_cycles=(cycle,),
                ),
            ),
        }

        rows = rt._collect_throughput_cycles()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["regime_at_entry"], cycle.regime_at_entry)

        rt._cycle_slot_mode[(0, "B", 1)] = "churner"
        rows_churner = rt._collect_throughput_cycles()
        self.assertEqual(len(rows_churner), 1)
        self.assertEqual(rows_churner[0]["regime_at_entry"], 1)

    def test_tighten_reprice_never_consumes_subsidy(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (40 * 3600)  # stale band
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=2.0,
        )

        with (
            mock.patch.object(config, "SUBSIDY_ENABLED", True),
            mock.patch.object(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck"),
            mock.patch.object(rt, "_volatility_profit_pct", return_value=0.8),
            mock.patch.object(rt, "_cancel_order", return_value=True),
            mock.patch.object(rt, "_place_order", return_value="TX-EXIT-NEW"),
        ):
            rt._run_self_healing_reprice(now_ts)

        pos = rt._position_ledger.get_position(position_id)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos["current_exit_price"]), 0.1010, places=6)
        self.assertEqual(int(pos["times_repriced"]), 1)

        journal = rt._position_ledger.get_journal(position_id)
        repriced = [row for row in journal if row.get("event_type") == "repriced"]
        self.assertEqual(len(repriced), 1)
        details = dict(repriced[0].get("details") or {})
        self.assertEqual(str(details.get("reason")), "tighten")
        self.assertAlmostEqual(float(details.get("subsidy_consumed", 0.0)), 0.0, places=8)

    def test_subsidized_reprice_full_when_balance_covers_needed(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)  # stuck band
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        rt._position_ledger.journal_event(
            position_id,
            "churner_profit",
            {"net_profit": 0.05},
            timestamp=now_ts - 120.0,
        )

        with (
            mock.patch.object(config, "SUBSIDY_ENABLED", True),
            mock.patch.object(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck"),
            mock.patch.object(config, "SUBSIDY_REPRICE_INTERVAL_SEC", 3600),
            mock.patch.object(rt, "_cancel_order", return_value=True),
            mock.patch.object(rt, "_place_order", return_value="TX-EXIT-NEW"),
        ):
            rt._run_self_healing_reprice(now_ts)

        pos = rt._position_ledger.get_position(position_id)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos["current_exit_price"]), 0.1005, places=6)

        journal = rt._position_ledger.get_journal(position_id)
        repriced = [row for row in journal if row.get("event_type") == "repriced"]
        self.assertEqual(len(repriced), 1)
        details = dict(repriced[0].get("details") or {})
        self.assertEqual(str(details.get("reason")), "subsidy")
        self.assertAlmostEqual(float(details.get("subsidy_consumed", 0.0)), 0.03, places=6)
        self.assertAlmostEqual(float(rt._position_ledger.get_subsidy_balance(0)), 0.02, places=6)

    def test_subsidized_reprice_partial_when_balance_is_insufficient(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)  # stuck band
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        rt._position_ledger.journal_event(
            position_id,
            "churner_profit",
            {"net_profit": 0.01},
            timestamp=now_ts - 120.0,
        )

        with (
            mock.patch.object(config, "SUBSIDY_ENABLED", True),
            mock.patch.object(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck"),
            mock.patch.object(config, "SUBSIDY_REPRICE_INTERVAL_SEC", 3600),
            mock.patch.object(rt, "_cancel_order", return_value=True),
            mock.patch.object(rt, "_place_order", return_value="TX-EXIT-NEW"),
        ):
            rt._run_self_healing_reprice(now_ts)

        pos = rt._position_ledger.get_position(position_id)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos["current_exit_price"]), 0.1015, places=6)

        journal = rt._position_ledger.get_journal(position_id)
        repriced = [row for row in journal if row.get("event_type") == "repriced"]
        self.assertEqual(len(repriced), 1)
        details = dict(repriced[0].get("details") or {})
        self.assertAlmostEqual(float(details.get("subsidy_consumed", 0.0)), 0.01, places=6)
        self.assertGreaterEqual(float(rt._position_ledger.get_subsidy_balance(0)), -1e-9)

    def test_subsidized_reprice_respects_cooldown(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)  # stuck band
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        rt._position_ledger.journal_event(
            position_id,
            "churner_profit",
            {"net_profit": 0.10},
            timestamp=now_ts - 300.0,
        )
        rt._position_ledger.journal_event(
            position_id,
            "repriced",
            {
                "old_price": 0.1020,
                "new_price": 0.1019,
                "old_txid": "TX-OLD",
                "new_txid": "TX-PREV",
                "reason": "subsidy",
                "subsidy_consumed": 0.002,
            },
            timestamp=now_ts - 120.0,
        )

        with (
            mock.patch.object(config, "SUBSIDY_ENABLED", True),
            mock.patch.object(config, "SUBSIDY_AUTO_REPRICE_BAND", "stuck"),
            mock.patch.object(config, "SUBSIDY_REPRICE_INTERVAL_SEC", 3600),
            mock.patch.object(rt, "_cancel_order", return_value=True) as cancel_mock,
            mock.patch.object(rt, "_place_order", return_value="TX-EXIT-NEW") as place_mock,
        ):
            rt._run_self_healing_reprice(now_ts)

        pos = rt._position_ledger.get_position(position_id)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos["current_exit_price"]), 0.1020, places=6)
        self.assertEqual(cancel_mock.call_count, 0)
        self.assertEqual(place_mock.call_count, 0)
        last_summary = dict(rt._self_heal_reprice_last_summary or {})
        skipped = dict(last_summary.get("skipped") or {})
        self.assertGreaterEqual(int(skipped.get("cooldown", 0)), 1)

    def test_churner_engine_does_not_auto_spawn_without_manual_activation(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, _position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )

        with (
            mock.patch.object(config, "CHURNER_ENABLED", True),
            mock.patch.object(config, "HERD_MODE_ENABLED", True),
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("RANGING", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
            mock.patch.object(rt, "_place_order", return_value="TX-CHURNER") as place_mock,
        ):
            rt._run_churner_engine(now_ts)

        self.assertEqual(place_mock.call_count, 0)
        churner = rt._churner_by_slot.get(0)
        self.assertIsNotNone(churner)
        self.assertFalse(bool(churner.active))

    def test_churner_spawn_rejects_non_ranging_regime(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, _position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        rt._manifold_score = bot.bayesian_engine.ManifoldScore(enabled=True, mts=0.70)
        with (
            mock.patch.object(config, "POSITION_LEDGER_ENABLED", True),
            mock.patch.object(config, "CHURNER_ENABLED", True),
            mock.patch.object(config, "HERD_MODE_ENABLED", True),
            mock.patch.object(config, "MTS_CHURNER_GATE", 0.30),
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("BULLISH", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
        ):
            ok, msg = rt._churner_spawn(slot_id=0)
        self.assertFalse(ok)
        self.assertEqual(str(msg), "regime_not_ranging")

    def test_churner_spawn_bypasses_mts_gate_when_mts_disabled(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, _position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        with (
            mock.patch.object(config, "POSITION_LEDGER_ENABLED", True),
            mock.patch.object(config, "CHURNER_ENABLED", True),
            mock.patch.object(config, "HERD_MODE_ENABLED", True),
            mock.patch.object(config, "MTS_ENABLED", False),
            mock.patch.object(config, "MTS_CHURNER_GATE", 0.30),
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("RANGING", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
        ):
            ok, msg = rt._churner_spawn(slot_id=0, now_ts=now_ts)
        self.assertTrue(ok)
        self.assertIn("spawned churner", str(msg))
        state = rt._churner_by_slot.get(0)
        self.assertIsNotNone(state)
        self.assertTrue(bool(state.active))
        self.assertEqual(str(state.stage), "idle")

    def test_churner_gate_check_uses_opposite_side_when_preferred_lacks_capital(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, position_id, parent_order = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        parent = dict(rt._position_ledger.get_position(position_id) or {})
        self.assertTrue(parent)
        with (
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("RANGING", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
            mock.patch.object(rt, "_available_free_balances", return_value=(0.0, 1_000_000.0)),
            mock.patch.object(
                rt,
                "_churner_entry_target_price",
                side_effect=lambda side, market: 0.1001 if str(side) == "buy" else 0.1002,
            ),
            mock.patch.object(bot.sm, "compute_order_volume", return_value=20.0),
        ):
            ok, reason, entry_price, _volume, _required_usd, chosen_side = rt._churner_gate_check(
                slot_id=0,
                state=state,
                parent=parent,
                parent_order=parent_order,
                now_ts=now_ts,
            )

        self.assertTrue(ok)
        self.assertEqual(str(reason), "ok")
        self.assertEqual(str(chosen_side), "sell")
        self.assertAlmostEqual(float(entry_price), 0.1002, places=8)

    def test_churner_gate_check_prefers_parent_side_when_both_sides_have_capital(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, position_id, parent_order = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        parent = dict(rt._position_ledger.get_position(position_id) or {})
        self.assertTrue(parent)
        with (
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("RANGING", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
            mock.patch.object(rt, "_available_free_balances", return_value=(1_000.0, 1_000_000.0)),
            mock.patch.object(
                rt,
                "_churner_entry_target_price",
                side_effect=lambda side, market: 0.1001 if str(side) == "buy" else 0.1002,
            ),
            mock.patch.object(bot.sm, "compute_order_volume", return_value=20.0),
        ):
            ok, reason, entry_price, _volume, _required_usd, chosen_side = rt._churner_gate_check(
                slot_id=0,
                state=state,
                parent=parent,
                parent_order=parent_order,
                now_ts=now_ts,
            )

        self.assertTrue(ok)
        self.assertEqual(str(reason), "ok")
        self.assertEqual(str(chosen_side), "buy")
        self.assertAlmostEqual(float(entry_price), 0.1001, places=8)

    def test_run_churner_engine_places_entry_with_gate_chosen_side(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (48 * 3600)
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        state.active = True
        state.stage = "idle"
        state.parent_position_id = int(position_id)
        state.parent_trade_id = "B"
        with (
            mock.patch.object(config, "POSITION_LEDGER_ENABLED", True),
            mock.patch.object(config, "CHURNER_ENABLED", True),
            mock.patch.object(config, "HERD_MODE_ENABLED", True),
            mock.patch.object(rt, "_policy_hmm_signal", return_value=("RANGING", 0.0, 0.0, True, {})),
            mock.patch.object(rt, "_compute_capacity_health", return_value={"open_order_headroom": 100}),
            mock.patch.object(rt, "_churner_gate_check", return_value=(True, "ok", 0.1002, 20.0, 2.004, "sell")),
            mock.patch.object(rt, "_try_reserve_loop_funds", return_value=True),
            mock.patch.object(rt, "_place_order", return_value="TX-CHURNER-ENTRY") as place_mock,
        ):
            rt._run_churner_engine(now_ts)

        self.assertEqual(place_mock.call_count, 1)
        self.assertEqual(str(place_mock.call_args.kwargs.get("side", "")), "sell")
        self.assertEqual(str(state.entry_side), "sell")
        self.assertEqual(str(state.stage), "entry_open")

    def test_churner_entry_timeout_cancels_and_resets(self):
        rt = bot.BotRuntime()
        state = rt._ensure_churner_state(0)
        state.active = True
        state.stage = "entry_open"
        state.entry_txid = "TX-CH-ENTRY"
        state.entry_placed_at = 1000.0
        state.parent_position_id = 1
        state.parent_trade_id = "B"
        with mock.patch.object(rt, "_cancel_order", return_value=True) as cancel_mock:
            rt._churner_timeout_tick(slot_id=0, state=state, now_ts=1400.0)
        self.assertEqual(cancel_mock.call_count, 1)
        self.assertTrue(bool(state.active))
        self.assertEqual(str(state.stage), "idle")
        self.assertEqual(str(state.entry_txid), "")

    def test_churner_profit_routes_to_subsidy_until_parent_healed(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,  # Not fillable at market 0.1000.
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        state.parent_position_id = int(position_id)
        state.parent_trade_id = "B"
        state.cycle_id = 7
        state.compound_usd = 0.0

        before = len(rt._position_ledger.get_journal(position_id))
        rt._churner_route_profit(slot_id=0, state=state, net_profit=0.01, now_ts=now_ts)
        after_rows = rt._position_ledger.get_journal(position_id)
        after = len(after_rows)
        self.assertEqual(after, before + 1)
        self.assertAlmostEqual(float(state.compound_usd), 0.0, places=8)
        self.assertEqual(str(after_rows[-1]["event_type"]), "churner_profit")
        self.assertAlmostEqual(float(after_rows[-1]["details"]["net_profit"]), 0.01, places=8)

    def test_churner_profit_compounds_after_parent_healed(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1005,  # Fillable for B at market=0.1000.
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        state.parent_position_id = int(position_id)
        state.parent_trade_id = "B"
        state.cycle_id = 9
        state.compound_usd = 0.0

        before = len(rt._position_ledger.get_journal(position_id))
        rt._churner_route_profit(slot_id=0, state=state, net_profit=0.02, now_ts=now_ts)
        after = len(rt._position_ledger.get_journal(position_id))
        self.assertEqual(after, before)  # No subsidy credit journal when healed.
        self.assertAlmostEqual(float(state.compound_usd), 0.02, places=8)

    def test_churner_cycle_records_churner_position_open_and_close(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (80 * 3600)
        rt, parent_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=1.0,
        )
        state = rt._ensure_churner_state(0)
        state.active = True
        state.stage = "entry_open"
        state.parent_position_id = int(parent_id)
        state.parent_trade_id = "B"
        state.cycle_id = 42
        state.entry_side = "buy"
        state.entry_txid = "TX-CHURNER-ENTRY"
        state.entry_placed_at = now_ts - 30.0

        with mock.patch.object(rt, "_place_order", return_value="TX-CHURNER-EXIT"):
            rt._churner_on_entry_fill(
                slot_id=0,
                txid="TX-CHURNER-ENTRY",
                fill_price=0.1000,
                fill_volume=20.0,
                fill_fee=0.0100,
                fill_cost=2.0,
                fill_ts=now_ts,
            )

        churner_pid = int(state.churner_position_id)
        self.assertGreater(churner_pid, 0)
        churner_row = rt._position_ledger.get_position(churner_pid)
        self.assertIsNotNone(churner_row)
        self.assertEqual(str(churner_row["slot_mode"]), "churner")
        self.assertEqual(str(churner_row["status"]), "open")

        rt._churner_on_exit_fill(
            slot_id=0,
            txid="TX-CHURNER-EXIT",
            fill_price=0.1005,
            fill_volume=20.0,
            fill_fee=0.0100,
            fill_cost=2.01,
            fill_ts=now_ts + 10.0,
        )

        churner_row_closed = rt._position_ledger.get_position(churner_pid)
        self.assertIsNotNone(churner_row_closed)
        self.assertEqual(str(churner_row_closed["status"]), "closed")
        self.assertTrue(bool(state.active))
        self.assertEqual(str(state.stage), "idle")

    def test_startup_migration_uses_sentinel_to_prevent_duplicate_import(self):
        rt = bot.BotRuntime()
        exit_order = sm.OrderState(
            local_id=10,
            side="sell",
            role="exit",
            price=0.1020,
            volume=20.0,
            trade_id="B",
            cycle=5,
            txid="TX-MIGRATE-1",
            placed_at=1000.0,
            entry_price=0.1000,
            entry_fee=0.0100,
            entry_filled_at=900.0,
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1000, now=1200.0, orders=(exit_order,)),
            ),
        }

        rt._migrate_open_exits_to_position_ledger()
        open_rows = rt._position_ledger.get_open_positions()
        self.assertEqual(len(open_rows), 1)
        self.assertTrue(bool(rt._position_ledger_migration_done))
        self.assertEqual(int(rt._position_ledger_migration_last_created), 1)
        self.assertEqual(int(rt._position_ledger_migration_last_scanned), 1)

        with mock.patch.object(rt._position_ledger, "open_position", side_effect=AssertionError("should not run")):
            rt._migrate_open_exits_to_position_ledger()
        open_rows_after = rt._position_ledger.get_open_positions()
        self.assertEqual(len(open_rows_after), 1)

    def test_startup_migration_recovers_from_stale_sentinel(self):
        rt = bot.BotRuntime()
        exit_order = sm.OrderState(
            local_id=11,
            side="buy",
            role="exit",
            price=0.0980,
            volume=20.0,
            trade_id="A",
            cycle=3,
            txid="TX-MIGRATE-2",
            placed_at=1000.0,
            entry_price=0.1000,
            entry_fee=0.0100,
            entry_filled_at=900.0,
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1000, now=1200.0, orders=(exit_order,)),
            ),
        }
        rt._position_ledger_migration_done = True

        rt._migrate_open_exits_to_position_ledger()

        open_rows = rt._position_ledger.get_open_positions()
        self.assertEqual(len(open_rows), 1)
        self.assertTrue(bool(rt._position_ledger_migration_done))
        self.assertEqual(int(rt._position_ledger_migration_last_created), 1)
        self.assertEqual(int(rt._position_ledger_migration_last_scanned), 1)

    def test_cleanup_queue_hides_kept_positions_until_timer_expires(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (10 * 86400.0)  # write_off band
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=2.0,
        )

        status_before = rt._self_healing_status_payload(now_ts)
        queue_before = list(status_before.get("cleanup_queue") or [])
        self.assertEqual(len(queue_before), 1)
        self.assertEqual(int(queue_before[0]["position_id"]), int(position_id))

        ok, msg = rt.self_heal_keep_holding(
            int(position_id),
            operator_reason="test_keep_holding",
            hold_sec=3600.0,
        )
        self.assertTrue(ok)
        self.assertIn("cleanup timer reset", msg)

        status_after = rt._self_healing_status_payload(now_ts)
        queue_after = list(status_after.get("cleanup_queue") or [])
        summary = dict(status_after.get("cleanup_queue_summary") or {})
        self.assertEqual(len(queue_after), 0)
        self.assertEqual(int(summary.get("hidden_by_hold", 0)), 1)

    def test_operator_reprice_breakeven_uses_subsidy_and_journals(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (8 * 86400.0)
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=2.0,
        )
        rt._position_ledger.journal_event(
            int(position_id),
            "churner_profit",
            {"net_profit": 5.0},
            timestamp=now_ts - 10.0,
        )

        with (
            mock.patch.object(rt, "_cancel_order", return_value=True),
            mock.patch.object(rt, "_place_order", return_value="TX-OP-BE"),
        ):
            ok, msg = rt.self_heal_reprice_breakeven(
                int(position_id),
                operator_reason="test_breakeven",
            )

        self.assertTrue(ok)
        self.assertIn("breakeven", msg.lower())
        pos = rt._position_ledger.get_position(int(position_id))
        self.assertIsNotNone(pos)
        fee_floor = max(0.0, float(config.ROUND_TRIP_FEE_PCT)) / 100.0
        expected_breakeven = round(0.1000 * (1.0 + fee_floor), int(rt.constraints.get("price_decimals", 6)))
        self.assertAlmostEqual(float(pos["current_exit_price"]), float(expected_breakeven), places=6)

        journal = rt._position_ledger.get_journal(int(position_id))
        event_types = [str(row.get("event_type") or "") for row in journal]
        self.assertIn("repriced", event_types)
        self.assertIn("operator_action", event_types)
        repriced = next(row for row in journal if str(row.get("event_type") or "") == "repriced")
        self.assertEqual(str((repriced.get("details") or {}).get("reason") or ""), "operator")

    def test_operator_close_market_marks_position_written_off(self):
        now_ts = 1_000_000.0
        entry_time = now_ts - (9 * 86400.0)
        rt, position_id, _ = self._build_open_position_runtime(
            now_ts=now_ts,
            entry_time=entry_time,
            entry_price=0.1000,
            exit_price=0.1020,
            target_profit_pct=2.0,
        )

        with (
            mock.patch.object(rt, "_cancel_order", return_value=True),
            mock.patch.object(rt, "_place_market_order", return_value="TX-MKT-1"),
            mock.patch.object(rt, "_apply_event") as apply_mock,
        ):
            ok, msg = rt.self_heal_close_at_market(
                int(position_id),
                operator_reason="test_write_off",
            )

        self.assertTrue(ok)
        self.assertIn("closed at market", msg.lower())
        self.assertEqual(apply_mock.call_count, 1)
        pos = rt._position_ledger.get_position(int(position_id))
        self.assertIsNotNone(pos)
        self.assertEqual(str(pos["status"]), "closed")
        self.assertEqual(str(pos["close_reason"]), "write_off")
        journal = rt._position_ledger.get_journal(int(position_id))
        event_types = [str(row.get("event_type") or "") for row in journal]
        self.assertIn("written_off", event_types)
        self.assertIn("operator_action", event_types)


if __name__ == "__main__":
    unittest.main()
