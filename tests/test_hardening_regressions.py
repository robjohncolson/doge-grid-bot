import io
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import bot
import config
import kraken_client
import state_machine as sm


class DogeV1StateMachineTests(unittest.TestCase):
    def _cfg(self) -> sm.EngineConfig:
        return sm.EngineConfig(
            entry_pct=0.2,
            profit_pct=1.0,
            refresh_pct=1.0,
            order_size_usd=2.0,
            price_decimals=6,
            volume_decimals=0,
            min_volume=13.0,
            min_cost_usd=0.0,
            maker_fee_pct=0.25,
            s1_orphan_after_sec=600,
            s2_orphan_after_sec=1800,
            loss_backoff_start=3,
            loss_cooldown_start=5,
            loss_cooldown_sec=900,
            max_recovery_slots=2,
        )

    def test_s0_invariants_hold(self):
        st = sm.PairState(market_price=0.1, now=1000)
        cfg = self._cfg()
        st, a = sm.add_entry_order(st, cfg, "sell", "A", 1, order_size_usd=2.0)
        st, b = sm.add_entry_order(st, cfg, "buy", "B", 1, order_size_usd=2.0)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(sm.derive_phase(st), "S0")
        self.assertEqual(sm.check_invariants(st), [])

    def test_entry_fill_preserves_volume_for_exit(self):
        st = sm.PairState(market_price=0.1, now=1000)
        cfg = self._cfg()
        st, action = sm.add_entry_order(st, cfg, "buy", "B", 1, order_size_usd=2.7)
        self.assertIsNotNone(action)
        order = st.orders[0]

        fill = sm.FillEvent(
            order_local_id=order.local_id,
            txid="TX1",
            side="buy",
            price=order.price,
            volume=27.0,
            fee=0.01,
            timestamp=1010,
        )
        st2, actions = sm.transition(st, fill, cfg, order_size_usd=2.7)
        self.assertEqual(len([o for o in st2.orders if o.role == "exit"]), 1)
        exit_order = [o for o in st2.orders if o.role == "exit"][0]
        self.assertEqual(exit_order.volume, 27.0)
        self.assertTrue(any(isinstance(x, sm.PlaceOrderAction) and x.role == "exit" for x in actions))

    def test_exit_fill_applies_base_reentry_cooldown(self):
        st = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="sell",
                    role="exit",
                    price=0.1008,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    placed_at=999.0,
                    entry_price=0.1,
                    entry_fee=0.0,
                    entry_filled_at=950.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=0.0998,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-ENTRY",
                    placed_at=999.0,
                ),
            ),
        )
        cfg = sm.EngineConfig(
            entry_pct=0.2,
            profit_pct=1.0,
            refresh_pct=1.0,
            order_size_usd=2.0,
            price_decimals=6,
            volume_decimals=0,
            min_volume=13.0,
            min_cost_usd=0.0,
            maker_fee_pct=0.25,
            s1_orphan_after_sec=600,
            s2_orphan_after_sec=1800,
            loss_backoff_start=3,
            loss_cooldown_start=5,
            loss_cooldown_sec=900,
            reentry_base_cooldown_sec=60.0,
        )
        fill = sm.FillEvent(
            order_local_id=1,
            txid="TX-A-EXIT",
            side="sell",
            price=0.1008,
            volume=13.0,
            fee=0.01,
            timestamp=1100.0,
        )
        st2, actions = sm.transition(st, fill, cfg, order_size_usd=2.0)
        self.assertAlmostEqual(st2.cooldown_until_a, 1160.0)
        self.assertFalse(
            any(isinstance(x, sm.PlaceOrderAction) and x.role == "entry" and x.trade_id == "A" for x in actions)
        )

    def test_add_entry_order_respects_trade_cooldown(self):
        st = sm.PairState(
            market_price=0.1,
            now=1000.0,
            cooldown_until_a=1060.0,
        )
        cfg = self._cfg()
        st2, action = sm.add_entry_order(
            st,
            cfg,
            side="sell",
            trade_id="A",
            cycle=1,
            order_size_usd=2.0,
            reason="cooldown_guard",
        )
        self.assertIsNone(action)
        self.assertEqual(st2.orders, ())

    def test_s1_timeout_orphans_stale_exit(self):
        st = sm.PairState(
            market_price=101.0,
            now=1000,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=100.0,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    entry_price=101.5,
                    entry_fee=0.02,
                    entry_filled_at=300.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=99.5,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-ENTRY",
                    placed_at=900.0,
                ),
            ),
            cycle_a=1,
            cycle_b=1,
            next_order_id=3,
            next_recovery_id=1,
        )
        cfg = self._cfg()

        st2, actions = sm.transition(st, sm.TimerTick(timestamp=1000.0), cfg, order_size_usd=2.0)
        self.assertEqual(len(st2.recovery_orders), 1)
        self.assertTrue(any(isinstance(x, sm.OrphanOrderAction) for x in actions))

    def test_sticky_timer_tick_does_not_orphan_s1_exit(self):
        st = sm.PairState(
            market_price=101.0,
            now=1000.0,
            s2_entered_at=900.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=100.0,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    entry_price=101.5,
                    entry_fee=0.02,
                    entry_filled_at=300.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=99.5,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-ENTRY",
                    placed_at=900.0,
                ),
            ),
            cycle_a=1,
            cycle_b=1,
            next_order_id=3,
            next_recovery_id=1,
        )
        base_cfg = self._cfg()
        cfg = sm.EngineConfig(**{**base_cfg.__dict__, "sticky_mode_enabled": True})

        st2, actions = sm.transition(st, sm.TimerTick(timestamp=1000.0), cfg, order_size_usd=2.0)
        self.assertEqual(len(st2.recovery_orders), 0)
        self.assertFalse(any(isinstance(x, sm.OrphanOrderAction) for x in actions))
        self.assertIsNone(st2.s2_entered_at)

    def test_s1_orphan_enforces_recovery_cap_furthest_then_oldest(self):
        st = sm.PairState(
            market_price=101.0,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=100.0,
                    volume=13.0,
                    trade_id="A",
                    cycle=7,
                    txid="TX-A-EXIT",
                    entry_price=101.5,
                    entry_fee=0.02,
                    entry_filled_at=300.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=99.5,
                    volume=13.0,
                    trade_id="B",
                    cycle=4,
                    txid="TX-B-ENTRY",
                    placed_at=900.0,
                ),
            ),
            recovery_orders=(
                sm.RecoveryOrder(
                    recovery_id=1,
                    side="sell",
                    price=101.0,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    entry_price=105.0,
                    entry_fee=0.03,
                    entry_filled_at=60.0,
                    orphaned_at=100.0,
                    txid="TX-OLD-1",
                    reason="s1_timeout",
                ),
                sm.RecoveryOrder(
                    recovery_id=2,
                    side="sell",
                    price=102.0,
                    volume=13.0,
                    trade_id="B",
                    cycle=2,
                    entry_price=106.0,
                    entry_fee=0.02,
                    entry_filled_at=120.0,
                    orphaned_at=200.0,
                    txid="TX-OLD-2",
                    reason="s1_timeout",
                ),
            ),
            cycle_a=7,
            cycle_b=4,
            next_order_id=3,
            next_recovery_id=3,
        )
        cfg = self._cfg()

        st2, actions = sm.transition(st, sm.TimerTick(timestamp=1000.0), cfg, order_size_usd=2.0)

        self.assertEqual(len(st2.recovery_orders), 2)
        remaining_ids = {r.recovery_id for r in st2.recovery_orders}
        self.assertNotIn(2, remaining_ids)
        self.assertIn(1, remaining_ids)
        self.assertIn(3, remaining_ids)
        cancels = [a for a in actions if isinstance(a, sm.CancelOrderAction)]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].txid, "TX-OLD-2")
        self.assertEqual(cancels[0].reason, "recovery_cap_evict_priority")
        books = [a for a in actions if isinstance(a, sm.BookCycleAction)]
        self.assertEqual(len(books), 1)
        self.assertTrue(books[0].from_recovery)
        self.assertLess(books[0].net_profit, 0.0)
        self.assertEqual(st2.total_round_trips, 1)
        self.assertEqual(len(st2.completed_cycles), 1)

    def test_s2_timeout_orphans_worse_leg(self):
        st = sm.PairState(
            market_price=0.11,
            now=4000,
            s2_entered_at=2000,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=0.10,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A",
                    entry_price=0.112,
                    entry_fee=0.01,
                    entry_filled_at=1900,
                ),
                sm.OrderState(
                    local_id=2,
                    side="sell",
                    role="exit",
                    price=0.13,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B",
                    entry_price=0.108,
                    entry_fee=0.01,
                    entry_filled_at=1900,
                ),
            ),
            cycle_a=1,
            cycle_b=1,
            next_order_id=3,
            next_recovery_id=1,
        )
        cfg = self._cfg()

        st2, actions = sm.transition(st, sm.TimerTick(timestamp=4005.0), cfg, order_size_usd=2.0)
        self.assertTrue(any(isinstance(x, sm.OrphanOrderAction) for x in actions))
        # Worse leg is the sell exit at 0.13 (farther from market 0.11).
        rec = st2.recovery_orders[0]
        self.assertEqual(rec.side, "sell")
        self.assertEqual(rec.trade_id, "B")

    def test_fill_clears_s2_flag_after_leaving_s2(self):
        st = sm.PairState(
            market_price=0.11,
            now=2000.0,
            s2_entered_at=1500.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=0.10,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    entry_price=0.112,
                    entry_fee=0.01,
                    entry_filled_at=1400.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="sell",
                    role="exit",
                    price=0.13,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-EXIT",
                    entry_price=0.108,
                    entry_fee=0.01,
                    entry_filled_at=1400.0,
                ),
            ),
            cycle_a=1,
            cycle_b=1,
            next_order_id=3,
            next_recovery_id=1,
        )
        cfg = self._cfg()
        fill = sm.FillEvent(
            order_local_id=2,
            txid="TX-B-EXIT",
            side="sell",
            price=0.13,
            volume=13.0,
            fee=0.01,
            timestamp=2010.0,
        )

        st2, _ = sm.transition(st, fill, cfg, order_size_usd=2.0)
        self.assertEqual(sm.derive_phase(st2), "S1a")
        self.assertIsNone(st2.s2_entered_at)
        self.assertEqual(sm.check_invariants(st2), [])

    def test_compute_order_volume_waits_below_minimum(self):
        cfg = self._cfg()
        vol = sm.compute_order_volume(price=1.0, cfg=cfg, order_size_usd=5.0)
        self.assertIsNone(vol)

    def test_pair_state_mode_source_round_trips_and_sanitizes(self):
        st = sm.PairState(market_price=0.1, now=1000.0, mode_source="balance")
        raw = sm.to_dict(st)
        self.assertEqual(raw["mode_source"], "balance")
        restored = sm.from_dict(raw)
        self.assertEqual(restored.mode_source, "balance")

        raw["mode_source"] = "unexpected"
        restored_bad = sm.from_dict(raw)
        self.assertEqual(restored_bad.mode_source, "none")

    def test_add_entry_order_uses_side_specific_entry_pct(self):
        st = sm.PairState(market_price=0.1, now=1000.0)
        cfg = sm.EngineConfig(
            entry_pct=0.2,
            entry_pct_a=0.50,
            entry_pct_b=0.10,
            profit_pct=1.0,
            refresh_pct=1.0,
            order_size_usd=2.0,
            price_decimals=6,
            volume_decimals=0,
            min_volume=13.0,
            min_cost_usd=0.0,
            maker_fee_pct=0.25,
        )

        st, a_sell = sm.add_entry_order(
            st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=2.0, reason="unit_a"
        )
        st, a_buy = sm.add_entry_order(
            st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=2.0, reason="unit_b"
        )

        self.assertIsNotNone(a_sell)
        self.assertIsNotNone(a_buy)
        self.assertAlmostEqual(float(a_sell.price), 0.1005, places=6)
        self.assertAlmostEqual(float(a_buy.price), 0.0999, places=6)


