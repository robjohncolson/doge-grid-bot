import io
import unittest
from unittest import mock

import bot
import config
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
        self.assertEqual(payload["slots"][0]["s2_entered_at"], 900.0)

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

        def pause(self, _reason):
            if self.raise_on_pause:
                raise RuntimeError("boom")

        def resume(self):
            return None

        def add_slot(self):
            return True, "ok"

        def set_entry_pct(self, _value):
            return True, "ok"

        def set_profit_pct(self, _value):
            return True, "ok"

        def soft_close(self, _slot_id, _recovery_id):
            return True, "ok"

        def soft_close_next(self):
            return True, "ok"

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


if __name__ == "__main__":
    unittest.main()
