import io
import time
import unittest
from collections import deque
from types import SimpleNamespace
from unittest import mock

import bot
import config
import dashboard
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

    def test_entry_fill_carries_regime_tag_to_exit_order(self):
        st = sm.PairState(
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
                    regime_at_entry=2,
                ),
            ),
            next_order_id=2,
        )
        cfg = self._cfg()
        fill = sm.FillEvent(
            order_local_id=1,
            txid="TX-B-ENTRY",
            side="buy",
            price=0.0998,
            volume=13.0,
            fee=0.01,
            timestamp=1010.0,
        )

        st2, _ = sm.transition(st, fill, cfg, order_size_usd=2.0)
        exit_order = next(o for o in st2.orders if o.role == "exit")
        self.assertEqual(exit_order.regime_at_entry, 2)

    def test_exit_fill_carries_regime_tag_to_cycle_record(self):
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
                    regime_at_entry=0,
                ),
            ),
            cycle_a=1,
            cycle_b=1,
        )
        cfg = self._cfg()
        fill = sm.FillEvent(
            order_local_id=1,
            txid="TX-A-EXIT",
            side="sell",
            price=0.1008,
            volume=13.0,
            fee=0.01,
            timestamp=1010.0,
        )

        st2, _ = sm.transition(st, fill, cfg, order_size_usd=2.0)
        self.assertTrue(st2.completed_cycles)
        self.assertEqual(st2.completed_cycles[-1].regime_at_entry, 0)

    def test_cycle_records_include_settled_usd_fee_split(self):
        st = sm.PairState(
            market_price=0.1,
            now=1000.0,
            orders=(
                sm.OrderState(
                    local_id=1,
                    side="buy",
                    role="exit",
                    price=0.1000,
                    volume=13.0,
                    trade_id="A",
                    cycle=1,
                    txid="TX-A-EXIT",
                    placed_at=999.0,
                    entry_price=0.1010,
                    entry_fee=0.0020,
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
        cfg = self._cfg()
        fill = sm.FillEvent(
            order_local_id=1,
            txid="TX-A-EXIT",
            side="buy",
            price=0.1000,
            volume=13.0,
            fee=0.0030,
            timestamp=1010.0,
        )

        st2, actions = sm.transition(st, fill, cfg, order_size_usd=2.0)
        cycle = st2.completed_cycles[-1]
        self.assertAlmostEqual(cycle.entry_fee, 0.0020, places=8)
        self.assertAlmostEqual(cycle.exit_fee, 0.0030, places=8)
        self.assertAlmostEqual(cycle.quote_fee, 0.0020, places=8)
        self.assertAlmostEqual(cycle.settled_usd, 0.0110, places=8)
        self.assertAlmostEqual(st2.total_settled_usd, 0.0110, places=8)

        book = next(a for a in actions if isinstance(a, sm.BookCycleAction))
        self.assertAlmostEqual(book.settled_usd, 0.0110, places=8)

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

    def test_execute_actions_bootstrap_bypasses_entry_scheduler_cap(self):
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

        with mock.patch.object(rt, "_place_order", side_effect=["TX-A", "TX-B"]) as place_order:
            rt._execute_actions(0, [a1, a2], "bootstrap")

        o1 = sm.find_order(rt.slots[0].state, a1.local_id)
        o2 = sm.find_order(rt.slots[0].state, a2.local_id)
        self.assertTrue(bool(o1 and o1.txid))
        self.assertTrue(bool(o2 and o2.txid))
        self.assertEqual(place_order.call_count, 2)
        self.assertEqual(rt.entry_adds_per_loop_used, 2)
        self.assertEqual(rt._entry_adds_deferred_total, 0)

    def test_execute_actions_non_bootstrap_respects_scheduler_cap(self):
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

        with mock.patch.object(rt, "_place_order", side_effect=["TX-A", "TX-B"]) as place_order:
            rt._execute_actions(0, [a1, a2], "unit_test")

        o1 = sm.find_order(rt.slots[0].state, a1.local_id)
        o2 = sm.find_order(rt.slots[0].state, a2.local_id)
        self.assertIsNotNone(o1)
        self.assertIsNotNone(o2)
        self.assertTrue(bool(o1.txid) ^ bool(o2.txid))
        self.assertEqual(place_order.call_count, 1)
        self.assertEqual(rt.entry_adds_per_loop_used, 1)
        self.assertEqual(rt._entry_adds_deferred_total, 1)
        self.assertEqual(len(rt._pending_entry_orders()), 1)

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

    def test_slot_order_size_usd_applies_throughput_multiplier(self):
        rt = bot.BotRuntime()
        rt._throughput = mock.Mock()
        rt._throughput.size_for_slot.return_value = (3.5, "tp_aggregate")
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(
                market_price=0.1,
                now=1000.0,
                total_profit=0.0,
            ),
        )

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        with mock.patch.object(rt, "_current_regime_id", return_value=2):
                            out = rt._slot_order_size_usd(slot, trade_id="A")

        self.assertAlmostEqual(out, 3.5)
        rt._throughput.size_for_slot.assert_called_once_with(2.0, regime_label="bullish", trade_id="A")

    def test_dust_dividend_zero_when_no_surplus(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
            )
        }
        rt._loop_available_usd = 2.0
        rt._loop_dust_dividend = None

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                    out = rt._compute_dust_dividend()

        self.assertAlmostEqual(out, 0.0)
        self.assertAlmostEqual(rt._dust_last_dividend_usd, 0.0)

    def test_dust_dividend_splits_across_slots(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
            ),
            1: bot.SlotRuntime(
                slot_id=1,
                state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
            ),
            2: bot.SlotRuntime(
                slot_id=2,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    total_profit=0.0,
                    orders=(
                        sm.OrderState(
                            local_id=10,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B2",
                        ),
                    ),
                ),
            ),
            3: bot.SlotRuntime(
                slot_id=3,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    total_profit=0.0,
                    orders=(
                        sm.OrderState(
                            local_id=11,
                            side="buy",
                            role="entry",
                            price=0.0998,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="TX-B3",
                        ),
                    ),
                ),
            ),
        }
        rt._loop_available_usd = 10.0
        rt._loop_dust_dividend = None

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                    out = rt._compute_dust_dividend()

        # Base split across 4 slots = 2.5. Two slots are buy-ready, so reserve=5.0.
        # Surplus=10.0-5.0 -> 5.0, dividend=2.5.
        self.assertAlmostEqual(out, 2.5, places=8)

    def test_b_side_sizing_allocates_surplus_to_buy_ready_slots(self):
        rt = bot.BotRuntime()
        ready = sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0)
        not_ready = sm.PairState(
            market_price=0.1,
            now=1000.0,
            total_profit=0.0,
            orders=(
                sm.OrderState(
                    local_id=10,
                    side="buy",
                    role="entry",
                    price=0.0998,
                    volume=13.0,
                    trade_id="B",
                    cycle=1,
                    txid="TX-B",
                ),
            ),
        )
        rt.slots = {
            0: bot.SlotRuntime(slot_id=0, state=ready),
            1: bot.SlotRuntime(slot_id=1, state=ready),
            2: bot.SlotRuntime(slot_id=2, state=not_ready),
            3: bot.SlotRuntime(slot_id=3, state=not_ready),
        }
        rt._loop_available_usd = 10.0
        rt._loop_dust_dividend = None
        rt._loop_b_side_base = None

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        size_ready = rt._slot_order_size_usd(rt.slots[0], trade_id="B")
                        size_not_ready = rt._slot_order_size_usd(rt.slots[2], trade_id="B")

        # Base = 10/4 = 2.5. Surplus goes only to two buy-ready slots (+2.5 each).
        self.assertAlmostEqual(size_ready, 5.0, places=8)
        self.assertAlmostEqual(size_not_ready, 2.5, places=8)

    def test_b_side_account_aware_sizing(self):
        """B-side uses available_usd / slot_count instead of per-slot profit compounding."""
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt._loop_available_usd = 7.0
        rt._loop_dust_dividend = None
        rt._loop_b_side_base = None

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        out = rt._slot_order_size_usd(slot, trade_id="B")

        # max(ORDER_SIZE_USD, available / slots) = max(2.0, 7.0/1) = 7.0
        self.assertAlmostEqual(out, 7.0, places=8)

    def test_dust_disabled(self):
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt._loop_available_usd = 7.0
        rt._loop_dust_dividend = None
        rt._dust_sweep_enabled = False

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        out = rt._slot_order_size_usd(slot, trade_id="B")
                        dividend = rt._compute_dust_dividend()

        # B-side still uses account-aware base even with dust disabled.
        # max(2.0, 7.0/1) = 7.0
        self.assertAlmostEqual(out, 7.0, places=8)
        self.assertAlmostEqual(dividend, 0.0, places=8)

    def test_dust_below_threshold(self):
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt._loop_available_usd = 2.3
        rt._loop_dust_dividend = None
        rt._dust_sweep_enabled = True
        rt._dust_min_threshold_usd = 0.50

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        out = rt._slot_order_size_usd(slot, trade_id="B")
                        dividend = rt._compute_dust_dividend()

        self.assertAlmostEqual(dividend, 0.0, places=8)
        # B-side: max(2.0, 2.3/1) = 2.3
        self.assertAlmostEqual(out, 2.3, places=8)

    def test_dust_no_buy_slots(self):
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
                            price=0.1010,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                        ),
                    ),
                ),
            )
        }
        rt._loop_available_usd = 50.0
        rt._loop_dust_dividend = None

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                    out = rt._compute_dust_dividend()

        self.assertAlmostEqual(out, 0.0, places=8)

    def test_dust_interacts_with_throughput(self):
        rt = bot.BotRuntime()
        rt._throughput = mock.Mock()
        rt._throughput.size_for_slot.return_value = (4.0, "tp_aggregate")
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt._loop_available_usd = 10.0
        rt._loop_dust_dividend = None
        rt._dust_sweep_enabled = True
        rt._dust_max_bump_pct = 25.0

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", False):
                with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                    with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                        with mock.patch.object(rt, "_current_regime_id", return_value=2):
                            out = rt._slot_order_size_usd(slot, trade_id="B")

        # B-side base = max(2.0, 10.0/1) = 10.0, throughput returns 4.0, no dust bump
        self.assertAlmostEqual(out, 4.0, places=8)
        self.assertIn(
            mock.call(10.0, regime_label="bullish", trade_id="B"),
            rt._throughput.size_for_slot.call_args_list,
        )

    def test_dust_fund_guard_clamp(self):
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt._loop_available_usd = 3.0
        rt._loop_dust_dividend = None
        rt._dust_sweep_enabled = True
        rt._dust_max_bump_pct = 25.0
        rt._rebalancer_current_skew = 0.5

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "REBALANCE_ENABLED", True):
                with mock.patch.object(config, "REBALANCE_SIZE_SENSITIVITY", 1.0):
                    with mock.patch.object(config, "REBALANCE_MAX_SIZE_MULT", 2.0):
                        with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                            with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                                out = rt._slot_order_size_usd(slot, trade_id="B")

        # B-side base = max(2.0, 3.0/1) = 3.0, rebalancer 1.5x = 4.5,
        # fund guard caps at max(3.0, 3.0-3.0) = 3.0
        self.assertAlmostEqual(out, 3.0, places=8)

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

    def test_auto_repair_skips_regime_mode_source_while_suppressed(self):
        """Repair is skipped when regime suppression is still active (Tier 2)."""
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt._regime_tier = 2
        rt._regime_side_suppressed = "A"
        rt._regime_tier2_grace_start = 1.0  # old timestamp so grace has elapsed
        rt._regime_tier_entered_at = 1.0
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

        saved = config.REGIME_DIRECTIONAL_ENABLED
        try:
            config.REGIME_DIRECTIONAL_ENABLED = True
            with mock.patch.object(rt, "_safe_balance") as safe_balance:
                with mock.patch.object(rt, "_execute_actions") as exec_actions:
                    rt._auto_repair_degraded_slot(0)

            safe_balance.assert_not_called()
            exec_actions.assert_not_called()
            self.assertTrue(rt.slots[0].state.long_only)
            self.assertEqual(rt.slots[0].state.mode_source, "regime")
        finally:
            config.REGIME_DIRECTIONAL_ENABLED = saved

    def test_auto_repair_restores_regime_slot_when_suppression_lapsed(self):
        """Repair proceeds when regime drops back to tier 0 (no suppression)."""
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt._regime_tier = 0
        rt._regime_side_suppressed = None
        rt._regime_tier2_last_downgrade_at = 0.0
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

        with mock.patch.object(rt, "_safe_balance", return_value={"ZUSD": "50.0", "XXDG": "1000.0"}):
            with mock.patch.object(rt, "_execute_actions") as exec_actions:
                rt._auto_repair_degraded_slot(0)
                exec_actions.assert_called_once()

        sides = [o.side for o in rt.slots[0].state.orders if o.role == "entry"]
        self.assertIn("sell", sides)
        self.assertFalse(rt.slots[0].state.long_only)

    def test_auto_repair_skips_during_cooldown(self):
        """Repair remains blocked while Tier2 re-entry cooldown is active."""
        rt = bot.BotRuntime()
        rt.mode = "RUNNING"
        rt.last_price = 0.1
        rt.constraints = {
            "price_decimals": 6,
            "volume_decimals": 0,
            "min_volume": 13.0,
            "min_cost_usd": 0.0,
        }
        rt._regime_tier = 0
        rt._regime_tier2_last_downgrade_at = 900.0
        rt._regime_cooldown_suppressed_side = "A"
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

        with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", True):
            with mock.patch.object(config, "REGIME_TIER2_REENTRY_COOLDOWN_SEC", 600.0):
                with mock.patch("bot._now", return_value=1000.0):
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
        ns_cursor = 1700000000000000000
        rows = [
            [1500, "0.10", "0.11", "0.09", "0.105", "0.103", "1200.0", "42"],
            [1800, "0.105", "0.12", "0.10", "0.11", "0.108", "900.0", "35"],
        ]
        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_OHLCV_INTERVAL_MIN", 5):
                with mock.patch.object(config, "HMM_OHLCV_SYNC_INTERVAL_SEC", 300.0):
                    with mock.patch("kraken_client.get_ohlc_page", return_value=(rows, ns_cursor)):
                        with mock.patch("supabase_store.queue_ohlcv_candles") as queue_mock:
                            rt._sync_ohlcv_candles(now=2000.0)

        queue_mock.assert_called_once()
        queued = queue_mock.call_args.args[0]
        self.assertEqual(len(queued), 1)
        self.assertAlmostEqual(float(queued[0]["time"]), 1500.0)
        self.assertEqual(rt._ohlcv_since_cursor, ns_cursor)
        self.assertEqual(rt._ohlcv_last_rows_queued, 1)

    def test_backfill_ohlcv_history_first_page_uses_since_none(self):
        rt = bot.BotRuntime()
        parsed = [{"time": 1000.0}]
        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_BACKFILL_MAX_STALLS", 3):
                with mock.patch("supabase_store.load_ohlcv_candles", return_value=[]):
                    with mock.patch.object(rt, "_normalize_kraken_ohlcv_rows", return_value=parsed):
                        with mock.patch("kraken_client.get_ohlc_page", return_value=([["raw"]], 42)) as ohlc_mock:
                            with mock.patch("supabase_store.queue_ohlcv_candles"):
                                ok, _msg = rt.backfill_ohlcv_history(
                                    target_candles=1,
                                    max_pages=1,
                                    interval_min=1,
                                    state_key="primary",
                                )

        self.assertTrue(ok)
        self.assertIsNone(ohlc_mock.call_args.kwargs["since"])

    def test_backfill_ohlcv_history_stall_breaker_skips_api_after_limit(self):
        rt = bot.BotRuntime()
        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_BACKFILL_MAX_STALLS", 2):
                with mock.patch("supabase_store.load_ohlcv_candles", return_value=[]):
                    with mock.patch.object(rt, "_normalize_kraken_ohlcv_rows", return_value=[]):
                        with mock.patch("kraken_client.get_ohlc_page", return_value=([], 7)) as ohlc_mock:
                            with mock.patch("supabase_store.queue_ohlcv_candles"):
                                ok1, _ = rt.backfill_ohlcv_history(target_candles=1, max_pages=1, interval_min=1)
                                ok2, _ = rt.backfill_ohlcv_history(target_candles=1, max_pages=1, interval_min=1)
                                ok3, msg3 = rt.backfill_ohlcv_history(target_candles=1, max_pages=1, interval_min=1)

        self.assertFalse(ok1)
        self.assertFalse(ok2)
        self.assertFalse(ok3)
        self.assertEqual(ohlc_mock.call_count, 2)
        self.assertEqual(rt._hmm_backfill_stall_count, 2)
        self.assertIn("circuit-breaker open", msg3)
        self.assertEqual(rt._hmm_backfill_last_message, "backfill_circuit_open:stalls=2/2")

    def test_backfill_ohlcv_history_resets_stalls_on_progress(self):
        rt = bot.BotRuntime()
        rt._hmm_backfill_stall_count = 1
        parsed = [{"time": 1000.0}]
        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_BACKFILL_MAX_STALLS", 3):
                with mock.patch("supabase_store.load_ohlcv_candles", return_value=[]):
                    with mock.patch.object(rt, "_normalize_kraken_ohlcv_rows", return_value=parsed):
                        with mock.patch("kraken_client.get_ohlc_page", return_value=([["raw"]], None)):
                            with mock.patch("supabase_store.queue_ohlcv_candles"):
                                ok, _msg = rt.backfill_ohlcv_history(
                                    target_candles=1,
                                    max_pages=1,
                                    interval_min=1,
                                    state_key="primary",
                                )

        self.assertTrue(ok)
        self.assertEqual(rt._hmm_backfill_stall_count, 0)
        self.assertNotIn("stalls=", rt._hmm_backfill_last_message)

    def test_backfill_ohlcv_history_already_ready_skips_api(self):
        rt = bot.BotRuntime()
        existing = [{"time": float(i)} for i in range(720)]
        with mock.patch("supabase_store.load_ohlcv_candles", return_value=existing):
            with mock.patch("kraken_client.get_ohlc_page") as ohlc_mock:
                ok, msg = rt.backfill_ohlcv_history(
                    target_candles=720,
                    max_pages=1,
                    interval_min=1,
                    state_key="primary",
                )

        self.assertTrue(ok)
        self.assertIn("already sufficient: 720/720", msg)
        self.assertEqual(rt._hmm_backfill_last_message, "already_ready:720/720")
        ohlc_mock.assert_not_called()

    def test_backfill_ohlcv_history_respects_training_target_override(self):
        rt = bot.BotRuntime()
        with mock.patch.object(config, "HMM_TRAINING_CANDLES", 2000):
            with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
                with mock.patch("supabase_store.load_ohlcv_candles", return_value=[]) as load_mock:
                    with mock.patch.object(rt, "_normalize_kraken_ohlcv_rows", return_value=[]):
                        with mock.patch("kraken_client.get_ohlc_page", return_value=([], None)):
                            with mock.patch("supabase_store.queue_ohlcv_candles"):
                                rt.backfill_ohlcv_history(
                                    target_candles=None,
                                    max_pages=1,
                                    interval_min=1,
                                    state_key="primary",
                                )

        self.assertEqual(load_mock.call_args.kwargs["limit"], 2000)

    def test_hmm_data_readiness_reports_target_window_against_720(self):
        rt = bot.BotRuntime()
        rows = []
        for i in range(500):
            rows.append(
                {
                    "time": float(i * 60),
                    "close": 0.1 + i * 1e-8,
                    "volume": 1000.0,
                }
            )

        with mock.patch.object(config, "HMM_OHLCV_ENABLED", True):
            with mock.patch.object(config, "HMM_TRAINING_CANDLES", 720):
                with mock.patch.object(config, "HMM_MIN_TRAIN_SAMPLES", 500):
                    with mock.patch.object(config, "HMM_OHLCV_INTERVAL_MIN", 1):
                        with mock.patch("supabase_store.load_ohlcv_candles", return_value=rows) as load_mock:
                            out = rt._hmm_data_readiness(float(rows[-1]["time"] + 60.0))

        self.assertEqual(load_mock.call_args.kwargs["limit"], 720)
        self.assertIn("below_target_window:500/720", out["gaps"])

    def test_backfill_command_resets_stall_counter_for_resolved_state_key(self):
        rt = bot.BotRuntime()
        rt._hmm_backfill_stall_count = 4
        rt._hmm_backfill_stall_count_secondary = 6
        rt._hmm_backfill_stall_count_tertiary = 8

        with mock.patch.object(config, "HMM_OHLCV_INTERVAL_MIN", 1):
            with mock.patch.object(config, "HMM_SECONDARY_INTERVAL_MIN", 15):
                with mock.patch.object(config, "HMM_TERTIARY_INTERVAL_MIN", 60):
                    with mock.patch("notifier._send_message"):
                        with mock.patch.object(rt, "backfill_ohlcv_history", return_value=(True, "queued")):
                            with mock.patch("notifier.poll_updates", return_value=([], [{"text": "/backfill_ohlcv 10 1 1"}])):
                                rt.poll_telegram()
                            self.assertEqual(rt._hmm_backfill_stall_count, 0)
                            self.assertEqual(rt._hmm_backfill_stall_count_secondary, 6)
                            self.assertEqual(rt._hmm_backfill_stall_count_tertiary, 8)

                            rt._hmm_backfill_stall_count = 5
                            rt._hmm_backfill_stall_count_secondary = 7
                            with mock.patch("notifier.poll_updates", return_value=([], [{"text": "/backfill_ohlcv 10 1 15"}])):
                                rt.poll_telegram()
                            self.assertEqual(rt._hmm_backfill_stall_count, 5)
                            self.assertEqual(rt._hmm_backfill_stall_count_secondary, 0)
                            self.assertEqual(rt._hmm_backfill_stall_count_tertiary, 8)

                            rt._hmm_backfill_stall_count = 9
                            rt._hmm_backfill_stall_count_secondary = 10
                            rt._hmm_backfill_stall_count_tertiary = 11
                            with mock.patch("notifier.poll_updates", return_value=([], [{"text": "/backfill_ohlcv 10 1 60"}])):
                                rt.poll_telegram()
                            self.assertEqual(rt._hmm_backfill_stall_count, 9)
                            self.assertEqual(rt._hmm_backfill_stall_count_secondary, 10)
                            self.assertEqual(rt._hmm_backfill_stall_count_tertiary, 0)

    def test_startup_backfill_runs_tertiary_even_when_secondary_disabled(self):
        rt = bot.BotRuntime()
        readiness_calls: list[str] = []
        backfill_calls: list[str] = []

        def _readiness(*_args, **kwargs):
            key = str(kwargs.get("state_key", "primary") or "primary")
            readiness_calls.append(key)
            return {"state_key": key, "ready_for_target_window": False}

        def _backfill(*_args, **kwargs):
            key = str(kwargs.get("state_key", "primary") or "primary")
            backfill_calls.append(key)
            return True, "queued"

        with mock.patch.object(config, "HMM_OHLCV_BACKFILL_ON_STARTUP", True):
            with mock.patch.object(config, "HMM_SECONDARY_OHLCV_ENABLED", False):
                with mock.patch.object(config, "HMM_MULTI_TIMEFRAME_ENABLED", False):
                    with mock.patch.object(config, "HMM_TERTIARY_ENABLED", True):
                        with mock.patch.object(rt, "_hmm_data_readiness", side_effect=_readiness):
                            with mock.patch.object(rt, "backfill_ohlcv_history", side_effect=_backfill):
                                rt._maybe_backfill_ohlcv_on_startup()

        self.assertEqual(readiness_calls, ["primary", "tertiary"])
        self.assertEqual(backfill_calls, ["primary", "tertiary"])

    def test_resample_candles_from_lower_interval_aggregates_contiguous_groups(self):
        rows = [
            {"time": 900.0, "open": 1.00, "high": 1.20, "low": 0.95, "close": 1.10, "volume": 10.0},
            {"time": 1800.0, "open": 1.10, "high": 1.25, "low": 1.00, "close": 1.20, "volume": 11.0},
            {"time": 2700.0, "open": 1.20, "high": 1.30, "low": 1.10, "close": 1.15, "volume": 12.0},
            {"time": 3600.0, "open": 1.15, "high": 1.22, "low": 1.05, "close": 1.18, "volume": 13.0},
            {"time": 5400.0, "open": 1.18, "high": 1.24, "low": 1.16, "close": 1.22, "volume": 9.0},
            {"time": 6300.0, "open": 1.22, "high": 1.28, "low": 1.20, "close": 1.26, "volume": 8.0},
            {"time": 7200.0, "open": 1.26, "high": 1.29, "low": 1.21, "close": 1.23, "volume": 7.0},
            {"time": 8100.0, "open": 1.23, "high": 1.27, "low": 1.19, "close": 1.25, "volume": 6.0},
        ]

        out = bot.BotRuntime._resample_candles_from_lower_interval(
            rows,
            group_size=4,
            base_interval_sec=900.0,
        )

        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(float(out[0]["time"]), 900.0)
        self.assertAlmostEqual(float(out[0]["open"]), 1.00)
        self.assertAlmostEqual(float(out[0]["high"]), 1.30)
        self.assertAlmostEqual(float(out[0]["low"]), 0.95)
        self.assertAlmostEqual(float(out[0]["close"]), 1.18)
        self.assertAlmostEqual(float(out[0]["volume"]), 46.0)
        self.assertIsNone(out[0]["trade_count"])
        self.assertAlmostEqual(float(out[1]["time"]), 5400.0)
        self.assertAlmostEqual(float(out[1]["open"]), 1.18)
        self.assertAlmostEqual(float(out[1]["high"]), 1.29)
        self.assertAlmostEqual(float(out[1]["low"]), 1.16)
        self.assertAlmostEqual(float(out[1]["close"]), 1.25)
        self.assertAlmostEqual(float(out[1]["volume"]), 30.0)

    def test_update_hmm_tertiary_transition_requires_confirmation_candles(self):
        rt = bot.BotRuntime()
        rt._hmm_state_tertiary.update({
            "enabled": True,
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.55,
            "last_update_ts": 1000.0,
        })

        with mock.patch.object(config, "HMM_TERTIARY_INTERVAL_MIN", 1):
            with mock.patch.object(config, "ACCUM_CONFIRMATION_CANDLES", 2):
                rt._update_hmm_tertiary_transition(1000.0)
                rt._hmm_state_tertiary.update({
                    "regime": "BULLISH",
                    "last_update_ts": 1100.0,
                })
                rt._update_hmm_tertiary_transition(1100.0)
                first = dict(rt._hmm_tertiary_transition)
                rt._update_hmm_tertiary_transition(1165.0)
                second = dict(rt._hmm_tertiary_transition)

        self.assertEqual(first["from_regime"], "RANGING")
        self.assertEqual(first["to_regime"], "BULLISH")
        self.assertEqual(int(first["confirmation_count"]), 1)
        self.assertFalse(bool(first["confirmed"]))
        self.assertEqual(float(first["changed_at"]), 1100.0)
        self.assertGreaterEqual(int(second["confirmation_count"]), 2)
        self.assertTrue(bool(second["confirmed"]))

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

    def _compute_hmm_consensus_for_test(
        self,
        primary,
        secondary,
        *,
        multi_enabled=True,
        hmm_enabled=True,
        tier1_conf=0.20,
        dampen=0.5,
        w1=0.3,
        w15=0.7,
    ):
        rt = bot.BotRuntime()
        rt._hmm_state.update(primary)
        rt._hmm_state_secondary.update(secondary)

        with mock.patch.object(config, "HMM_ENABLED", bool(hmm_enabled)):
            with mock.patch.object(config, "HMM_MULTI_TIMEFRAME_ENABLED", bool(multi_enabled)):
                with mock.patch.object(config, "REGIME_TIER1_CONFIDENCE", float(tier1_conf)):
                    with mock.patch.object(config, "CONSENSUS_DAMPEN_FACTOR", float(dampen)):
                        with mock.patch.object(config, "CONSENSUS_1M_WEIGHT", float(w1)):
                            with mock.patch.object(config, "CONSENSUS_15M_WEIGHT", float(w15)):
                                return rt._compute_hmm_consensus()

    def test_consensus_ranging_with_negative_bias_keeps_ranging_label(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "RANGING",
                "confidence": 0.9884,
                "bias_signal": 0.011,
                "probabilities": {"bearish": 0.0003, "ranging": 0.9884, "bullish": 0.0113},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "RANGING",
                "confidence": 0.9807,
                "bias_signal": -0.019,
                "probabilities": {"bearish": 0.0193, "ranging": 0.9807, "bullish": 0.0},
            },
            tier1_conf=0.20,
            w1=0.3,
            w15=0.7,
        )

        self.assertEqual(out["agreement"], "full")
        self.assertEqual(out["regime"], "RANGING")
        self.assertAlmostEqual(float(out["bias_signal"]), -0.01, places=6)

    def test_consensus_ranging_with_positive_bias_keeps_ranging_label(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "RANGING",
                "confidence": 0.95,
                "bias_signal": 0.03,
                "probabilities": {"bearish": 0.01, "ranging": 0.98, "bullish": 0.01},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "RANGING",
                "confidence": 0.94,
                "bias_signal": 0.02,
                "probabilities": {"bearish": 0.01, "ranging": 0.97, "bullish": 0.02},
            },
            tier1_conf=0.20,
            w1=0.3,
            w15=0.7,
        )

        self.assertEqual(out["agreement"], "full")
        self.assertEqual(out["regime"], "RANGING")
        self.assertAlmostEqual(float(out["bias_signal"]), 0.023, places=6)

    def test_consensus_full_agreement_bullish_unchanged(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.70,
                "bias_signal": 0.40,
                "probabilities": {"bearish": 0.10, "ranging": 0.20, "bullish": 0.70},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.80,
                "bias_signal": 0.30,
                "probabilities": {"bearish": 0.05, "ranging": 0.15, "bullish": 0.80},
            },
            tier1_conf=0.20,
        )

        self.assertEqual(out["agreement"], "full")
        self.assertEqual(out["regime"], "BULLISH")

    def test_consensus_full_agreement_bearish_unchanged(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BEARISH",
                "confidence": 0.78,
                "bias_signal": -0.30,
                "probabilities": {"bearish": 0.80, "ranging": 0.15, "bullish": 0.05},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BEARISH",
                "confidence": 0.72,
                "bias_signal": -0.50,
                "probabilities": {"bearish": 0.75, "ranging": 0.20, "bullish": 0.05},
            },
            tier1_conf=0.20,
        )

        self.assertEqual(out["agreement"], "full")
        self.assertEqual(out["regime"], "BEARISH")

    def test_consensus_1m_cooling_uses_15m_label(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "RANGING",
                "confidence": 0.90,
                "bias_signal": 0.02,
                "probabilities": {"bearish": 0.05, "ranging": 0.90, "bullish": 0.05},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BEARISH",
                "confidence": 0.80,
                "bias_signal": -0.60,
                "probabilities": {"bearish": 0.80, "ranging": 0.15, "bullish": 0.05},
            },
            tier1_conf=0.20,
            dampen=0.5,
        )

        self.assertEqual(out["agreement"], "1m_cooling")
        self.assertEqual(out["regime"], "BEARISH")
        self.assertAlmostEqual(float(out["confidence"]), 0.40, places=6)

    def test_consensus_conflict_gives_ranging(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.85,
                "bias_signal": 0.60,
                "probabilities": {"bearish": 0.05, "ranging": 0.15, "bullish": 0.80},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BEARISH",
                "confidence": 0.82,
                "bias_signal": -0.50,
                "probabilities": {"bearish": 0.78, "ranging": 0.17, "bullish": 0.05},
            },
            tier1_conf=0.20,
        )

        self.assertEqual(out["agreement"], "conflict")
        self.assertEqual(out["regime"], "RANGING")
        self.assertAlmostEqual(float(out["confidence"]), 0.0, places=6)

    def test_consensus_low_confidence_gives_ranging(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.12,
                "bias_signal": 0.50,
                "probabilities": {"bearish": 0.10, "ranging": 0.20, "bullish": 0.70},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.15,
                "bias_signal": 0.40,
                "probabilities": {"bearish": 0.12, "ranging": 0.20, "bullish": 0.68},
            },
            tier1_conf=0.20,
        )

        self.assertEqual(out["agreement"], "full")
        self.assertEqual(out["regime"], "RANGING")
        self.assertLess(float(out["confidence"]), 0.20)

    def test_consensus_probabilities_blended(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.85,
                "bias_signal": 0.40,
                "probabilities": {"bearish": 0.20, "ranging": 0.50, "bullish": 0.30},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.90,
                "bias_signal": 0.60,
                "probabilities": {"bearish": 0.40, "ranging": 0.10, "bullish": 0.50},
            },
            w1=0.25,
            w15=0.75,
        )

        probs = out["consensus_probabilities"]
        self.assertAlmostEqual(float(probs["bearish"]), 0.35, places=6)
        self.assertAlmostEqual(float(probs["ranging"]), 0.20, places=6)
        self.assertAlmostEqual(float(probs["bullish"]), 0.45, places=6)

    def test_consensus_probabilities_primary_only_uses_primary_probs(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.65,
                "bias_signal": 0.32,
                "probabilities": {"bearish": 0.20, "ranging": 0.10, "bullish": 0.70},
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BEARISH",
                "confidence": 0.70,
                "bias_signal": -0.22,
                "probabilities": {"bearish": 0.70, "ranging": 0.20, "bullish": 0.10},
            },
            multi_enabled=False,
        )

        self.assertEqual(out["agreement"], "primary_only")
        probs = out["consensus_probabilities"]
        self.assertAlmostEqual(float(probs["bearish"]), 0.20, places=6)
        self.assertAlmostEqual(float(probs["ranging"]), 0.10, places=6)
        self.assertAlmostEqual(float(probs["bullish"]), 0.70, places=6)

    def test_consensus_probabilities_primary_untrained_defaults_to_ranging(self):
        out = self._compute_hmm_consensus_for_test(
            primary={
                "available": False,
                "trained": False,
            },
            secondary={
                "available": True,
                "trained": True,
                "regime": "BULLISH",
                "confidence": 0.90,
                "bias_signal": 0.80,
                "probabilities": {"bearish": 0.10, "ranging": 0.10, "bullish": 0.80},
            },
            multi_enabled=True,
        )

        self.assertEqual(out["agreement"], "primary_untrained")
        probs = out["consensus_probabilities"]
        self.assertAlmostEqual(float(probs["bearish"]), 0.0, places=6)
        self.assertAlmostEqual(float(probs["ranging"]), 1.0, places=6)
        self.assertAlmostEqual(float(probs["bullish"]), 0.0, places=6)

    def test_hmm_prob_triplet_dict_format(self):
        out = bot.BotRuntime._hmm_prob_triplet({
            "probabilities": {"bearish": "0.11", "ranging": 0.77, "bullish": 0.12},
        })
        self.assertEqual(out, [0.11, 0.77, 0.12])

    def test_hmm_prob_triplet_list_format(self):
        out = bot.BotRuntime._hmm_prob_triplet({
            "probabilities": [0.21, 0.55, 0.24],
        })
        self.assertEqual(out, [0.21, 0.55, 0.24])

    def test_hmm_prob_triplet_missing_defaults_to_ranging(self):
        out = bot.BotRuntime._hmm_prob_triplet({})
        self.assertEqual(out, [0.0, 1.0, 0.0])

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

    def test_status_payload_exposes_hmm_tertiary_pipeline_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        def _readiness(*_args, **kwargs):
            state_key = str(kwargs.get("state_key", "primary") or "primary")
            return {
                "enabled": True,
                "state_key": state_key,
                "ready_for_min_train": state_key == "tertiary",
                "ready_for_target_window": False,
                "gaps": [],
            }

        with mock.patch.object(config, "HMM_TERTIARY_ENABLED", True):
            with mock.patch.object(config, "HMM_MULTI_TIMEFRAME_ENABLED", False):
                with mock.patch.object(config, "HMM_SECONDARY_OHLCV_ENABLED", False):
                    with mock.patch.object(rt, "_hmm_data_readiness", side_effect=_readiness):
                        payload = rt.status_payload()

        self.assertIn("hmm_data_pipeline_tertiary", payload)
        self.assertEqual(payload["hmm_data_pipeline_tertiary"]["state_key"], "tertiary")
        self.assertTrue(payload["hmm_data_pipeline_tertiary"]["ready_for_min_train"])
        self.assertIn("tertiary", payload["hmm_regime"])
        self.assertIn("tertiary_transition", payload["hmm_regime"])

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

    def test_hmm_status_payload_exposes_confidence_modifier_fields(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "enabled": True,
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "regime_id": 2,
            "confidence": 0.60,
            "bias_signal": 0.40,
            "last_update_ts": 1000.0,
        })
        rt._hmm_training_depth = {
            "confidence_modifier": 0.70,
        }

        with mock.patch.object(config, "HMM_ENABLED", True):
            payload = rt._hmm_status_payload()

        self.assertAlmostEqual(float(payload["confidence"]), 0.60)
        self.assertAlmostEqual(float(payload["confidence_raw"]), 0.60)
        self.assertAlmostEqual(float(payload["confidence_modifier"]), 0.70)
        self.assertAlmostEqual(float(payload["confidence_effective"]), 0.42)
        self.assertEqual(payload["confidence_modifier_source"], "primary")

    def test_hmm_status_payload_includes_training_depth_block(self):
        rt = bot.BotRuntime()
        rt._hmm_training_depth = {
            "state_key": "primary",
            "current_candles": 1200,
            "target_candles": 4000,
            "min_train_samples": 500,
            "quality_tier": "baseline",
            "confidence_modifier": 0.85,
            "pct_complete": 30.0,
            "interval_min": 1,
            "estimated_full_at": "2026-02-16T14:00:00+00:00",
            "updated_at": 1000.0,
        }
        rt._hmm_state.update({
            "enabled": True,
            "available": True,
            "trained": True,
            "confidence": 0.5,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            payload = rt._hmm_status_payload()

        self.assertIn("training_depth", payload)
        depth = payload["training_depth"]
        self.assertEqual(depth["current_candles"], 1200)
        self.assertEqual(depth["target_candles"], 4000)
        self.assertEqual(depth["quality_tier"], "baseline")
        self.assertAlmostEqual(float(depth["confidence_modifier"]), 0.85)
        self.assertAlmostEqual(float(depth["pct_complete"]), 30.0)

    def test_record_regime_history_30m_prunes_old_samples(self):
        rt = bot.BotRuntime()
        rt._regime_history_30m.clear()
        rt._hmm_state.update({
            "enabled": True,
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.55,
            "bias_signal": 0.22,
        })

        with mock.patch.object(config, "HMM_ENABLED", True):
            rt._record_regime_history_sample(now=1000.0)
            rt._record_regime_history_sample(now=1200.0)
            rt._record_regime_history_sample(now=2901.0)

        self.assertEqual(len(rt._regime_history_30m), 2)
        first = rt._regime_history_30m[0]
        latest = rt._regime_history_30m[-1]
        self.assertAlmostEqual(float(first["ts"]), 1200.0)
        self.assertAlmostEqual(float(latest["ts"]), 2901.0)
        self.assertEqual(latest["regime"], "BULLISH")
        self.assertAlmostEqual(float(latest["conf"]), 0.55)
        self.assertAlmostEqual(float(latest["bias"]), 0.22)

    def test_status_payload_exposes_regime_history_30m(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt._regime_history_30m = deque([
            {"ts": 1000.0, "regime": "RANGING", "conf": 0.1, "bias": 0.0},
            {"ts": 1300.0, "regime": "BULLISH", "conf": 0.4, "bias": 0.3},
        ])

        payload = rt.status_payload()
        self.assertIn("regime_history_30m", payload)
        self.assertEqual(len(payload["regime_history_30m"]), 2)
        self.assertIn("regime_history_30m", payload["hmm_regime"])
        self.assertEqual(len(payload["hmm_regime"]["regime_history_30m"]), 2)

    def test_status_payload_exposes_ai_regime_advisor_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt._ai_regime_last_run_ts = 900.0
        rt._ai_regime_opinion = {
            "recommended_tier": 1,
            "recommended_direction": "long_bias",
            "conviction": 72,
            "accumulation_signal": "accumulate_doge",
            "accumulation_conviction": 78,
            "rationale": "Momentum building.",
            "watch_for": "15m confidence > 0.20.",
            "panelist": "Llama-70B",
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "agreement": "ai_upgrade",
            "error": "",
            "ts": 905.0,
            "mechanical_tier": 0,
            "mechanical_direction": "symmetric",
        }
        rt._ai_regime_history = deque([
            {
                "ts": 905.0,
                "mechanical_tier": 0,
                "mechanical_direction": "symmetric",
                "ai_tier": 1,
                "ai_direction": "long_bias",
                "conviction": 72,
                "agreement": "ai_upgrade",
                "action": "pending",
            }
        ])
        now_ts = time.time()
        rt._ai_override_tier = 1
        rt._ai_override_direction = "long_bias"
        rt._ai_override_applied_at = now_ts - 60.0
        rt._ai_override_until = now_ts + 1800.0
        rt._ai_override_source_conviction = 72

        with mock.patch.object(config, "AI_REGIME_ADVISOR_ENABLED", True):
            payload = rt.status_payload()

        self.assertIn("ai_regime_advisor", payload)
        block = payload["ai_regime_advisor"]
        self.assertTrue(block["enabled"])
        self.assertIn("default_ttl_sec", block)
        self.assertIn("min_conviction", block)
        self.assertEqual(block["opinion"]["recommended_tier"], 1)
        self.assertEqual(block["opinion"]["recommended_direction"], "long_bias")
        self.assertEqual(block["opinion"]["agreement"], "ai_upgrade")
        self.assertEqual(block["opinion"]["accumulation_signal"], "accumulate_doge")
        self.assertEqual(block["opinion"]["accumulation_conviction"], 78)
        self.assertEqual(block["opinion"]["provider"], "groq")
        self.assertEqual(block["opinion"]["model"], "llama-3.3-70b-versatile")
        self.assertTrue(block["override"]["active"])
        self.assertEqual(block["override"]["tier"], 1)
        self.assertEqual(len(block["history"]), 1)

    def test_build_ai_regime_context_exposes_capital_and_accumulation_blocks(self):
        rt = bot.BotRuntime()
        rt._ai_regime_opinion = {
            "accumulation_signal": "accumulate_doge",
            "accumulation_conviction": 66,
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "RANGING",
            "confidence": 0.5,
            "bias_signal": 0.0,
            "probabilities": {"bearish": 0.2, "ranging": 0.6, "bullish": 0.2},
        })

        with mock.patch.object(rt, "_available_free_balances", return_value=(120.0, 1800.0)):
            with mock.patch.object(rt, "_compute_doge_bias_scoreboard", return_value={"idle_usd": 45.0, "idle_usd_pct": 37.5}):
                with mock.patch.object(config, "ACCUM_ENABLED", True):
                    context = rt._build_ai_regime_context(now=1000.0)

        self.assertIn("capital", context)
        self.assertAlmostEqual(float(context["capital"]["free_usd"]), 120.0)
        self.assertAlmostEqual(float(context["capital"]["idle_usd"]), 45.0)
        self.assertAlmostEqual(float(context["capital"]["idle_usd_pct"]), 37.5)
        self.assertAlmostEqual(float(context["capital"]["free_doge"]), 1800.0)
        self.assertIn("accumulation", context)
        self.assertTrue(bool(context["accumulation"]["enabled"]))
        self.assertEqual(context["accumulation"]["signal"], "accumulate_doge")
        self.assertEqual(int(context["accumulation"]["conviction"]), 66)

    def test_update_accumulation_arms_on_confirmed_tertiary_transition(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._hmm_tertiary_transition = {
            "from_regime": "BEARISH",
            "to_regime": "RANGING",
            "confirmed": True,
        }
        rt._ai_regime_opinion = {
            "accumulation_signal": "hold",
            "accumulation_conviction": 0,
        }

        with mock.patch.object(config, "ACCUM_ENABLED", True):
            with mock.patch.object(config, "ACCUM_RESERVE_USD", 50.0):
                with mock.patch.object(config, "ACCUM_MAX_BUDGET_USD", 50.0):
                    with mock.patch.object(rt, "_compute_capacity_health", return_value={"status_band": "normal"}):
                        with mock.patch.object(rt, "_compute_doge_bias_scoreboard", return_value={"idle_usd": 74.0}):
                            with mock.patch.object(rt, "_available_free_balances", return_value=(120.0, 1000.0)):
                                rt._update_accumulation(now=1000.0)

        self.assertEqual(rt._accum_state, "ARMED")
        self.assertEqual(rt._accum_trigger_from_regime, "BEARISH")
        self.assertEqual(rt._accum_trigger_to_regime, "RANGING")
        self.assertAlmostEqual(float(rt._accum_budget_usd), 24.0, places=6)
        self.assertAlmostEqual(float(rt._accum_armed_at), 1000.0, places=6)

    def test_update_accumulation_active_places_market_buy(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._accum_state = "ARMED"
        rt._accum_direction = "doge"
        rt._accum_trigger_from_regime = "BEARISH"
        rt._accum_trigger_to_regime = "RANGING"
        rt._accum_budget_usd = 6.0
        rt._accum_armed_at = 900.0
        rt._hmm_tertiary_transition = {
            "from_regime": "BEARISH",
            "to_regime": "RANGING",
            "confirmed": True,
        }
        rt._hmm_state_tertiary.update({
            "regime": "RANGING",
        })
        rt._ai_regime_opinion = {
            "accumulation_signal": "accumulate_doge",
            "accumulation_conviction": 80,
        }

        with mock.patch.object(config, "ACCUM_ENABLED", True):
            with mock.patch.object(config, "ACCUM_MIN_CONVICTION", 60):
                with mock.patch.object(config, "ACCUM_RESERVE_USD", 20.0):
                    with mock.patch.object(config, "ACCUM_MAX_BUDGET_USD", 50.0):
                        with mock.patch.object(config, "ACCUM_CHUNK_USD", 2.0):
                            with mock.patch.object(config, "ACCUM_INTERVAL_SEC", 120.0):
                                with mock.patch.object(rt, "_compute_capacity_health", return_value={"status_band": "normal"}):
                                    with mock.patch.object(rt, "_compute_doge_bias_scoreboard", return_value={"idle_usd": 80.0}):
                                        with mock.patch.object(rt, "_available_free_balances", return_value=(120.0, 1000.0)):
                                            with mock.patch.object(rt, "_try_reserve_loop_funds", return_value=True):
                                                with mock.patch.object(rt, "_consume_private_budget", return_value=True):
                                                    with mock.patch("bot.kraken_client.place_order", return_value="TX-ACC-1") as place_mock:
                                                        rt._update_accumulation(now=1000.0)

        self.assertEqual(rt._accum_state, "ACTIVE")
        self.assertEqual(rt._accum_n_buys, 1)
        self.assertAlmostEqual(float(rt._accum_spent_usd), 2.0, places=6)
        self.assertAlmostEqual(float(rt._accum_acquired_doge), 20.0, places=6)
        self.assertAlmostEqual(float(rt._accum_last_buy_ts), 1000.0, places=6)
        place_mock.assert_called_once()
        self.assertEqual(place_mock.call_args.kwargs["ordertype"], "market")
        self.assertEqual(place_mock.call_args.kwargs["side"], "buy")

    def test_update_accumulation_active_stops_on_drawdown_breach(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.095
        rt._accum_state = "ACTIVE"
        rt._accum_direction = "doge"
        rt._accum_trigger_from_regime = "BEARISH"
        rt._accum_trigger_to_regime = "RANGING"
        rt._accum_start_ts = 1000.0
        rt._accum_start_price = 0.1
        rt._accum_budget_usd = 12.0
        rt._accum_spent_usd = 4.0
        rt._accum_acquired_doge = 40.0
        rt._accum_n_buys = 2
        rt._hmm_state_tertiary.update({
            "regime": "RANGING",
        })
        rt._hmm_tertiary_transition = {
            "from_regime": "BEARISH",
            "to_regime": "RANGING",
            "confirmed": True,
        }
        rt._ai_regime_opinion = {
            "accumulation_signal": "accumulate_doge",
            "accumulation_conviction": 90,
        }

        with mock.patch.object(config, "ACCUM_ENABLED", True):
            with mock.patch.object(config, "ACCUM_MAX_DRAWDOWN_PCT", 3.0):
                with mock.patch.object(config, "ACCUM_COOLDOWN_SEC", 3600.0):
                    with mock.patch.object(rt, "_compute_capacity_health", return_value={"status_band": "normal"}):
                        with mock.patch.object(rt, "_compute_doge_bias_scoreboard", return_value={"idle_usd": 60.0}):
                            with mock.patch.object(rt, "_available_free_balances", return_value=(120.0, 1000.0)):
                                rt._update_accumulation(now=1100.0)

        self.assertEqual(rt._accum_state, "STOPPED")
        self.assertIn("reason", rt._accum_last_session_summary)
        self.assertEqual(rt._accum_last_session_summary["reason"], "drawdown_breach")
        self.assertGreaterEqual(int(rt._accum_cooldown_remaining_sec), 3599)

    def test_status_payload_includes_accumulation_block(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        rt._accum_state = "ACTIVE"
        rt._accum_direction = "doge"
        rt._accum_budget_usd = 10.0
        rt._accum_spent_usd = 4.0
        rt._accum_acquired_doge = 40.0
        rt._accum_n_buys = 2
        rt._accum_start_ts = 1000.0
        rt._accum_start_price = 0.1
        rt._accum_trigger_from_regime = "BEARISH"
        rt._accum_trigger_to_regime = "RANGING"
        rt._ai_regime_opinion = {
            "accumulation_signal": "accumulate_doge",
            "accumulation_conviction": 77,
        }

        payload = rt.status_payload()

        self.assertIn("accumulation", payload)
        block = payload["accumulation"]
        self.assertEqual(block["state"], "ACTIVE")
        self.assertEqual(block["direction"], "doge")
        self.assertAlmostEqual(float(block["spent_usd"]), 4.0, places=6)
        self.assertAlmostEqual(float(block["budget_remaining_usd"]), 6.0, places=6)
        self.assertEqual(int(block["ai_accumulation_conviction"]), 77)

    def test_maybe_schedule_ai_regime_periodic_and_event_triggers(self):
        rt = bot.BotRuntime()
        rt._hmm_consensus = {"agreement": "primary_only"}

        with mock.patch.object(config, "AI_REGIME_ADVISOR_ENABLED", True):
            with mock.patch.object(config, "AI_REGIME_INTERVAL_SEC", 300.0):
                with mock.patch.object(config, "AI_REGIME_DEBOUNCE_SEC", 60.0):
                    with mock.patch.object(rt, "_start_ai_regime_run") as start_mock:
                        rt._ai_regime_last_run_ts = 0.0
                        rt._maybe_schedule_ai_regime(now=1000.0)
                        start_mock.assert_called_once()
                        self.assertEqual(start_mock.call_args[0][1], "periodic")

                    with mock.patch.object(rt, "_start_ai_regime_run") as start_mock:
                        rt._ai_regime_last_run_ts = 1000.0
                        rt._ai_regime_last_mechanical_tier = 0
                        rt._regime_mechanical_tier = 1
                        rt._ai_regime_last_consensus_agreement = "primary_only"
                        rt._hmm_consensus = {"agreement": "primary_only"}
                        rt._maybe_schedule_ai_regime(now=1120.0)
                        start_mock.assert_called_once()
                        self.assertEqual(start_mock.call_args[0][1], "mechanical_tier_change")

    def test_update_regime_tier_ai_override_enforces_one_tier_hop(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.10,
            "bias_signal": 0.40,
            "last_update_ts": 1000.0,
        })
        rt._ai_override_tier = 2
        rt._ai_override_direction = "long_bias"
        rt._ai_override_applied_at = 900.0
        rt._ai_override_until = 1900.0
        rt._ai_override_source_conviction = 80

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "AI_OVERRIDE_MIN_CONVICTION", 50):
                        rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_mechanical_tier, 0)
        self.assertEqual(rt._regime_tier, 1)
        self.assertEqual(rt._regime_shadow_state.get("reason"), "ai_override")
        self.assertTrue(bool(rt._regime_shadow_state.get("override_active")))

    def test_update_regime_tier_ai_override_respects_capacity_stop_gate(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.10,
            "bias_signal": 0.40,
            "last_update_ts": 1000.0,
        })
        rt._ai_override_tier = 1
        rt._ai_override_direction = "long_bias"
        rt._ai_override_applied_at = 900.0
        rt._ai_override_until = 1900.0
        rt._ai_override_source_conviction = 90

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(rt, "_compute_capacity_health", return_value={"status_band": "stop"}):
                        rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_mechanical_tier, 0)
        self.assertEqual(rt._regime_tier, 0)
        self.assertNotEqual(rt._regime_shadow_state.get("reason"), "ai_override")

    def test_update_regime_tier_applies_training_depth_modifier(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.60,
            "bias_signal": 0.70,
            "last_update_ts": 1000.0,
        })
        rt._hmm_training_depth = {
            "confidence_modifier": 0.70,
        }

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 1)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_raw", 0.0)), 0.60)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence", 0.0)), 0.42)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_effective", 0.0)), 0.42)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_modifier", 0.0)), 0.70)
        self.assertEqual(rt._regime_shadow_state.get("confidence_modifier_source"), "primary")

    def test_update_regime_tier_manual_override_bypasses_modifier(self):
        rt = bot.BotRuntime()
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime": "BULLISH",
            "confidence": 0.60,
            "bias_signal": 0.70,
            "last_update_ts": 1000.0,
        })
        rt._hmm_training_depth = {
            "confidence_modifier": 0.70,
        }

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(config, "REGIME_SHADOW_ENABLED", True):
                with mock.patch.object(config, "REGIME_DIRECTIONAL_ENABLED", False):
                    with mock.patch.object(config, "REGIME_MANUAL_OVERRIDE", "BULLISH"):
                        with mock.patch.object(config, "REGIME_MANUAL_CONFIDENCE", 0.80):
                            rt._update_regime_tier(now=1000.0)

        self.assertEqual(rt._regime_tier, 2)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_raw", 0.0)), 0.80)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence", 0.0)), 0.80)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_effective", 0.0)), 0.80)
        self.assertAlmostEqual(float(rt._regime_shadow_state.get("confidence_modifier", 0.0)), 1.0)
        self.assertEqual(rt._regime_shadow_state.get("confidence_modifier_source"), "manual_override")

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
            regime_at_entry=0,
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
            "regime": "BULLISH",
            "confidence": 0.60,
            "bias_signal": 0.40,
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
        self.assertEqual(row["regime_at_entry"], 0)
        self.assertEqual(row["regime_tier"], 1)
        self.assertEqual(row["against_trend"], True)
        self.assertAlmostEqual(row["total_age_sec"], 100.0)

    def test_execute_actions_stamps_exit_order_regime_at_entry(self):
        rt = bot.BotRuntime()
        now = time.time()
        rt.last_price = 0.1
        rt.last_price_ts = now
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=now,
                    orders=(
                        sm.OrderState(
                            local_id=2,
                            side="sell",
                            role="exit",
                            price=0.101,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            txid="",
                            placed_at=now,
                            entry_price=0.1,
                            entry_fee=0.01,
                            entry_filled_at=now - 5.0,
                        ),
                    ),
                    next_order_id=3,
                ),
            ),
        }
        rt._hmm_state.update({
            "available": True,
            "trained": True,
            "regime_id": 2,
        })
        action = sm.PlaceOrderAction(
            local_id=2,
            side="sell",
            role="exit",
            price=0.101,
            volume=13.0,
            trade_id="B",
            cycle=1,
            reason="entry_fill_exit",
        )

        with mock.patch.object(config, "HMM_ENABLED", True):
            with mock.patch.object(rt, "_try_reserve_loop_funds", return_value=True):
                with mock.patch.object(rt, "_place_order", return_value="TX-EXIT"):
                    rt._execute_actions(0, [action], "unit_test")

        stamped = sm.find_order(rt.slots[0].state, 2)
        self.assertIsNotNone(stamped)
        self.assertEqual(stamped.regime_at_entry, 2)
        self.assertEqual(stamped.txid, "TX-EXIT")

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

    def test_status_payload_exposes_dust_sweep_block(self):
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(market_price=0.1, now=1000.0, total_profit=0.0),
        )
        rt.slots = {0: slot}
        rt.last_price = 0.1
        rt._loop_available_usd = 7.0
        rt._loop_dust_dividend = None
        rt._dust_sweep_enabled = False
        rt._dust_last_absorbed_usd = 1.23

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                with mock.patch.object(rt, "_layer_mark_price", return_value=0.1):
                    payload = rt.status_payload()

        dust = payload.get("dust_sweep", {})
        self.assertFalse(bool(dust.get("enabled")))
        self.assertAlmostEqual(float(dust.get("current_dividend_usd") or 0.0), 0.0, places=8)
        self.assertAlmostEqual(float(dust.get("lifetime_absorbed_usd") or 0.0), 1.23, places=8)
        self.assertAlmostEqual(float(dust.get("available_usd") or 0.0), 7.0, places=8)

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

    def test_global_snapshot_persists_dust_fields(self):
        rt = bot.BotRuntime()
        rt._dust_last_absorbed_usd = 4.56
        rt._dust_last_dividend_usd = 0.12

        snap = rt._global_snapshot()

        self.assertAlmostEqual(float(snap["dust_last_absorbed_usd"]), 4.56, places=8)
        self.assertAlmostEqual(float(snap["dust_last_dividend_usd"]), 0.12, places=8)

    def test_load_snapshot_restores_dust_fields(self):
        rt = bot.BotRuntime()
        snap = rt._global_snapshot()
        snap["dust_last_absorbed_usd"] = 7.89
        snap["dust_last_dividend_usd"] = 0.34
        rt._dust_last_absorbed_usd = 0.0
        rt._dust_last_dividend_usd = 0.0

        with mock.patch("supabase_store.load_state", return_value=snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                rt._load_snapshot()

        self.assertAlmostEqual(rt._dust_last_absorbed_usd, 7.89, places=8)
        self.assertAlmostEqual(rt._dust_last_dividend_usd, 0.34, places=8)

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

    def test_load_snapshot_clears_expired_ai_override(self):
        rt = bot.BotRuntime()
        snap = rt._global_snapshot()
        snap["ai_override_tier"] = 1
        snap["ai_override_direction"] = "long_bias"
        snap["ai_override_applied_at"] = 1000.0
        snap["ai_override_until"] = 1200.0
        snap["ai_override_source_conviction"] = 80

        with mock.patch("supabase_store.load_state", return_value=snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                with mock.patch("bot._now", return_value=1500.0):
                    rt._load_snapshot()

        self.assertIsNone(rt._ai_override_tier)
        self.assertIsNone(rt._ai_override_direction)
        self.assertIsNone(rt._ai_override_until)
        self.assertIsNone(rt._ai_override_applied_at)
        self.assertIsNone(rt._ai_override_source_conviction)

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

    def test_status_payload_exposes_recovery_orders_enabled_flag(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            payload = rt.status_payload()

        self.assertIn("recovery_orders_enabled", payload)
        self.assertFalse(payload["recovery_orders_enabled"])

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
        self.assertIn("max_target_layers", layers)
        self.assertIn("layer_step_doge_eq", layers)

    def test_status_payload_exposes_capital_layer_max_target_override(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }

        with mock.patch.object(config, "CAPITAL_LAYER_MAX_TARGET_LAYERS", 17):
            payload = rt.status_payload()

        self.assertEqual(payload["capital_layers"]["max_target_layers"], 17)

    def test_dashboard_layer_source_not_synced_from_backend_default(self):
        self.assertNotIn("layerSourceSelect.value = sourceDefault", dashboard.DASHBOARD_HTML)

    def test_dashboard_layers_hardening_markup_and_disable_states_present(self):
        self.assertIn('id="layerTelemetryRows"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="layerNoLayers"', dashboard.DASHBOARD_HTML)
        self.assertIn("addLayerBtn.disabled = (targetLayers >= maxTargetLayers);", dashboard.DASHBOARD_HTML)
        self.assertIn("removeLayerBtn.disabled = (targetLayers <= 0);", dashboard.DASHBOARD_HTML)

    def test_dashboard_throughput_age_pressure_p90_surface_present(self):
        self.assertIn('id="throughputAgeLabel"', dashboard.DASHBOARD_HTML)
        self.assertIn("Age Pressure (p90)", dashboard.DASHBOARD_HTML)
        self.assertIn("age_pressure_ref_age_sec", dashboard.DASHBOARD_HTML)
        self.assertIn(" (healthy)", dashboard.DASHBOARD_HTML)

    def test_dashboard_hmm_card_includes_tertiary_rows(self):
        self.assertIn('id="hmmRegime1hRow"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="hmmRegime1h"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="hmmWindowTertiary"', dashboard.DASHBOARD_HTML)
        self.assertIn("hmmPipeTertiary", dashboard.DASHBOARD_HTML)

    def test_dashboard_ai_and_accumulation_surface_present(self):
        self.assertIn('id="aiRegimeProvider"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="aiRegimeAccumSignal"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumState"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumTrigger"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumBudget"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumDrawdown"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumAiSignal"', dashboard.DASHBOARD_HTML)
        self.assertIn('id="accumLastSession"', dashboard.DASHBOARD_HTML)

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

    def test_add_layer_doge_source_success(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 1.0, "DOGE": 230.0}):
            ok, msg = rt.add_layer("DOGE")

        self.assertTrue(ok)
        self.assertIn("layer added", msg)
        self.assertEqual(rt.target_layers, 1)
        self.assertEqual(rt.layer_last_add_event["source"], "DOGE")

    def test_add_layer_usd_source_success(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 30.0, "DOGE": 0.0}):
            ok, msg = rt.add_layer("USD")

        self.assertTrue(ok)
        self.assertIn("layer added", msg)
        self.assertEqual(rt.target_layers, 1)
        self.assertEqual(rt.layer_last_add_event["source"], "USD")

    def test_add_layer_rejects_underfunded_doge(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 1000.0, "DOGE": 10.0}):
            ok, msg = rt.add_layer("DOGE")

        self.assertFalse(ok)
        self.assertIn("need 225 DOGE", msg)
        self.assertEqual(rt.target_layers, 0)

    def test_add_layer_rejects_underfunded_usd(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 1.0, "DOGE": 1000.0}):
            ok, msg = rt.add_layer("USD")

        self.assertFalse(ok)
        self.assertIn("need $22.5000", msg)
        self.assertEqual(rt.target_layers, 0)

    def test_add_layer_rejects_underfunded_auto(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 10.0, "DOGE": 100.0}):
            ok, msg = rt.add_layer("AUTO")

        self.assertFalse(ok)
        self.assertIn("need 225 DOGE-eq", msg)
        self.assertEqual(rt.target_layers, 0)

    def test_effective_layers_never_exceeds_target(self):
        rt = bot.BotRuntime()
        cases = [
            {"target": 0, "usd": 0.0, "doge": 0.0, "sells": 0, "buys": 0, "price": 0.1},
            {"target": 1, "usd": 1000.0, "doge": 1000.0, "sells": 1, "buys": 1, "price": 0.1},
            {"target": 5, "usd": 10.0, "doge": 10.0, "sells": 3, "buys": 3, "price": 0.2},
            {"target": 8, "usd": 0.0, "doge": 500.0, "sells": 2, "buys": 2, "price": 0.1},
            {"target": 8, "usd": 500.0, "doge": 0.0, "sells": 2, "buys": 2, "price": 0.1},
        ]
        for row in cases:
            rt.target_layers = row["target"]
            with mock.patch.object(rt, "_available_free_balances", return_value=(row["usd"], row["doge"])):
                with mock.patch.object(rt, "_active_order_side_counts", return_value=(row["sells"], row["buys"], 0)):
                    metrics = rt._recompute_effective_layers(mark_price=row["price"])

            self.assertLessEqual(int(metrics["effective_layers"]), int(row["target"]))
            self.assertGreaterEqual(int(metrics["effective_layers"]), 0)
            self.assertEqual(int(rt.effective_layers), int(metrics["effective_layers"]))

    def test_alias_fallback_format_after_pool_exhaustion(self):
        rt = bot.BotRuntime()
        rt.slot_alias_pool = ("wow", "such")
        alias_1 = rt._allocate_slot_alias(used_aliases={"wow", "such"})
        alias_2 = rt._allocate_slot_alias(used_aliases={"wow", "such", "doge-01"})

        self.assertEqual(alias_1, "doge-01")
        self.assertEqual(alias_2, "doge-02")

    def test_gap_fields_non_negative_when_underfunded(self):
        rt = bot.BotRuntime()
        rt.target_layers = 5

        with mock.patch.object(rt, "_available_free_balances", return_value=(0.0, 0.0)):
            with mock.patch.object(rt, "_active_order_side_counts", return_value=(4, 3, 7)):
                metrics = rt._recompute_effective_layers(mark_price=0.1)

        self.assertGreaterEqual(int(metrics["gap_layers"]), 0)
        self.assertGreaterEqual(float(metrics["gap_doge_now"]), 0.0)
        self.assertGreaterEqual(float(metrics["gap_usd_now"]), 0.0)
        self.assertGreater(int(metrics["gap_layers"]), 0)

    def test_drip_sizing_existing_orders_unchanged(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(
                market_price=0.1,
                now=1000.0,
                orders=(
                    sm.OrderState(
                        local_id=1,
                        side="sell",
                        role="entry",
                        price=0.101,
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
                        price=0.099,
                        volume=21.0,
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
                        price=0.102,
                        volume=34.0,
                        trade_id="B",
                        cycle=1,
                        entry_price=0.1,
                        orphaned_at=900.0,
                        txid="TX-B-REC",
                    ),
                ),
            ),
        )
        rt.slots = {0: slot}
        before_orders = tuple(rt.slots[0].state.orders)
        before_recovery_orders = tuple(rt.slots[0].state.recovery_orders)

        with mock.patch.object(rt, "_safe_balance", return_value={"USD": 500.0, "DOGE": 500.0}):
            with mock.patch.object(rt, "_save_snapshot", return_value=None):
                ok, _msg = rt.add_layer("AUTO")

        self.assertTrue(ok)
        self.assertEqual(before_orders, tuple(rt.slots[0].state.orders))
        self.assertEqual(before_recovery_orders, tuple(rt.slots[0].state.recovery_orders))

    def test_layer_snapshot_round_trip(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt.last_price_ts = 1000.0
        rt.target_layers = 2
        rt.effective_layers = 2
        rt.layer_last_add_event = {
            "timestamp": 1000.0,
            "source": "AUTO",
            "price_at_commit": 0.1,
            "usd_equiv_at_commit": 22.5,
        }
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                alias="wow",
                state=sm.PairState(market_price=0.1, now=1000.0),
            )
        }
        snap = rt._global_snapshot()

        restored = bot.BotRuntime()
        restored._last_balance_snapshot = {"USD": 500.0, "DOGE": 500.0}
        restored._last_balance_ts = 1000.0
        with mock.patch("supabase_store.load_state", return_value=snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                restored._load_snapshot()

        self.assertEqual(restored.target_layers, 2)
        self.assertEqual(restored.effective_layers, 2)
        self.assertEqual(restored.layer_last_add_event, rt.layer_last_add_event)
        self.assertEqual(restored.slots[0].alias, "wow")

    def test_accumulation_snapshot_round_trip(self):
        rt = bot.BotRuntime()
        rt._accum_state = "ACTIVE"
        rt._accum_direction = "doge"
        rt._accum_trigger_from_regime = "BEARISH"
        rt._accum_trigger_to_regime = "RANGING"
        rt._accum_start_ts = 1000.0
        rt._accum_start_price = 0.1
        rt._accum_spent_usd = 4.0
        rt._accum_acquired_doge = 40.0
        rt._accum_n_buys = 2
        rt._accum_last_buy_ts = 1100.0
        rt._accum_budget_usd = 12.0
        rt._accum_armed_at = 950.0
        rt._accum_hold_streak = 1
        rt._accum_last_session_end_ts = 900.0
        rt._accum_last_session_summary = {"state": "STOPPED", "reason": "manual_stop"}
        rt._accum_manual_stop_requested = False
        rt._accum_cooldown_remaining_sec = 1800
        snap = rt._global_snapshot()

        restored = bot.BotRuntime()
        with mock.patch("supabase_store.load_state", return_value=snap):
            with mock.patch("supabase_store.load_max_event_id", return_value=0):
                restored._load_snapshot()

        self.assertEqual(restored._accum_state, "ACTIVE")
        self.assertEqual(restored._accum_direction, "doge")
        self.assertEqual(restored._accum_trigger_from_regime, "BEARISH")
        self.assertEqual(restored._accum_trigger_to_regime, "RANGING")
        self.assertAlmostEqual(float(restored._accum_spent_usd), 4.0, places=6)
        self.assertEqual(int(restored._accum_n_buys), 2)
        self.assertEqual(int(restored._accum_cooldown_remaining_sec), 1800)

    def test_zero_slots_effective_layers_safe(self):
        rt = bot.BotRuntime()
        rt.slots = {}
        rt.target_layers = 3
        rt.last_price = 0.1
        with mock.patch.object(rt, "_available_free_balances", return_value=(100.0, 1000.0)):
            metrics = rt._recompute_effective_layers(mark_price=0.1)

        self.assertEqual(int(metrics["active_sell_orders"]), 0)
        self.assertEqual(int(metrics["active_buy_orders"]), 0)
        self.assertEqual(int(metrics["open_orders_total"]), 0)
        self.assertGreaterEqual(int(metrics["effective_layers"]), 0)
        self.assertLessEqual(int(metrics["effective_layers"]), int(rt.target_layers))

    def test_slot_order_size_uses_loop_effective_layers_cache(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.2
        slot = bot.SlotRuntime(slot_id=0, state=sm.PairState(market_price=0.2, now=1000.0))
        rt.slots = {0: slot}
        rt._loop_effective_layers = {"effective_layers": 1}

        with mock.patch.object(rt, "_recompute_effective_layers") as recompute_mock:
            size_1 = rt._slot_order_size_usd(slot)
            size_2 = rt._slot_order_size_usd(slot)

        recompute_mock.assert_not_called()
        self.assertAlmostEqual(size_1, size_2, places=8)

    def test_slot_order_size_uses_price_override_for_layer_usd(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.2
        slot = bot.SlotRuntime(slot_id=0, state=sm.PairState(market_price=0.2, now=1000.0))
        rt.slots = {0: slot}
        rt._loop_effective_layers = {"effective_layers": 1}

        with mock.patch.object(config, "ORDER_SIZE_USD", 2.0):
            with mock.patch.object(config, "CAPITAL_LAYER_DOGE_PER_ORDER", 1.0):
                with_override = rt._slot_order_size_usd(slot, price_override=0.1)
                without_override = rt._slot_order_size_usd(slot)

        self.assertAlmostEqual(with_override, 2.1, places=8)
        self.assertAlmostEqual(without_override, 2.2, places=8)

    def test_count_orders_at_funded_size_uses_order_price_override(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.2
        slot = bot.SlotRuntime(
            slot_id=0,
            state=sm.PairState(
                market_price=0.2,
                now=1000.0,
                orders=(
                    sm.OrderState(
                        local_id=1,
                        side="sell",
                        role="entry",
                        price=0.11,
                        volume=13.0,
                        trade_id="A",
                        cycle=1,
                        txid="TX-A-ENTRY",
                        placed_at=999.0,
                    ),
                ),
                recovery_orders=(
                    sm.RecoveryOrder(
                        recovery_id=1,
                        side="buy",
                        price=0.09,
                        volume=13.0,
                        trade_id="B",
                        cycle=1,
                        entry_price=0.1,
                        orphaned_at=950.0,
                        txid="TX-B-REC",
                    ),
                ),
            ),
        )
        rt.slots = {0: slot}

        observed_overrides: list[float | None] = []

        def _fake_size(_slot, trade_id=None, price_override=None):
            observed_overrides.append(price_override)
            return 2.0

        with mock.patch.object(rt, "_slot_order_size_usd", side_effect=_fake_size):
            with mock.patch("bot.sm.compute_order_volume", return_value=13.0):
                matched = rt._count_orders_at_funded_size()

        self.assertEqual(matched, 2)
        self.assertIn(0.11, observed_overrides)
        self.assertIn(0.09, observed_overrides)

    def test_add_layer_rejects_at_max_target_layers(self):
        rt = bot.BotRuntime()
        rt.last_price = 0.1
        rt._last_balance_snapshot = {"USD": 1000.0, "DOGE": 1000.0}
        rt._last_balance_ts = 1000.0

        with mock.patch.object(config, "CAPITAL_LAYER_MAX_TARGET_LAYERS", 1):
            ok1, _msg1 = rt.add_layer("AUTO")
            ok2, msg2 = rt.add_layer("AUTO")

        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("target limit 1 reached", msg2)
        self.assertEqual(rt.target_layers, 1)

    def test_layer_action_in_flight_guard_rejects_parallel_action(self):
        rt = bot.BotRuntime()
        rt._layer_action_in_flight = True
        ok_add, msg_add = rt.add_layer("AUTO")
        self.assertFalse(ok_add)
        self.assertIn("already in progress", msg_add)

        rt._layer_action_in_flight = True
        ok_remove, msg_remove = rt.remove_layer()
        self.assertFalse(ok_remove)
        self.assertIn("already in progress", msg_remove)

    def test_remove_layer_message_uses_doge_per_order_multiplier(self):
        rt = bot.BotRuntime()
        rt.target_layers = 3

        with mock.patch.object(config, "CAPITAL_LAYER_DOGE_PER_ORDER", 2.5):
            with mock.patch.object(rt, "_recompute_effective_layers", return_value={"effective_layers": 0}):
                ok, msg = rt.remove_layer()

        self.assertTrue(ok)
        self.assertIn("target=2", msg)
        self.assertIn("(+5.000 DOGE/order)", msg)

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

    def test_recovery_actions_disabled_when_recovery_orders_disabled(self):
        rt = bot.BotRuntime()
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            ok_close, msg_close = rt.soft_close(1, 1)
            ok_next, msg_next = rt.soft_close_next()
            ok_stale, msg_stale = rt.cancel_stale_recoveries()
        self.assertFalse(ok_close)
        self.assertIn("RECOVERY_ORDERS_ENABLED=false", msg_close)
        self.assertFalse(ok_next)
        self.assertIn("RECOVERY_ORDERS_ENABLED=false", msg_next)
        self.assertFalse(ok_stale)
        self.assertIn("RECOVERY_ORDERS_ENABLED=false", msg_stale)

    def test_engine_cfg_disables_orphan_timers_when_recovery_orders_disabled(self):
        rt = bot.BotRuntime()
        slot = bot.SlotRuntime(slot_id=0, state=sm.PairState(market_price=0.1, now=1000.0))
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            cfg = rt._engine_cfg(slot)
        self.assertEqual(cfg.s1_orphan_after_sec, float("inf"))
        self.assertEqual(cfg.s2_orphan_after_sec, float("inf"))

    def test_collect_open_exits_excludes_recoveries_when_recovery_orders_disabled(self):
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
                            price=0.101,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            entry_filled_at=900.0,
                        ),
                    ),
                    recovery_orders=(
                        sm.RecoveryOrder(
                            recovery_id=1,
                            side="buy",
                            price=0.099,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            entry_price=0.1,
                            entry_filled_at=850.0,
                            orphaned_at=910.0,
                        ),
                    ),
                ),
            )
        }
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            rows = rt._collect_open_exits(now_ts=1000.0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_id"], "A")

    def test_cleanup_recovery_orders_on_startup_cancels_and_clears_orders(self):
        rt = bot.BotRuntime()
        rt.slots = {
            0: bot.SlotRuntime(
                slot_id=0,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    recovery_orders=(
                        sm.RecoveryOrder(
                            recovery_id=1,
                            side="buy",
                            price=0.099,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            entry_price=0.1,
                            orphaned_at=900.0,
                            txid="TX-REC-1",
                        ),
                        sm.RecoveryOrder(
                            recovery_id=2,
                            side="sell",
                            price=0.101,
                            volume=13.0,
                            trade_id="A",
                            cycle=1,
                            entry_price=0.1,
                            orphaned_at=920.0,
                        ),
                    ),
                ),
            ),
            1: bot.SlotRuntime(
                slot_id=1,
                state=sm.PairState(
                    market_price=0.1,
                    now=1000.0,
                    recovery_orders=(
                        sm.RecoveryOrder(
                            recovery_id=1,
                            side="buy",
                            price=0.098,
                            volume=13.0,
                            trade_id="B",
                            cycle=1,
                            entry_price=0.1,
                            orphaned_at=930.0,
                            txid="TX-REC-2",
                        ),
                    ),
                ),
            ),
        }
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            with mock.patch.object(rt, "_cancel_order", side_effect=[True, False]) as cancel_mock:
                cleared, cancelled, failed = rt._cleanup_recovery_orders_on_startup()

        self.assertEqual(cleared, 3)
        self.assertEqual(cancelled, 1)
        self.assertEqual(failed, 1)
        self.assertEqual(cancel_mock.call_count, 2)
        self.assertEqual(len(rt.slots[0].state.recovery_orders), 0)
        self.assertEqual(len(rt.slots[1].state.recovery_orders), 0)

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
            self.ai_override_calls = []
            self.ai_revert_calls = 0
            self.ai_dismiss_calls = 0
            self.accum_stop_calls = 0
            self.self_heal_reprice_calls = []
            self.self_heal_close_calls = []
            self.self_heal_keep_calls = []

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

        def apply_ai_regime_override(self, ttl_sec=None):
            self.ai_override_calls.append(ttl_sec)
            return True, "AI override applied"

        def revert_ai_regime_override(self):
            self.ai_revert_calls += 1
            return True, "override cancelled"

        def dismiss_ai_regime_opinion(self):
            self.ai_dismiss_calls += 1
            return True, "ai disagreement dismissed"

        def stop_accumulation(self):
            self.accum_stop_calls += 1
            return True, "accumulation stopped"

        def self_heal_reprice_breakeven(self, position_id, operator_reason=""):
            self.self_heal_reprice_calls.append((position_id, operator_reason))
            return True, "breakeven repriced"

        def self_heal_close_at_market(self, position_id, operator_reason=""):
            self.self_heal_close_calls.append((position_id, operator_reason))
            return True, "closed at market"

        def self_heal_keep_holding(self, position_id, operator_reason="", hold_sec=None):
            self.self_heal_keep_calls.append((position_id, operator_reason, hold_sec))
            return True, "hold timer reset"

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

    def test_api_action_ai_regime_override_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "ai_regime_override", "ttl_sec": 600})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.ai_override_calls, [600])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("ok"))

    def test_api_action_ai_regime_override_invalid_ttl_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "ai_regime_override", "ttl_sec": "nope"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "invalid ttl_sec"})

    def test_api_action_ai_regime_revert_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "ai_regime_revert"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.ai_revert_calls, 1)
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("ok"))

    def test_api_action_ai_regime_dismiss_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "ai_regime_dismiss"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.ai_dismiss_calls, 1)
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertIsInstance(payload, dict)
        self.assertTrue(payload.get("ok"))

    def test_api_action_accum_stop_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "accum_stop"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.accum_stop_calls, 1)
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "accumulation stopped"})

    def test_api_action_self_heal_reprice_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub(
            {"action": "self_heal_reprice_breakeven", "position_id": 47, "reason": "operator_test"}
        )

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.self_heal_reprice_calls, [(47, "operator_test")])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "breakeven repriced"})

    def test_api_action_self_heal_close_market_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub({"action": "self_heal_close_market", "position_id": 51, "reason": "write_off"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.self_heal_close_calls, [(51, "write_off")])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "closed at market"})

    def test_api_action_self_heal_keep_holding_routes_ok(self):
        runtime = self._RuntimeStub()
        bot._RUNTIME = runtime
        handler = self._HandlerStub(
            {"action": "self_heal_keep_holding", "position_id": 99, "reason": "hold", "hold_sec": 7200}
        )

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(runtime.self_heal_keep_calls, [(99, "hold", 7200.0)])
        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(payload, {"ok": True, "message": "hold timer reset"})

    def test_api_action_self_heal_invalid_position_id_returns_json_400(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "self_heal_reprice_breakeven", "position_id": "bad"})

        bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(payload, {"ok": False, "message": "invalid position_id"})

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

    def test_api_action_soft_close_rejected_when_recovery_orders_disabled(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "soft_close", "slot_id": 1, "recovery_id": 1})
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(
            payload,
            {"ok": False, "message": "soft_close disabled when RECOVERY_ORDERS_ENABLED=false"},
        )

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

    def test_api_action_cancel_stale_rejected_when_recovery_orders_disabled(self):
        bot._RUNTIME = self._RuntimeStub()
        handler = self._HandlerStub({"action": "cancel_stale_recoveries"})
        with mock.patch.object(config, "RECOVERY_ORDERS_ENABLED", False):
            bot.DashboardHandler.do_POST(handler)

        self.assertEqual(len(handler.sent), 1)
        code, payload = handler.sent[0]
        self.assertEqual(code, 400)
        self.assertEqual(
            payload,
            {"ok": False, "message": "cancel_stale_recoveries disabled when RECOVERY_ORDERS_ENABLED=false"},
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