class BotEventLogTests(unittest.TestCase):
    @mock.patch("supabase_store.save_event")
    def test_event_id_is_monotonic(self, save_event):
        rt = bot.BotRuntime()
        rt.next_event_id = 100
        rt._log_event(0, "S0", "S1b", "fill", {"txid": "A"})
        rt._log_event(0, "S1b", "S0", "fill", {"txid": "B"})

        self.assertEqual(save_event.call_count, 2)
        first = save_event.call_args_list[0].args[0]
        second = save_event.call_args_list[1].args[0]
        self.assertEqual(first["event_id"], 100)
        self.assertEqual(second["event_id"], 101)
        self.assertEqual(rt.next_event_id, 102)

    def test_loop_private_budget_caps_query_batch(self):
        rt = bot.BotRuntime()
        with mock.patch.object(config, "MAX_API_CALLS_PER_LOOP", 2):
            rt.begin_loop()
            rt.loop_private_calls = 1
            with mock.patch("kraken_client.query_orders_batched", return_value={}) as q:
                rt._query_orders_batched([f"TX{i}" for i in range(120)], batch_size=50)
                q.assert_called_once()
                bounded = q.call_args.kwargs["txids"] if "txids" in q.call_args.kwargs else q.call_args.args[0]
                self.assertEqual(len(bounded), 50)
            rt.end_loop()

    def test_entry_scheduler_defers_and_drains_pending_entries(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        now = bot._now()
        rt.last_price = 0.1
        rt.last_price_ts = now
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=now,
                ),
            )
        }
        cfg = rt._engine_cfg(rt.slots[0])
        st = rt.slots[0].state
        st, a1 = sm.add_entry_order(st, cfg, side="sell", trade_id="A", cycle=st.cycle_a, order_size_usd=2.0, reason="t1")
        st, a2 = sm.add_entry_order(st, cfg, side="buy", trade_id="B", cycle=st.cycle_b, order_size_usd=2.0, reason="t2")
        rt.slots[0].state = st
        self.assertIsNotNone(a1)
        self.assertIsNotNone(a2)

        rt.entry_adds_per_loop_cap = 1
        rt.entry_adds_per_loop_used = 0
        rt._loop_available_usd = 100.0
        rt._loop_available_doge = 1000.0

        with mock.patch.object(rt, "_place_order", side_effect=["TX-A", "TX-B"]):
            rt._execute_actions(0, [a1, a2], "unit_test")
            o1 = sm.find_order(rt.slots[0].state, a1.local_id)
            o2 = sm.find_order(rt.slots[0].state, a2.local_id)
            self.assertIsNotNone(o1)
            self.assertIsNotNone(o2)
            self.assertTrue(bool(o1.txid) ^ bool(o2.txid))
            self.assertEqual(rt._entry_adds_deferred_total, 1)

            rt.entry_adds_per_loop_used = 0
            rt._drain_pending_entry_orders("unit_test_drain", skip_stale=False)

        o1 = sm.find_order(rt.slots[0].state, a1.local_id)
        o2 = sm.find_order(rt.slots[0].state, a2.local_id)
        self.assertTrue(bool(o1 and o1.txid))
        self.assertTrue(bool(o2 and o2.txid))
        self.assertEqual(rt._entry_adds_drained_total, 1)

    def test_deferred_entries_purged_for_suppressed_side(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        now = bot._now()
        rt.last_price = 0.1
        rt.last_price_ts = now
        rt._regime_tier = 2
        rt._regime_side_suppressed = "A"
        rt._regime_tier2_grace_start = now - 120.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=now,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=0.1002,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="",
                            placed_at=now - 2.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="",
                            placed_at=now - 1.0,
                        ),
                    ),
                    next_order_id=3,
                ),
            )
        }

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
            with mock.patch.object(config, "REGIME_SUPPRESSION_GRACE_SEC", 60.0):
                with mock.patch.object(rt, "_execute_actions") as exec_actions:
                    rt._drain_pending_entry_orders("unit_test_regime_drain", skip_stale=False)

        self.assertIsNone(sm.find_order(rt.slots[0].state, 1))
        self.assertIsNotNone(sm.find_order(rt.slots[0].state, 2))
        exec_actions.assert_called_once()
        action = exec_actions.call_args.args[1][0]
        self.assertEqual(action.side, "buy")

    def test_engine_cfg_regime_spacing_bias_gated_by_actuation_toggle(self):
        rt = bot.BotRuntime()
        rt.entry_pct = 0.35
        rt._regime_tier = 1
        rt._regime_shadow_state.update({
            "regime": "BULLISH",
            "confidence": 0.90,
            "bias_signal": 0.80,
        })
        rt._hmm_module = SimpleNamespace(
            compute_grid_bias=lambda _state: {
                "entry_spacing_mult_a": 1.5,
                "entry_spacing_mult_b": 0.7,
            }
        )
        slot = bot.SlotRuntime(slot_id=0, state=sm.PairState(market_price=0.1, now=1000.0))

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
            cfg_shadow = rt._engine_cfg(slot)
        self.assertAlmostEqual(float(cfg_shadow.entry_pct_a), 0.35)
        self.assertAlmostEqual(float(cfg_shadow.entry_pct_b), 0.35)

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
            cfg_active = rt._engine_cfg(slot)
        self.assertAlmostEqual(float(cfg_active.entry_pct_a), 0.525)
        self.assertAlmostEqual(float(cfg_active.entry_pct_b), 0.245)

    @mock.patch("supabase_store.save_fill")
    def test_replay_missed_fills_aggregates_and_applies_once(self, _save_fill):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX1",
                            placed_at=900.0,
                        ),
                    ),
                ),
            )
        }
        history = {
            "T1": {"ordertxid": "TX1", "pair": "XDGUSD", "vol": "5", "cost": "0.5000", "fee": "0.001", "time": 1001},
            "T2": {"ordertxid": "TX1", "pair": "XDGUSD", "vol": "8", "cost": "0.8000", "fee": "0.0016", "time": 1002},
        }
        with mock.patch.object(rt, "_get_trades_history", return_value=history):
            with mock.patch.object(rt, "_apply_event") as apply_event:
                rt._replay_missed_fills(open_orders={})
                apply_event.assert_called_once()
                ev = apply_event.call_args.args[1]
                self.assertEqual(ev.txid, "TX1")
                self.assertAlmostEqual(ev.volume, 13.0, places=8)
                self.assertAlmostEqual(ev.price, 0.1, places=8)
                self.assertIn("TX1", rt.seen_fill_txids)

    def test_min_size_wait_state_does_not_halt(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(),
                ),
            )
        }
        with mock.patch.object(config, "ORDER_SIZE_USD", 0.5):
            self.assertTrue(rt._is_min_size_wait_state(0, ["S0 must be exactly A sell entry + B buy entry"]))
            with mock.patch.object(rt, "halt") as halt_mock:
                rt._validate_slot(0)
                halt_mock.assert_not_called()

    def test_bootstrap_pending_state_does_not_halt(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=0.1002,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }
        with mock.patch.object(config, "ORDER_SIZE_USD", 5.0):
            self.assertFalse(rt._is_min_size_wait_state(0, ["S0 must be exactly A sell entry + B buy entry"]))
            self.assertTrue(rt._is_bootstrap_pending_state(0, ["S0 must be exactly A sell entry + B buy entry"]))
            with mock.patch.object(rt, "halt") as halt_mock:
                rt._validate_slot(0)
                halt_mock.assert_not_called()

    def test_normalize_slot_mode_tracks_single_sided_entry(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }

        rt._normalize_slot_mode(0)
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

        rt.slots[0].state = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="sell",
                    role="entry",
                    price=0.1002,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-ENTRY",
                    placed_at=999.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=0.0998,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-ENTRY",
                    placed_at=999.0,
                ),
            ),
            long_only=True,
            short_only=False,
        )
        rt._normalize_slot_mode(0)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_normalize_slot_mode_clears_flags_when_slot_empty(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    long_only=True,
                    short_only=False,
                ),
            )
        }
        rt._normalize_slot_mode(0)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_normalize_slot_mode_tracks_single_sided_exit(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="exit",
                            price=0.1008,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-EXIT",
                            placed_at=999.0,
                            entry_price=0.1,
                        ),
                    ),
                ),
            )
        }

        rt._normalize_slot_mode(0)
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)
        with mock.patch.object(rt, "halt") as halt_mock:
            rt._validate_slot(0)
            halt_mock.assert_not_called()

        rt.slots[0].state = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="exit",
                    price=0.0992,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B-EXIT",
                    placed_at=999.0,
                    entry_price=0.1,
                ),
            ),
        )
        rt._normalize_slot_mode(0)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertTrue(rt.slots[0].state.short_only)
        with mock.patch.object(rt, "halt") as halt_mock:
            rt._validate_slot(0)
            halt_mock.assert_not_called()

    def test_normalize_slot_mode_clears_flags_for_non_degraded_s1(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="exit",
                            price=0.0992,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-EXIT",
                            placed_at=999.0,
                            entry_price=0.1008,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    long_only=False,
                    short_only=True,
                ),
            )
        }

        rt._normalize_slot_mode(0)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_auto_repair_degraded_s0_adds_missing_entry(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    long_only=True,
                    short_only=False,
                    next_order_id=2,
                ),
            )
        }

        with mock.patch.object(rt, "_safe_balance", return_value={"ZUSD": "50.0", "XXDG": "1000.0"}):
            with mock.patch.object(rt, "_execute_actions") as exec_actions:
                rt._auto_repair_degraded_slot(0)
                exec_actions.assert_called_once()

        sides = [o.side for o in rt.slots[0].state.orders if o.role == "entry"]
        self.assertIn("buy", sides)
        self.assertIn("sell", sides)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_auto_repair_skips_regime_mode_source(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    long_only=True,
                    short_only=False,
                    mode_source="regime",
                    next_order_id=2,
                ),
            )
        }

        with mock.patch.object(rt, "_safe_balance") as safe_balance:
            with mock.patch.object(rt, "_execute_actions") as exec_actions:
                rt._auto_repair_degraded_slot(0)

        safe_balance.assert_not_called()
        exec_actions.assert_not_called()
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertEqual(rt.slots[0].state.mode_source, "regime")

    def test_auto_repair_degraded_s1a_adds_missing_entry(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="exit",
                            price=0.0992,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-EXIT",
                            placed_at=999.0,
                            entry_price=0.1008,
                        ),
                    ),
                    cycle_a=2,
                    cycle_b=4,
                    long_only=False,
                    short_only=True,
                    next_order_id=2,
                ),
            )
        }

        with mock.patch.object(rt, "_safe_balance", return_value={"ZUSD": "50.0", "XXDG": "1000.0"}):
            with mock.patch.object(rt, "_execute_actions") as exec_actions:
                rt._auto_repair_degraded_slot(0)
                exec_actions.assert_called_once()

        buy_entries = [o for o in rt.slots[0].state.orders if o.role == "entry" and o.side == "buy"]
        self.assertEqual(len(buy_entries), 1)
        self.assertEqual(buy_entries[0].trade_id, "B")
        self.assertEqual(buy_entries[0].cycle, 4)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_validate_slot_skips_bootstrap_pending_for_empty_single_sided_s0(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    long_only=True,
                    short_only=False,
                ),
            )
        }

        with mock.patch.object(rt, "halt") as halt_mock:
            rt._validate_slot(0)
            halt_mock.assert_not_called()

    def test_apply_event_normalizes_before_validate_when_no_actions(self):
        rt = bot.BotRuntime()
        state = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="sell",
                    role="exit",
                    price=0.1008,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    placed_at=999.0,
                    entry_price=0.1,
                ),
            ),
        )
        rt.slots = {0: bot.SlotRuntime(slot_id=0, state=state)}

        with mock.patch.object(sm, "transition", return_value=(state, [])):
            with mock.patch.object(rt, "_log_event"):
                with mock.patch.object(rt, "halt") as halt_mock:
                    rt._apply_event(
                        0,
                        sm.TimerTick(timestamp=1001.0),
                        "timer",
                        {},
                    )
                    halt_mock.assert_not_called()
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)

    def test_status_payload_capacity_uses_internal_fallback_when_kraken_unavailable(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    recovery_orders=(
                        sm.RecoveryOrder(
                            recovery_id=1,
                            side="sell",
                            price=0.1008,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            entry_price=0.1,
                            orphaned_at=900.0,
                            txid="TX-REC",
                        ),
                    ),
                ),
            )
        }
        rt._kraken_open_orders_current = None

        with mock.patch.object(config, "KRAKEN_OPEN_ORDERS_PER_PAIR_LIMIT", 225):
            with mock.patch.object(config, "OPEN_ORDER_SAFETY_RATIO", 0.75):
                payload = rt.status_payload()
        cfh = payload["capacity_fill_health"]
        self.assertEqual(cfh["open_orders_source"], "internal_fallback")
        self.assertEqual(cfh["open_orders_internal"], 2)
        self.assertEqual(cfh["open_orders_current"], 2)
        self.assertEqual(cfh["open_orders_safe_cap"], 168)
        self.assertEqual(cfh["open_order_headroom"], 166)
        self.assertIn("entry_scheduler", cfh)

    def test_status_payload_exposes_entry_scheduler_fields(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.entry_adds_per_loop_cap = 2
        rt.entry_adds_per_loop_used = 1
        rt._entry_adds_deferred_total = 5
        rt._entry_adds_drained_total = 3
        rt._entry_adds_last_deferred_at = 1000.0
        rt._entry_adds_last_drained_at = 1005.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }
        payload = rt.status_payload()
        cfh = payload["capacity_fill_health"]
        es = cfh["entry_scheduler"]
        self.assertEqual(es["cap_per_loop"], 2)
        self.assertEqual(es["used_this_loop"], 1)
        self.assertEqual(es["pending_entries"], 1)
        self.assertEqual(es["deferred_total"], 5)
        self.assertEqual(es["drained_total"], 3)
        self.assertIn("auto_recovery_drain_total", cfh)
        self.assertIn("auto_recovery_drain_last_at", cfh)
        self.assertIn("auto_recovery_drain_threshold_pct", cfh)

    def test_status_payload_exposes_factory_fields(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    s2_entered_at=900.0,
                ),
            )
        }

        payload = rt.status_payload()
        self.assertEqual(payload["s2_orphan_after_sec"], float(config.S2_ORPHAN_AFTER_SEC))
        self.assertEqual(payload["stale_price_max_age_sec"], float(config.STALE_PRICE_MAX_AGE_SEC))
        self.assertEqual(payload["reentry_base_cooldown_sec"], float(config.REENTRY_BASE_COOLDOWN_SEC))
        self.assertEqual(payload["slots"][0]["s2_entered_at"], 900.0)
        self.assertIn("pnl_audit", payload)
        self.assertTrue(payload["pnl_audit"]["ok"])
        self.assertAlmostEqual(payload["pnl_audit"]["profit_drift"], 0.0)
        self.assertAlmostEqual(payload["pnl_audit"]["loss_drift"], 0.0)
        self.assertEqual(payload["pnl_audit"]["trips_drift"], 0)

    def test_status_payload_exposes_sticky_vintage_and_release_health(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._last_balance_snapshot = {"ZUSD": "100.0", "XXDG": "1000.0"}
        rt._sticky_release_total = 3
        rt._sticky_release_last_at = 1234.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="exit",
                            price=0.12,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-EXIT",
                            placed_at=900.0,
                            entry_price=0.1,
                            entry_filled_at=200.0,
                        ),
                    ),
                ),
            )
        }

        payload = rt.status_payload()
        self.assertIn("sticky_mode", payload)
        self.assertIn("slot_vintage", payload)
        self.assertIn("release_health", payload)
        self.assertIn("oldest_exit_age_sec", payload["slot_vintage"])
        self.assertIn("stuck_capital_pct", payload["slot_vintage"])
        self.assertIn("vintage_warn", payload["slot_vintage"])
        self.assertIn("vintage_critical", payload["slot_vintage"])
        self.assertEqual(payload["release_health"]["sticky_release_total"], 3)
        self.assertEqual(payload["release_health"]["sticky_release_last_at"], 1234.0)

    def test_sync_ohlcv_candles_queues_closed_rows_only(self):
        rt = bot.BotRuntime()
        rt._ohlcv_last_sync_ts = 0.0
        rows = [
            [1500, "0.10", "0.11", "0.09", "0.105", "0.103", "1200.0", "42"],
            [1800, "0.105", "0.12", "0.10", "0.11", "0.108", "900.0", "35"],
        ]
        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_OHLCV_INTERVAL_MIN", 5):
                with mock.patch.object(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0):
                    with mock.patch("kraken_client.get_ohlc_page", return_value=(rows, 9999)):
                        with mock.patch("supabase_store.queue_ohlcv_candles") as queue_mock:
                            rt._sync_ohlcv_candles(now=2000.0)

        queue_mock.assert_called_once()
        queued = queue_mock.call_args.args[0]
        self.assertEqual(len(queued), 1)
        self.assertAlmostEqual(float(queued[0]["time"]), 1500.0)
        self.assertEqual(rt._ohlcv_since_cursor, 9999)
        self.assertEqual(rt._ohlcv_last_rows_queued, 1)

    def test_fetch_training_candles_prefers_supabase_ohlcv(self):
        rt = bot.BotRuntime()
        rows = [
            {"time": 1000.0, "open": 0.10, "high": 0.11, "low": 0.09, "close": 0.101, "volume": 1000.0, "trade_count": 10},
            {"time": 1300.0, "open": 0.101, "high": 0.112, "low": 0.10, "close": 0.102, "volume": 1100.0, "trade_count": 11},
            {"time": 1600.0, "open": 0.102, "high": 0.113, "low": 0.101, "close": 0.103, "volume": 1200.0, "trade_count": 12},
        ]
        with mock.patch("supabase_store.load_ohlcv_candles", return_value=rows):
            with mock.patch("kraken_client.get_ohlc") as kraken_mock:
                closes, volumes = rt._fetch_training_candles(count=3)
        self.assertEqual(closes, [0.101, 0.102, 0.103])
        self.assertEqual(volumes, [1000.0, 1100.0, 1200.0])
        kraken_mock.assert_not_called()

    def test_status_payload_exposes_hmm_data_pipeline_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        with mock.patch.object(rt, "_hmm_data_readiness", return_value={"ready_for_min_train": True, "samples": 2000}):
            payload = rt.status_payload()
        self.assertIn("hmm_data_pipeline", payload)
        self.assertIn("hmm_regime", payload)
        self.assertTrue(payload["hmm_data_pipeline"]["ready_for_min_train"])
        self.assertEqual(payload["hmm_data_pipeline"]["samples"], 2000)
        self.assertIn("bias_signal", payload["hmm_regime"])
        self.assertIn("probabilities", payload["hmm_regime"])

    def test_status_payload_exposes_regime_directional_shadow_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.80,
            "bias_signal": 0.70,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    rt._update_regime_tier(now=1000.0)
                    payload = rt.status_payload()

        self.assertIn("regime_directional", payload)
        regime = payload["regime_directional"]
        self.assertEqual(regime["tier"], 2)
        self.assertEqual(regime["suppressed_side"], "A")
        self.assertTrue(regime["shadow_enabled"])
        self.assertFalse(regime["actuation_enabled"])

    def test_update_regime_tier_shadow_does_not_actuate_slot_modes(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=0.1002,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-ENTRY",
                            placed_at=999.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    long_only=False,
                    short_only=False,
                    mode_source="none",
                ),
            )
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BEARISH",
            "confidence": 0.90,
            "bias_signal": -0.80,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 2)
        self.assertEqual(rt._regime_side_suppressed, "B")
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)
        self.assertEqual(rt.slots[0].state.mode_source, "none")

    def test_update_regime_tier_persists_transition_row(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 0
        rt._regime_tier_entered_at = 900.0
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.80,
            "bias_signal": 0.70,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_TIER1_CONFIDENCE", 0.20):
                        with mock.patch.object(config, "REGIME_TIER2_CONFIDENCE", 0.50):
                            with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 0.0):
                                with mock.patch("bot.supabase_store.save_regime_tier_transition") as save_transition:
                                    rt._update_regime_tier(now=1000.0)

        save_transition.assert_called_once()
        row = save_transition.call_args.args[0]
        self.assertEqual(row["pair"], rt.pair)
        self.assertEqual(row["from_tier"], 0)
        self.assertEqual(row["to_tier"], 2)
        self.assertEqual(row["from_label"], "symmetric")
        self.assertEqual(row["to_label"], "directional")
        self.assertEqual(row["suppressed_side"], "A")
        self.assertEqual(row["favored_side"], "B")
        self.assertEqual(row["shadow_enabled"], True)
        self.assertEqual(row["actuation_enabled"], False)
        self.assertEqual(row["hmm_ready"], True)
        self.assertAlmostEqual(float(row["dwell_sec"]), 100.0)

    def test_update_regime_tier_directional_gate_beats_hysteresis(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_tier_entered_at = 100.0
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.95,
            "bias_signal": 0.02,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 300.0):
                        with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                            with mock.patch("bot.supabase_store.save_regime_tier_transition") as save_transition:
                                rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 0)
        self.assertFalse(bool(rt._regime_shadow_state.get("directional_ok_tier2")))
        self.assertIsNone(rt._regime_side_suppressed)
        save_transition.assert_called_once()
        row = save_transition.call_args.args[0]
        self.assertEqual(row["from_tier"], 2)
        self.assertEqual(row["to_tier"], 0)

    def test_update_regime_tier_dwell_blocks_gate_downgrade(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_tier_entered_at = 950.0
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.95,
            "bias_signal": 0.02,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 300.0):
                        with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                            with mock.patch("bot.supabase_store.save_regime_tier_transition") as save_transition:
                                rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 2)
        self.assertFalse(bool(rt._regime_shadow_state.get("directional_ok_tier2")))
        save_transition.assert_not_called()

    def test_tier2_grace_period_delays_suppression(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_side_suppressed = "A"
        rt._regime_tier2_grace_start = 1000.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=0.1002,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-ENTRY",
                            placed_at=999.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    next_order_id=3,
                ),
            )
        }

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
            with mock.patch.object(config, "REGIME_SUPPRESSION_GRACE_SEC", 60.0):
                with mock.patch.object(rt, "_cancel_order", return_value=True) as cancel_mock:
                    rt._apply_tier2_suppression(now=1030.0)
                    self.assertIsNotNone(sm.find_order(rt.slots[0].state, 1))
                    cancel_mock.assert_not_called()

                    rt._apply_tier2_suppression(now=1061.0)

        cancel_mock.assert_called_once_with("TX-A-ENTRY")
        self.assertIsNone(sm.find_order(rt.slots[0].state, 1))
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertEqual(rt.slots[0].state.mode_source, "regime")

    def test_bootstrap_only_places_favored_side_during_tier2(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt._regime_tier = 2
        rt._regime_side_suppressed = "A"
        rt._regime_tier2_grace_start = 900.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        with mock.patch.object(rt, "_safe_balance", return_value={"ZUSD": "50.0", "XXDG": "1000.0"}):
            with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
                with mock.patch.object(config, "REGIME_SUPPRESSION_GRACE_SEC", 60.0):
                    with mock.patch.object(rt, "_execute_actions") as exec_actions:
                        rt._ensure_slot_bootstrapped(0)

        entries = [o for o in rt.slots[0].state.orders if o.role == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].side, "buy")
        self.assertTrue(rt.slots[0].state.long_only)
        self.assertFalse(rt.slots[0].state.short_only)
        self.assertEqual(rt.slots[0].state.mode_source, "regime")
        exec_actions.assert_called_once()
        self.assertEqual(exec_actions.call_args.args[1][0].side, "buy")

    def test_tier_downgrade_clears_mode_source_regime(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_tier_entered_at = 900.0
        rt._regime_tier2_grace_start = 900.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    long_only=True,
                    short_only=False,
                    mode_source="regime",
                ),
            ),
            1: bot.SlotRuntime(
                slot_id=1,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    long_only=False,
                    short_only=True,
                    mode_source="regime",
                ),
            ),
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.0,
            "bias_signal": 0.0,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
                    with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                        with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 0.0):
                            with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 0.0):
                                rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 0)
        self.assertEqual(rt._regime_tier2_grace_start, 0.0)
        self.assertEqual(rt.slots[0].state.mode_source, "none")
        self.assertEqual(rt.slots[1].state.mode_source, "none")

    def test_tier2_reentry_cooldown_blocks_repromotion(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_tier_entered_at = 900.0
        rt._regime_tier2_grace_start = 900.0
        rt._regime_side_suppressed = "A"
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.0,
            "bias_signal": 0.0,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                        with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 0.0):
                            with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
                                with mock.patch("bot.supabase_store.save_regime_tier_transition"):
                                    rt._update_regime_tier(now=1000.0)
                                    self.assertEqual(rt._regime_tier, 0)
                                    self.assertAlmostEqual(rt._regime_tier2_last_downgrade_at, 1000.0)

                                    rt._hmm_state.update({
                                        "regime": "BULLISH",
                                        "confidence": 0.95,
                                        "bias_signal": 0.80,
                                        "last_update_ts": 1030.0,
                                    })
                                    rt._update_regime_tier(now=1030.0)
                                    self.assertIn(rt._regime_tier, (0, 1))

                                    rt._hmm_state["last_update_ts"] = 1701.0
                                    rt._update_regime_tier(now=1701.0)

        self.assertEqual(rt._regime_tier, 2)

    def test_cooldown_preserves_mode_source_regime(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_tier_entered_at = 900.0
        rt._regime_tier2_grace_start = 900.0
        rt._regime_side_suppressed = "A"
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    long_only=True,
                    short_only=False,
                    mode_source="regime",
                ),
            )
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.0,
            "bias_signal": 0.0,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
                    with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                        with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 0.0):
                            with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
                                with mock.patch("bot.supabase_store.save_regime_tier_transition"):
                                    rt._update_regime_tier(now=1000.0)

                                self.assertEqual(rt._regime_tier, 0)
                                self.assertEqual(rt.slots[0].state.mode_source, "regime")
                                self.assertEqual(rt._regime_cooldown_suppressed_side, "A")

                                rt._clear_expired_regime_cooldown(now=1701.0)

        self.assertEqual(rt.slots[0].state.mode_source, "none")
        self.assertEqual(rt._regime_tier2_last_downgrade_at, 0.0)
        self.assertIsNone(rt._regime_cooldown_suppressed_side)

    def test_cooldown_resets_metadata_on_expiry(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 0
        rt._regime_tier2_last_downgrade_at = 100.0
        rt._regime_cooldown_suppressed_side = "B"

        with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
            rt._clear_expired_regime_cooldown(now=1000.0)

        self.assertEqual(rt._regime_tier2_last_downgrade_at, 0.0)
        self.assertIsNone(rt._regime_cooldown_suppressed_side)

    def test_bootstrap_respects_cooldown_suppression(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt._regime_tier = 0
        rt._regime_tier2_last_downgrade_at = bot._now() - 10.0
        rt._regime_cooldown_suppressed_side = "B"
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        with mock.patch.object(rt, "_safe_balance", return_value={"ZUSD": "50.0", "XXDG": "1000.0"}):
            with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
                with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
                    with mock.patch.object(rt, "_execute_actions") as exec_actions:
                        rt._ensure_slot_bootstrapped(0)

        entries = [o for o in rt.slots[0].state.orders if o.role == "entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].side, "sell")
        self.assertTrue(rt.slots[0].state.short_only)
        self.assertFalse(rt.slots[0].state.long_only)
        self.assertEqual(rt.slots[0].state.mode_source, "regime")
        exec_actions.assert_called_once()
        self.assertEqual(exec_actions.call_args.args[1][0].side, "sell")

    def test_tier_history_buffer_max_20(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.95,
            "bias_signal": 0.80,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_EVAL_INTERVAL_SEC", 1.0):
                        with mock.patch.object(config, "REGIME_MIN_DWELL_SEC", 0.0):
                            with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 0.0):
                                with mock.patch("bot.supabase_store.save_regime_tier_transition"):
                                    for i in range(25):
                                        if i % 2 == 0:
                                            rt._hmm_state.update({
                                                "regime": "BULLISH",
                                                "confidence": 0.95,
                                                "bias_signal": 0.80,
                                                "last_update_ts": 1000.0 + i,
                                            })
                                        else:
                                            rt._hmm_state.update({
                                                "regime": "RANGING",
                                                "confidence": 0.0,
                                                "bias_signal": 0.0,
                                                "last_update_ts": 1000.0 + i,
                                            })
                                        rt._update_regime_tier(now=1000.0 + i)

        self.assertEqual(len(rt._regime_tier_history), 20)
        self.assertAlmostEqual(float(rt._regime_tier_history[0]["time"]), 1005.0)
        self.assertAlmostEqual(float(rt._regime_tier_history[-1]["time"]), 1024.0)

    def test_regime_status_payload_includes_cooldown_and_history(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 0
        rt._regime_tier_entered_at = 900.0
        rt._regime_tier2_last_downgrade_at = 1000.0
        rt._regime_cooldown_suppressed_side = "B"
        rt._regime_tier_history = [{
            "time": 990.0,
            "from_tier": 2,
            "to_tier": 0,
            "regime": "RANGING",
            "confidence": 0.0,
            "bias": 0.0,
            "reason": "hmm_eval",
        }]

        with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
            payload = rt._regime_status_payload(now=1200.0)

        self.assertIn("cooldown_remaining_sec", payload)
        self.assertIn("cooldown_suppressed_side", payload)
        self.assertIn("tier_history", payload)
        self.assertAlmostEqual(float(payload["cooldown_remaining_sec"]), 400.0)
        self.assertEqual(payload["cooldown_suppressed_side"], "B")
        self.assertEqual(len(payload["tier_history"]), 1)

    def test_regime_flip_clears_old_suppression(self):
        rt = bot.BotRuntime()
        rt._regime_tier = 2
        rt._regime_side_suppressed = "A"
        rt._regime_tier2_grace_start = 900.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    short_only=True,
                    mode_source="regime",
                ),
            ),
            1: bot.SlotRuntime(
                slot_id=1,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=0.1002,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-ENTRY",
                            placed_at=999.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    next_order_id=3,
                ),
            ),
        }

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
            with mock.patch.object(config, "REGIME_SUPPRESSION_GRACE_SEC", 60.0):
                with mock.patch.object(rt, "_cancel_order", return_value=True) as cancel_mock:
                    rt._apply_tier2_suppression(now=1000.0)

        self.assertEqual(rt.slots[0].state.mode_source, "none")
        self.assertTrue(rt.slots[0].state.short_only)
        self.assertIsNone(sm.find_order(rt.slots[1].state, 1))
        self.assertTrue(rt.slots[1].state.long_only)
        self.assertEqual(rt.slots[1].state.mode_source, "regime")
        cancel_mock.assert_called_once_with("TX-A-ENTRY")

    def test_execute_actions_books_exit_outcome_row(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        cycle = sm.CycleRecord(
            trade_id="A",
            cycle=3,
            entry_price=0.101,
            exit_price=0.099,
            volume=13.0,
            gross_profit=0.026,
            fees=0.002,
            net_profit=0.024,
            entry_time=900.0,
            exit_time=1000.0,
            from_recovery=False,
        )
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    completed_cycles=(cycle,),
                ),
            )
        }
        rt._hmm_state.update({
            "regime": "BEARISH",
            "confidence": 0.60,
            "bias_signal": -0.40,
        })
        rt._regime_tier = 1

        action = sm.BookCycleAction(
            trade_id="A",
            cycle=3,
            net_profit=0.024,
            gross_profit=0.026,
            fees=0.002,
            from_recovery=False,
        )

        with mock.patch("notifier._send_message"):
            with mock.patch("supabase_store.save_exit_outcome") as save_outcome:
                rt._execute_actions(0, [action], "unit_test")

        save_outcome.assert_called_once()
        row = save_outcome.call_args.args[0]
        self.assertEqual(row["pair"], rt.pair)
        self.assertEqual(row["trade"], "A")
        self.assertEqual(row["cycle"], 3)
        self.assertEqual(row["resolution"], "normal")
        self.assertEqual(row["regime_at_entry"], "BEARISH")
        self.assertEqual(row["regime_tier"], 1)
        self.assertEqual(row["against_trend"], False)
        self.assertAlmostEqual(row["total_age_sec"], 100.0)

    def test_dynamic_idle_target_blends_with_hmm_bias_when_enabled(self):
        def _mk_runtime() -> bot.BotRuntime:
            rt = bot.BotRuntime()
            rt.last_price = 0.102
            rt.price_history = []
            rt._trend_fast_ema = 0.102
            rt._trend_slow_ema = 0.100
            rt._trend_last_update_ts = 1000.0
            rt._trend_dynamic_target = 0.40
            rt._trend_smoothed_target = 0.40
            rt._trend_target_locked_until = 0.0
            return rt

        now = 1300.0
        base_rt = _mk_runtime()
        hmm_rt = _mk_runtime()
        hmm_rt._hmm_state.update({
            "available": True,
            "trained": True,
            "bias_signal": -1.0,
            "blend_factor": 0.0,
        })

        with mock.patch.object(config, "HMM_ENABLED", False):
            base_target = base_rt._compute_dynamic_idle_target(now)

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "HMM_BLEND_WITH_TREND", 0.0):
                hmm_target = hmm_rt._compute_dynamic_idle_target(now)

        self.assertGreater(hmm_target, base_target)
        self.assertLessEqual(hmm_target, float(config.TREND_IDLE_CEILING))

    def test_dynamic_idle_target_cold_start_uses_base_target(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.price_history = [(float(i), 0.1) for i in range(5)]
        now = 2000.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_MIN_SAMPLES", 24):
                target = rt._compute_dynamic_idle_target(now)

        self.assertAlmostEqual(target, 0.40)
        self.assertAlmostEqual(rt._trend_score, 0.0)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.40)

    def test_dynamic_idle_target_restart_uses_persisted_emas(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.102
        rt.price_history = []
        rt._trend_fast_ema = 0.102
        rt._trend_slow_ema = 0.100
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            target = rt._compute_dynamic_idle_target(now)

        self.assertGreater(rt._trend_score, 0.0)
        self.assertLess(target, 0.40)
        self.assertLess(rt._trend_slow_ema, rt.last_price)

    def test_dynamic_idle_target_resets_after_long_data_gap(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.12
        rt.price_history = [(float(i), 0.12) for i in range(50)]
        rt._trend_fast_ema = 0.20
        rt._trend_slow_ema = 0.10
        rt._trend_last_update_ts = 1000.0
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_FAST_HALFLIFE", 100.0):
                with mock.patch.object(config, "TREND_SLOW_HALFLIFE", 100.0):
                    target = rt._compute_dynamic_idle_target(now)

        self.assertAlmostEqual(target, 0.40)
        self.assertAlmostEqual(rt._trend_fast_ema, 0.12)
        self.assertAlmostEqual(rt._trend_slow_ema, 0.12)
        self.assertAlmostEqual(rt._trend_score, 0.0)

    def test_dynamic_idle_target_positive_trend_hits_floor_bound(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.20
        rt.price_history = [(float(i), 0.20) for i in range(50)]
        rt._trend_fast_ema = 0.20
        rt._trend_slow_ema = 0.10
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_IDLE_SENSITIVITY", 5.0):
                with mock.patch.object(config, "TREND_IDLE_FLOOR", 0.15):
                    with mock.patch.object(config, "TREND_IDLE_CEILING", 0.60):
                        with mock.patch.object(config, "TREND_HYSTERESIS_SMOOTH_HALFLIFE", 1.0):
                            target = rt._compute_dynamic_idle_target(now)

        self.assertGreater(rt._trend_score, 0.0)
        self.assertAlmostEqual(target, 0.15, places=4)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.15, places=4)

    def test_dynamic_idle_target_negative_trend_hits_ceiling_bound(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.10
        rt.price_history = [(float(i), 0.10) for i in range(50)]
        rt._trend_fast_ema = 0.10
        rt._trend_slow_ema = 0.20
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_IDLE_SENSITIVITY", 5.0):
                with mock.patch.object(config, "TREND_IDLE_FLOOR", 0.15):
                    with mock.patch.object(config, "TREND_IDLE_CEILING", 0.60):
                        with mock.patch.object(config, "TREND_HYSTERESIS_SMOOTH_HALFLIFE", 1.0):
                            target = rt._compute_dynamic_idle_target(now)

        self.assertLess(rt._trend_score, 0.0)
        self.assertAlmostEqual(target, 0.60, places=4)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.60, places=4)

    def test_dynamic_idle_target_dead_zone_returns_base_target(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.10005
        rt.price_history = [(float(i), 0.10005) for i in range(50)]
        rt._trend_fast_ema = 0.10005
        rt._trend_slow_ema = 0.10000
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.37
        rt._trend_smoothed_target = 0.37
        rt._trend_target_locked_until = 9999.0
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_DEAD_ZONE", 0.001):
                target = rt._compute_dynamic_idle_target(now)

        self.assertAlmostEqual(target, 0.40, places=6)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.40, places=6)
        self.assertAlmostEqual(rt._trend_smoothed_target, 0.40, places=6)
        self.assertEqual(rt._trend_target_locked_until, 0.0)

    def test_dynamic_idle_target_hysteresis_hold_freezes_output_and_smoothing(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.20
        rt.price_history = [(float(i), 0.20) for i in range(50)]
        rt._trend_fast_ema = 0.20
        rt._trend_slow_ema = 0.10
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.33
        rt._trend_smoothed_target = 0.33
        rt._trend_target_locked_until = 2000.0
        now = 1500.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_HYSTERESIS_SMOOTH_HALFLIFE", 1.0):
                target = rt._compute_dynamic_idle_target(now)

        self.assertAlmostEqual(target, 0.33, places=6)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.33, places=6)
        self.assertAlmostEqual(rt._trend_smoothed_target, 0.33, places=6)
        self.assertAlmostEqual(rt._trend_target_locked_until, 2000.0, places=6)

    def test_dynamic_idle_target_hysteresis_hold_triggers_on_large_jump(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.20
        rt.price_history = [(float(i), 0.20) for i in range(50)]
        rt._trend_fast_ema = 0.20
        rt._trend_slow_ema = 0.10
        rt._trend_last_update_ts = 1000.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        now = 1300.0

        with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
            with mock.patch.object(config, "TREND_HYSTERESIS_SEC", 600.0):
                with mock.patch.object(config, "TREND_HYSTERESIS_SMOOTH_HALFLIFE", 1.0):
                    target = rt._compute_dynamic_idle_target(now)

        self.assertLess(target, 0.38)
        self.assertAlmostEqual(rt._trend_target_locked_until, 1900.0, places=6)

    def test_dynamic_idle_target_fast_ema_tracks_price_more_closely_than_slow(self):
        rt = bot.BotRuntime()
        rt._trend_fast_ema = 1.0
        rt._trend_slow_ema = 1.0
        rt._trend_last_update_ts = 0.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        rt.price_history = [(float(i), 1.0) for i in range(50)]
        now = 1000.0

        with mock.patch.object(config, "TREND_FAST_HALFLIFE", 30.0):
            with mock.patch.object(config, "TREND_SLOW_HALFLIFE", 300.0):
                with mock.patch.object(config, "TREND_DEAD_ZONE", 0.0):
                    for px in [1.00, 1.05, 1.10, 1.20, 1.30]:
                        rt.last_price = px
                        now += 60.0
                        rt._compute_dynamic_idle_target(now)

        current = 1.30
        self.assertLess(abs(rt._trend_fast_ema - current), abs(rt._trend_slow_ema - current))

    def test_status_payload_exposes_dynamic_rebalancer_target_and_trend_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._trend_score = 0.0123
        rt._trend_fast_ema = 0.10541
        rt._trend_slow_ema = 0.10412
        rt._trend_dynamic_target = 0.32
        rt._trend_target_locked_until = 1050.0
        rt._rebalancer_current_skew = 0.10

        with mock.patch("bot._now", return_value=1000.0):
            with mock.patch.object(config, "REBALANCE_TARGET_IDLE_PCT", 0.40):
                payload = rt.status_payload()

        self.assertAlmostEqual(payload["rebalancer"]["target"], 0.32)
        self.assertAlmostEqual(payload["rebalancer"]["base_target"], 0.40)
        trend = payload["trend"]
        self.assertAlmostEqual(trend["score"], 0.0123)
        self.assertEqual(trend["score_display"], "+1.23%")
        self.assertAlmostEqual(trend["dynamic_idle_target"], 0.32)
        self.assertTrue(trend["hysteresis_active"])
        self.assertEqual(trend["hysteresis_expires_in_sec"], 50)

    def test_global_snapshot_persists_trend_fields(self):
        rt = bot.BotRuntime()
        rt._trend_fast_ema = 0.101
        rt._trend_slow_ema = 0.100
        rt._trend_score = 0.01
        rt._trend_dynamic_target = 0.35
        rt._trend_smoothed_target = 0.34
        rt._trend_target_locked_until = 2222.0
        rt._trend_last_update_ts = 1234.0

        snap = rt._global_snapshot()

        self.assertAlmostEqual(snap["trend_fast_ema"], 0.101)
        self.assertAlmostEqual(snap["trend_slow_ema"], 0.100)
        self.assertAlmostEqual(snap["trend_score"], 0.01)
        self.assertAlmostEqual(snap["trend_dynamic_target"], 0.35)
        self.assertAlmostEqual(snap["trend_smoothed_target"], 0.34)
        self.assertAlmostEqual(snap["trend_target_locked_until"], 2222.0)
        self.assertAlmostEqual(snap["trend_last_update_ts"], 1234.0)

    def test_load_snapshot_backward_compatible_without_trend_fields(self):
        rt = bot.BotRuntime()
        old_snap = rt._global_snapshot()
        old_snap.pop("trend_fast_ema", None)
        old_snap.pop("trend_slow_ema", None)
        old_snap.pop("trend_score", None)
        old_snap.pop("trend_dynamic_target", None)
        old_snap.pop("trend_smoothed_target", None)
        old_snap.pop("trend_target_locked_until", None)
        old_snap.pop("trend_last_update_ts", None)

        rt._trend_fast_ema = 0.0
        rt._trend_slow_ema = 0.0
        rt._trend_score = 0.0
        rt._trend_dynamic_target = 0.40
        rt._trend_smoothed_target = 0.40
        rt._trend_target_locked_until = 0.0
        rt._trend_last_update_ts = 0.0

        with mock.patch("supabase_store.load_state", return_value=old_snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                rt._load_snapshot()

        self.assertAlmostEqual(rt._trend_dynamic_target, 0.40)
        self.assertAlmostEqual(rt._trend_smoothed_target, 0.40)
        self.assertAlmostEqual(rt._trend_score, 0.0)

    def test_load_snapshot_restores_trend_fields(self):
        rt = bot.BotRuntime()
        snap = rt._global_snapshot()
        snap["trend_fast_ema"] = 0.106
        snap["trend_slow_ema"] = 0.104
        snap["trend_score"] = 0.0192307692
        snap["trend_dynamic_target"] = 0.31
        snap["trend_smoothed_target"] = 0.315
        snap["trend_target_locked_until"] = 9999.0
        snap["trend_last_update_ts"] = 7777.0

        with mock.patch("supabase_store.load_state", return_value=snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                rt._load_snapshot()

        self.assertAlmostEqual(rt._trend_fast_ema, 0.106)
        self.assertAlmostEqual(rt._trend_slow_ema, 0.104)
        self.assertAlmostEqual(rt._trend_score, 0.0192307692)
        self.assertAlmostEqual(rt._trend_dynamic_target, 0.31)
        self.assertAlmostEqual(rt._trend_smoothed_target, 0.315)
        self.assertAlmostEqual(rt._trend_target_locked_until, 9999.0)
        self.assertAlmostEqual(rt._trend_last_update_ts, 7777.0)

    def test_daily_loss_lock_pauses_on_aggregate_utc_threshold(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        day_ts = 1704068400.0  # 2024-01-01 00:20:00 UTC
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=day_ts,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="A",
                            cycle=1,
                            entry_price=0.1,
                            exit_price=0.099,
                            volume=13.0,
                            gross_profit=-1.6,
                            fees=0.1,
                            net_profit=-1.7,
                            entry_time=day_ts - 1200.0,
                            exit_time=day_ts - 600.0,
                        ),
                    ),
                ),
            ),
            1: bot.SlotRuntime(
                slot_id=1,
                state=sm.PairState(
                    market_price=0.1,
                    now=day_ts,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="B",
                            cycle=1,
                            entry_price=0.1,
                            exit_price=0.101,
                            volume=13.0,
                            gross_profit=-1.3,
                            fees=0.1,
                            net_profit=-1.4,
                            entry_time=day_ts - 1100.0,
                            exit_time=day_ts - 500.0,
                        ),
                    ),
                ),
            ),
        }

        with mock.patch.object(config, "DAILY_LOSS_LIMIT", 3.0):
            daily_loss = rt._update_daily_loss_lock(day_ts)

        self.assertAlmostEqual(daily_loss, 3.1, places=8)
        self.assertTrue(rt._daily_loss_lock_active)
        self.assertEqual(rt._daily_loss_lock_utc_day, "2024-01-01")
        self.assertEqual(rt.mode, "PAUSED")
        self.assertIn("daily loss limit hit", rt.pause_reason)

    def test_daily_loss_lock_counts_eviction_booked_loss(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        now_ts = 1704068400.0  # 2024-01-01 UTC
        st = sm.PairState(
            market_price=101.0,
            now=now_ts,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=100.0,
                    volume=13.0,
                    trade_id="A",
                    cycle=9,
                    txid="TX-A-EXIT",
                    entry_price=101.5,
                    entry_fee=0.02,
                    entry_filled_at=now_ts - 3600.0,
                ),
                sm.OrderState(
                    local_id=2,
                    side="buy",
                    role="entry",
                    price=99.5,
                    volume=13.0,
                    trade_id="B",
                    cycle=5,
                    txid="TX-B-ENTRY",
                    placed_at=now_ts - 1800.0,
                ),
            ),
            recovery_orders=(
                sm.RecoveryOrder(
                    recovery_id=1,
                    side="sell",
                    price=101.0,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    entry_price=105.0,
                    entry_fee=0.03,
                    entry_filled_at=now_ts - 7200.0,
                    orphaned_at=now_ts - 7000.0,
                    txid="TX-OLD-1",
                    reason="s1_timeout",
                ),
                sm.RecoveryOrder(
                    recovery_id=2,
                    side="sell",
                    price=102.0,
                    volume=13.0,
                    trade_id="B",
                    cycle=2,
                    entry_price=106.0,
                    entry_fee=0.02,
                    entry_filled_at=now_ts - 6800.0,
                    orphaned_at=now_ts - 6500.0,
                    txid="TX-OLD-2",
                    reason="s1_timeout",
                ),
            ),
            cycle_a=9,
            cycle_b=5,
            next_order_id=3,
            next_recovery_id=3,
        )
        rt.slots = {0: bot.SlotRuntime(slot_id=0, state=st)}
        with mock.patch.object(rt, "_cancel_order"):
            with mock.patch.object(rt, "_place_order", return_value="TX-NEW"):
                rt._apply_event(0, sm.TimerTick(timestamp=now_ts), "timer_tick", {})

        self.assertGreater(rt.slots[0].state.today_realized_loss, 0.0)
        with mock.patch.object(config, "DAILY_LOSS_LIMIT", 0.5):
            daily_loss = rt._update_daily_loss_lock(now_ts)
        self.assertGreaterEqual(daily_loss, 0.5)
        self.assertTrue(rt._daily_loss_lock_active)

    def test_resume_is_blocked_while_daily_loss_lock_active(self):
        rt = bot.BotRuntime()
        rt.mode = "PAUSED"
        rt.pause_reason = "daily loss limit hit: $3.1000 >= $3.0000 (UTC 2024-01-01)"
        rt._daily_loss_lock_active = True
        rt._daily_loss_lock_utc_day = "2024-01-01"
        day_ts = 1704072000.0  # 2024-01-01 UTC
        # Need actual losses exceeding the limit so the lock stays active.
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1, now=day_ts,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="A", cycle=1,
                            entry_price=0.1, exit_price=0.099, volume=13.0,
                            gross_profit=-1.6, fees=0.1, net_profit=-1.7,
                            entry_time=day_ts - 1200.0, exit_time=day_ts - 600.0,
                        ),
                        sm.CycleRecord(
                            trade_id="B", cycle=1,
                            entry_price=0.1, exit_price=0.101, volume=13.0,
                            gross_profit=-1.3, fees=0.1, net_profit=-1.4,
                            entry_time=day_ts - 1100.0, exit_time=day_ts - 500.0,
                        ),
                    ),
                ),
            ),
        }

        with mock.patch.object(config, "DAILY_LOSS_LIMIT", 3.0):
            with mock.patch("bot._now", return_value=day_ts):
                ok, msg = rt.resume()

        self.assertFalse(ok)
        self.assertIn("daily loss lock active", msg)
        self.assertEqual(rt.mode, "PAUSED")

    def test_daily_loss_lock_clears_when_limit_raised_above_loss(self):
        """Lock clears on same UTC day when DAILY_LOSS_LIMIT is raised above current loss."""
        rt = bot.BotRuntime()
        rt.mode = "PAUSED"
        rt.pause_reason = "daily loss limit hit: $3.1000 >= $3.0000 (UTC 2024-01-01)"
        rt._daily_loss_lock_active = True
        rt._daily_loss_lock_utc_day = "2024-01-01"
        day_ts = 1704072000.0  # 2024-01-01 UTC
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1, now=day_ts,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="A", cycle=1,
                            entry_price=0.1, exit_price=0.099, volume=13.0,
                            gross_profit=-1.6, fees=0.1, net_profit=-1.7,
                            entry_time=day_ts - 1200.0, exit_time=day_ts - 600.0,
                        ),
                        sm.CycleRecord(
                            trade_id="B", cycle=1,
                            entry_price=0.1, exit_price=0.101, volume=13.0,
                            gross_profit=-1.3, fees=0.1, net_profit=-1.4,
                            entry_time=day_ts - 1100.0, exit_time=day_ts - 500.0,
                        ),
                    ),
                ),
            ),
        }

        # Loss is 3.1, raise limit to 25 — lock should clear, resume should work.
        with mock.patch.object(config, "DAILY_LOSS_LIMIT", 25.0):
            with mock.patch("bot._now", return_value=day_ts):
                ok, msg = rt.resume()

        self.assertTrue(ok)
        self.assertFalse(rt._daily_loss_lock_active)
        self.assertEqual(rt.mode, "RUNNING")

    def test_daily_loss_lock_clears_on_utc_rollover_then_manual_resume_works(self):
        rt = bot.BotRuntime()
        rt.mode = "PAUSED"
        rt.pause_reason = "daily loss limit hit: $3.1000 >= $3.0000 (UTC 2024-01-01)"
        rt._daily_loss_lock_active = True
        rt._daily_loss_lock_utc_day = "2024-01-01"
        rt.slots = {}

        with mock.patch.object(config, "DAILY_LOSS_LIMIT", 3.0):
            rt._update_daily_loss_lock(1704153900.0)  # 2024-01-02 00:05:00 UTC
            self.assertFalse(rt._daily_loss_lock_active)
            self.assertIn("cleared at UTC rollover", rt.pause_reason)

            with mock.patch("bot._now", return_value=1704153900.0):
                ok, msg = rt.resume()

        self.assertTrue(ok)
        self.assertEqual(msg, "running")
        self.assertEqual(rt.mode, "RUNNING")

    def test_status_payload_exposes_daily_loss_lock_fields(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._daily_loss_lock_active = True
        rt._daily_loss_lock_utc_day = "2024-01-01"
        rt._daily_realized_loss_utc = 3.2
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        payload = rt.status_payload()
        self.assertIn("daily_loss_limit", payload)
        self.assertIn("daily_realized_loss_utc", payload)
        self.assertIn("daily_loss_lock_active", payload)
        self.assertIn("daily_loss_lock_utc_day", payload)

    def test_status_payload_exposes_capital_layers_and_slot_alias(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                alias="wow",
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        payload = rt.status_payload()
        self.assertEqual(payload["slots"][0]["slot_alias"], "wow")
        self.assertEqual(payload["slots"][0]["slot_label"], "wow")
        layers = payload["capital_layers"]
        self.assertIn("target_layers", layers)
        self.assertIn("effective_layers", layers)
        self.assertIn("layer_step_doge_eq", layers)

    def test_auto_drain_recovery_backlog_prefers_furthest_then_oldest(self):
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 100.0
        rt._kraken_open_orders_current = 0
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=100.0,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="entry",
                            price=100.2,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-ENTRY",
                            placed_at=999.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="entry",
                            price=99.8,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-ENTRY",
                            placed_at=999.0,
                        ),
                    ),
                    recovery_orders=(
                        sm.RecoveryOrder(
                            recovery_id=1,
                            side="sell",
                            price=103.0,
                            volume=13.0,
                            trade_id="B",
                            cycle=11,
                            entry_price=106.0,
                            entry_fee=0.02,
                            entry_filled_at=600.0,
                            orphaned_at=500.0,
                            txid="TX-REC-1",
                            reason="s1_timeout",
                        ),
                        sm.RecoveryOrder(
                            recovery_id=2,
                            side="sell",
                            price=110.0,
                            volume=13.0,
                            trade_id="B",
                            cycle=12,
                            entry_price=107.0,
                            entry_fee=0.03,
                            entry_filled_at=700.0,
                            orphaned_at=700.0,
                            txid="TX-REC-2",
                            reason="s1_timeout",
                        ),
                    ),
                ),
            )
        }

        with mock.patch.object(config, "AUTO_RECOVERY_DRAIN_ENABLED", True):
            with mock.patch.object(config, "AUTO_RECOVERY_DRAIN_MAX_PER_LOOP", 1):
                with mock.patch.object(config, "AUTO_RECOVERY_DRAIN_CAPACITY_PCT", 95.0):
                    with mock.patch.object(config, "MAX_RECOVERY_SLOTS", 1):
                        with mock.patch.object(rt, "_cancel_order", return_value=True) as cancel_mock:
                            rt._auto_drain_recovery_backlog()

        cancel_mock.assert_called_once_with("TX-REC-2")
        remaining = {r.recovery_id for r in rt.slots[0].state.recovery_orders}
        self.assertIn(1, remaining)
        self.assertNotIn(2, remaining)
        self.assertEqual(rt._auto_recovery_drain_total, 1)
        self.assertEqual(len(rt.slots[0].state.completed_cycles), 1)
        self.assertTrue(rt.slots[0].state.completed_cycles[0].from_recovery)
        self.assertGreater(rt.slots[0].state.today_realized_loss, 0.0)

    def test_add_layer_auto_accepts_mixed_doge_and_usd(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._last_balance_snapshot = {"USD": 10.0, "DOGE": 200.0}
        rt._last_balance_ts = 1000.0

        ok, msg = rt.add_layer("AUTO")
        self.assertTrue(ok)
        self.assertIn("layer added", msg)
        self.assertEqual(rt.target_layers, 1)
        self.assertEqual(rt.layer_last_add_event["source"], "AUTO")

    def test_remove_layer_rejects_zero_target(self):
        rt = bot.BotRuntime()
        ok, msg = rt.remove_layer()
        self.assertFalse(ok)
        self.assertIn("target already zero", msg)

    def test_recycled_alias_not_reused_until_unused_pool_exhausted(self):
        rt = bot.BotRuntime()
        rt.slot_alias_pool = ("wow", "such", "much")
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                alias="wow",
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt.next_slot_id = 1

        with mock.patch.object(rt, "_save_snapshot"):
            ok, _msg = rt.remove_slot(0)
            self.assertTrue(ok)
            with mock.patch.object(rt, "_ensure_slot_bootstrapped"):
                ok2, _msg2 = rt.add_slot()
                self.assertTrue(ok2)

        # "such" is still unused, so it must be picked before recycled "wow".
        self.assertEqual(rt.slots[1].alias, "such")

    def test_audit_pnl_ok_when_cycle_totals_match(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="A",
                            cycle=1,
                            entry_price=0.101,
                            exit_price=0.099,
                            volume=13.0,
                            gross_profit=0.026,
                            fees=0.001,
                            net_profit=0.025,
                            entry_time=900.0,
                            exit_time=950.0,
                        ),
                    ),
                    total_profit=0.025,
                    today_realized_loss=0.0,
                    total_round_trips=1,
                ),
            )
        }

        ok, msg = rt.audit_pnl()
        self.assertTrue(ok)
        self.assertIn("pnl audit OK", msg)

    def test_audit_pnl_flags_drift(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    completed_cycles=(
                        sm.CycleRecord(
                            trade_id="B",
                            cycle=2,
                            entry_price=0.099,
                            exit_price=0.101,
                            volume=13.0,
                            gross_profit=0.026,
                            fees=0.001,
                            net_profit=0.025,
                            entry_time=900.0,
                            exit_time=950.0,
                        ),
                    ),
                    total_profit=0.05,
                    today_realized_loss=0.01,
                    total_round_trips=2,
                ),
            )
        }

        ok, msg = rt.audit_pnl()
        self.assertFalse(ok)
        self.assertIn("pnl audit mismatch", msg)
        self.assertIn("slots=0(", msg)

    def test_partial_fill_open_is_counted_once_per_txid(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX1",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }
        row = {"status": "open", "vol_exec": "2", "vol": "13"}
        with mock.patch.object(rt, "_query_orders_batched", return_value={"TX1": row}):
            rt._poll_order_status()
            rt._poll_order_status()
        self.assertEqual(len(rt._partial_fill_open_events), 1)

    def test_partial_fill_cancel_canary_is_counted(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX1",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }
        row = {"status": "canceled", "vol_exec": "1", "vol": "13"}
        with mock.patch.object(rt, "_query_orders_batched", return_value={"TX1": row}):
            rt._poll_order_status()
        self.assertEqual(len(rt._partial_fill_cancel_events), 1)
        self.assertEqual(len(rt.slots[0].state.orders), 0)

    def test_legacy_manual_recovery_actions_disabled_in_sticky_mode(self):
        rt = bot.BotRuntime()
        with mock.patch.object(config, "STICKY_MODE_ENABLED", True):
            ok_close, msg_close = rt.soft_close(1, 1)
            ok_next, msg_next = rt.soft_close_next()
            ok_stale, msg_stale = rt.cancel_stale_recoveries()
        self.assertFalse(ok_close)
        self.assertIn("disabled in sticky mode", msg_close)
        self.assertFalse(ok_next)
        self.assertIn("disabled in sticky mode", msg_next)
        self.assertFalse(ok_stale)
        self.assertIn("disabled in sticky mode", msg_stale)

    def test_release_oldest_eligible_selects_oldest_gate_passing_exit(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="sell",
                            role="exit",
                            price=0.11,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            txid="TX-A-EXIT",
                            entry_price=0.1,
                            entry_filled_at=200.0,
                        ),
                        sm.OrderState(
                            local_id=2,
                            side="buy",
                            role="exit",
                            price=0.09,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B-EXIT",
                            entry_price=0.1,
                            entry_filled_at=300.0,
                        ),
                    ),
                ),
            ),
        }

        failing = {"age_ok": False, "distance_ok": True, "regime_ok": True}
        passing = {"age_ok": True, "distance_ok": True, "regime_ok": True}
        with mock.patch.object(rt, "_update_release_recon_gate_locked", return_value=(True, "ok")):
            with mock.patch.object(rt, "_release_gate_flags", side_effect=[failing, passing]):
                with mock.patch.object(rt, "_release_exit_locked", return_value=(True, "released")) as release_mock:
                    ok, msg = rt.release_oldest_eligible(0)

        self.assertTrue(ok)
        self.assertEqual(msg, "released")
        self.assertEqual(release_mock.call_count, 1)
        called_order = release_mock.call_args.args[1]
        self.assertEqual(called_order.local_id, 2)


