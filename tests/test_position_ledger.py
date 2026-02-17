import unittest

from position_ledger import PositionLedger


class PositionLedgerTests(unittest.TestCase):
    def _open_basic_position(self, ledger: PositionLedger, *, slot_id: int = 0) -> int:
        return ledger.open_position(
            slot_id=slot_id,
            trade_id="B",
            slot_mode="sticky",
            cycle=1,
            entry_data={
                "entry_price": 0.1,
                "entry_cost": 2.0,
                "entry_fee": 0.01,
                "entry_volume": 20.0,
                "entry_time": 1000.0,
                "entry_regime": "ranging",
                "entry_volatility": 0.0,
            },
            exit_data={
                "current_exit_price": 0.101,
                "original_exit_price": 0.101,
                "target_profit_pct": 1.0,
                "exit_txid": "TX-EXIT-1",
            },
        )

    def test_open_position_creates_record(self):
        ledger = PositionLedger(enabled=True, journal_local_limit=500)
        pid = self._open_basic_position(ledger)
        row = ledger.get_position(pid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["slot_mode"], "sticky")
        self.assertEqual(row["trade_id"], "B")

    def test_close_position_is_idempotent(self):
        ledger = PositionLedger(enabled=True, journal_local_limit=500)
        pid = self._open_basic_position(ledger)
        ledger.close_position(
            pid,
            {
                "exit_price": 0.101,
                "exit_cost": 2.02,
                "exit_fee": 0.01,
                "exit_time": 1100.0,
                "exit_regime": "ranging",
                "net_profit": 0.01,
                "close_reason": "filled",
            },
        )
        first_count = len(ledger.get_journal(pid))
        ledger.close_position(
            pid,
            {
                "exit_price": 0.101,
                "exit_cost": 2.02,
                "exit_fee": 0.01,
                "exit_time": 1100.0,
                "exit_regime": "ranging",
                "net_profit": 0.01,
                "close_reason": "filled",
            },
        )
        second_count = len(ledger.get_journal(pid))
        self.assertEqual(first_count, second_count)

    def test_subsidy_balance_derived_from_journal(self):
        ledger = PositionLedger(enabled=True, journal_local_limit=500)
        pid = self._open_basic_position(ledger, slot_id=3)
        ledger.journal_event(pid, "churner_profit", {"net_profit": 0.10}, timestamp=1001.0)
        ledger.journal_event(pid, "over_performance", {"excess": 0.05}, timestamp=1002.0)
        ledger.journal_event(
            pid,
            "repriced",
            {"reason": "subsidy", "subsidy_consumed": 0.08},
            timestamp=1003.0,
        )
        self.assertAlmostEqual(ledger.get_subsidy_balance(3), 0.07, places=8)

    def test_subsidy_watermark_preserved_on_trim(self):
        ledger = PositionLedger(enabled=True, journal_local_limit=3)
        pid = self._open_basic_position(ledger, slot_id=7)
        # 4 events with a limit of 3 forces one trim into watermark.
        ledger.journal_event(pid, "churner_profit", {"net_profit": 0.20}, timestamp=1001.0)
        ledger.journal_event(pid, "over_performance", {"excess": 0.10}, timestamp=1002.0)
        ledger.journal_event(
            pid,
            "repriced",
            {"reason": "subsidy", "subsidy_consumed": 0.05},
            timestamp=1003.0,
        )
        ledger.journal_event(
            pid,
            "repriced",
            {"reason": "subsidy", "subsidy_consumed": 0.10},
            timestamp=1004.0,
        )
        totals = ledger.get_subsidy_totals(slot_id=7)
        self.assertAlmostEqual(float(totals["earned"]), 0.30, places=8)
        self.assertAlmostEqual(float(totals["consumed"]), 0.15, places=8)
        self.assertAlmostEqual(float(totals["balance"]), 0.15, places=8)


if __name__ == "__main__":
    unittest.main()