class DashboardApiHardeningTests(unittest.TestCase):
    class _LockStub:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _RuntimeStub:
        def __init__(self):
            self.lock = DashboardApiHardeningTests._LockStub()
            self.raise_on_pause = False
            self.resume_result = (True, "running")
            self.release_slot_calls = []

        def pause(self, _reason):
            if self.raise_on_pause:
                raise RuntimeError("boom")

        def resume(self):
            return self.resume_result

        def add_slot(self):
            return True, "ok"

        def add_layer(self, _source):
            return True, "layer ok"

        def remove_layer(self):
            return True, "layer removed"

        def set_entry_pct(self, _value):
            return True, "ok"

        def set_profit_pct(self, _value):
            return True, "ok"

        def soft_close(self, _slot_id, _recovery_id):
            return True, "ok"

        def release_slot(self, slot_id, local_id=None, trade_id=None):
            self.release_slot_calls.append((slot_id, local_id, trade_id))
            return True, "released"

        def release_oldest_eligible(self, slot_id):
            self.release_slot_calls.append((slot_id, "eligible", None))
            return True, "released eligible"

        def soft_close_next(self):
            return True, "ok"

        def audit_pnl(self):
            return True, "audit ok"

        def _save_snapshot(self):
            return None

    class _HandlerStub:
        def __init__(self, body_or_exc):
            self.path = "/api/action"
            self._body_or_exc = body_or_exc
            self.sent = []

        def _read_json(self):
            if isinstance(self._body_or_exc, Exception):
                raise self._body_or_exc
            return self._body_or_exc

        def _send_json(self, data, code=200):
            self.sent.append((code, data))

    class _GetHandlerStub:
        def __init__(self, path):
            self.path = path
            self.code = None
            self.headers = []
            self.sent_json = []
            self.wfile = io.BytesIO()

        def send_response(self, code):
            self.code = code

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            return None

        def _send_json(self, data, code=200):
            self.sent_json.append((code, data))

    def setUp(self):
        self.prev_runtime = bot._RUNTIME

    def tearDown(self):
        bot._RUNTIME = self.prev_runtime

    def test_api_action_malformed_body_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub(ValueError("bad json"))

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "invalid request body"})

    def test_api_action_unknown_action_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "wat"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "unknown action: wat"})

    def test_api_action_add_layer_invalid_source_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "add_layer", "source": "bad"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "invalid layer source"})

    def test_api_action_catch_all_returns_json_500(self):
        runtime = self._RuntimeStub()
        runtime.raise_on_pause = True
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "pause"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 500)
        self.assertEqual(payload, {"ok": False, "message": "internal server error"})

    def test_api_action_audit_pnl_routes_ok(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "audit_pnl"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "audit ok"})

    def test_api_action_add_layer_routes_ok(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "add_layer", "source": "AUTO"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "layer ok"})

    def test_api_action_release_slot_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "release_slot", "slot_id": 7, "local_id": 4})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.release_slot_calls, [(7, 4, None)])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "released"})

    def test_api_action_release_slot_invalid_trade_id_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "release_slot", "slot_id": 7, "trade_id": "C"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "invalid trade_id (expected A or B)"})

    def test_api_action_release_oldest_eligible_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "release_oldest_eligible", "slot_id": 5})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.release_slot_calls, [(5, "eligible", None)])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "released eligible"})

    def test_api_action_soft_close_rejected_when_sticky_enabled(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "soft_close", "slot_id": 1, "recovery_id": 1})
        with mock.patch.object(config, "STICKY_MODE_ENABLED", True):
            bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "soft_close disabled in sticky mode; use release_slot"})

    def test_api_action_cancel_stale_rejected_when_sticky_enabled(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "cancel_stale_recoveries"})
        with mock.patch.object(config, "STICKY_MODE_ENABLED", True):
            bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(
            payload,
            {"ok": False, "message": "cancel_stale_recoveries disabled in sticky mode; use release_slot"},
        )

    def test_api_action_resume_failure_returns_json_400(self):
        runtime = self._RuntimeStub()
        runtime.resume_result = (False, "daily loss lock active")
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "resume"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "daily loss lock active"})

    def test_api_action_remove_layer_routes_ok(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "remove_layer"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "layer removed"})

    def test_send_json_sets_no_cache_headers(self):
        handler = self._GetHandlerStub("/api/status")
        bot.DashboardHandler._send_json(handler, {"ok": True}, 200)

        self.assertEqual(handler.code, 200)
        self.assertIn(("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"), handler.headers)
        self.assertIn(("Pragma", "no-cache"), handler.headers)
        self.assertIn(("Expires", "0"), handler.headers)

    def test_factory_route_serves_html(self):
        handler = self._GetHandlerStub("/factory")

        bot.DashboardHandler.do_GET(handler)

        self.assertEqual(handler.code, 200)
        self.assertFalse(handler.sent_json)
        body = handler.wfile.getvalue()
        self.assertIn(b"Factory Lens", body)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), handler.headers)


class OpenOrderDriftAlertTests(unittest.TestCase):
    def _runtime_with_one_order(self) -> bot.BotRuntime:
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.last_price_ts = 1000.0
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    orders=(
                        sm.OrderState(
                            local_id=1,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX1",
                            placed_at=999.0,
                        ),
                    ),
                ),
            )
        }
        return rt

    def test_open_order_drift_alert_requires_persistence_and_cooldown(self):
        rt = self._runtime_with_one_order()
        rt._kraken_open_orders_current = 25
        with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10):
            with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC", 300):
                with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC", 900):
                    with mock.patch("notifier._send_message") as send_mock:
                        for ts in (1000.0, 1299.0, 1301.0, 1500.0):
                            rt._kraken_open_orders_ts = ts
                            rt._maybe_alert_persistent_open_order_drift(now=ts)
                        self.assertEqual(send_mock.call_count, 1)

                        rt._kraken_open_orders_ts = 2205.0
                        rt._maybe_alert_persistent_open_order_drift(now=2205.0)
                        self.assertEqual(send_mock.call_count, 2)

    def test_open_order_drift_tracker_resets_after_recovery(self):
        rt = self._runtime_with_one_order()
        with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10):
            with mock.patch("notifier._send_message"):
                rt._kraken_open_orders_current = 25
                rt._kraken_open_orders_ts = 1000.0
                rt._maybe_alert_persistent_open_order_drift(now=1000.0)
                self.assertEqual(rt._open_order_drift_over_threshold_since, 1000.0)

                rt._kraken_open_orders_current = 5
                rt._kraken_open_orders_ts = 1010.0
                rt._maybe_alert_persistent_open_order_drift(now=1010.0)
                self.assertIsNone(rt._open_order_drift_over_threshold_since)

    def test_status_payload_exposes_persistent_open_order_drift_hint(self):
        rt = self._runtime_with_one_order()
        rt._kraken_open_orders_current = 25
        rt._kraken_open_orders_ts = 1000.0
        rt._open_order_drift_over_threshold_since = 600.0
        with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10):
            with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC", 300):
                with mock.patch("bot._now", return_value=1000.0):
                    payload = rt.status_payload()

        hints = payload["capacity_fill_health"]["blocked_risk_hint"]
        self.assertIn("open_order_drift_persistent", hints)

    def test_open_order_drift_recovery_notifies_once_when_cleared(self):
        rt = self._runtime_with_one_order()
        rt._kraken_open_orders_current = 25
        with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_THRESHOLD", 10):
            with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_PERSIST_SEC", 300):
                with mock.patch.object(config, "OPEN_ORDER_DRIFT_ALERT_COOLDOWN_SEC", 900):
                    with mock.patch("notifier._send_message") as send_mock:
                        for ts in (1000.0, 1301.0):
                            rt._kraken_open_orders_ts = ts
                            rt._maybe_alert_persistent_open_order_drift(now=ts)

                        self.assertEqual(send_mock.call_count, 1)
                        self.assertIn("Open-order drift persistent", send_mock.call_args_list[0].args[0])
                        self.assertTrue(rt._open_order_drift_alert_active)

                        rt._kraken_open_orders_current = 5
                        rt._kraken_open_orders_ts = 1310.0
                        rt._maybe_alert_persistent_open_order_drift(now=1310.0)

                        self.assertEqual(send_mock.call_count, 2)
                        self.assertIn("Open-order drift recovered", send_mock.call_args_list[1].args[0])
                        self.assertFalse(rt._open_order_drift_alert_active)
                        self.assertIsNone(rt._open_order_drift_over_threshold_since)


class PrivateApiMetronomeTests(unittest.TestCase):
    def test_rate_limiter_metronome_waits_between_waves(self):
        limiter = kraken_client._RateLimiter(max_budget=100, decay_rate=100.0)
        with mock.patch.object(config, "PRIVATE_API_METRONOME_ENABLED", True):
            with mock.patch.object(config, "PRIVATE_API_METRONOME_WAVE_CALLS", 1):
                with mock.patch.object(config, "PRIVATE_API_METRONOME_WAVE_SECONDS", 0.08):
                    start = time.time()
                    limiter.consume(1)
                    limiter.consume(1)
                    elapsed = time.time() - start
                    self.assertGreaterEqual(elapsed, 0.06)
                    telemetry = limiter.telemetry()
                    self.assertGreaterEqual(telemetry["wait_events"], 1)
                    self.assertTrue(telemetry["enabled"])

    def test_status_payload_exposes_private_api_metronome(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        fake = {
            "enabled": True,
            "wave_calls": 2,
            "wave_seconds": 1.5,
            "wave_calls_used": 1,
            "wave_window_remaining_sec": 0.6,
            "wait_events": 3,
            "wait_total_sec": 1.2,
            "last_wait_sec": 0.5,
            "calls_last_60s": 40,
            "effective_calls_per_sec": 0.6667,
            "budget_available": 9.5,
            "consecutive_rate_errors": 0,
        }
        with mock.patch("kraken_client.rate_limit_telemetry", return_value=fake):
            payload = rt.status_payload()
        meta = payload["capacity_fill_health"]["private_api_metronome"]
        self.assertTrue(meta["enabled"])
        self.assertEqual(meta["wave_calls"], 2)
        self.assertEqual(meta["wait_events"], 3)
        self.assertAlmostEqual(meta["wait_total_sec"], 1.2)


if __name__ == "__main__":
    unittest.main()
