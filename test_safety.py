import asyncio
import sys
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import MT5TradingBot
from config import SUPPORTED_SYMBOLS
from risk import InstrumentSpec, RiskCalculationError, calculate_volume
from auth_service import AuthenticationError, InMemorySessionService, Principal
from connector_protocol import ConnectorMessage, ProtocolError
from pending_reconciliation import reconcile_pending_orders
from persistence import InMemoryAccountRepository
from config import agents_enabled, allowed_origins


class RiskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.spec = InstrumentSpec(
            tick_size=0.01,
            tick_value=1.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )

    def test_sizes_from_money_risk_and_caps_volume(self):
        self.assertEqual(
            calculate_volume(
                equity=10000,
                risk_percent=1,
                entry_price=2000,
                stop_loss=1990,
                instrument=self.spec,
            ),
            0.1,
        )
        self.assertEqual(
            calculate_volume(
                equity=10000,
                risk_percent=1,
                entry_price=2000,
                stop_loss=1999.997,
                instrument=self.spec,
            ),
            100.0,
        )

    def test_invalid_stop_fails_closed(self):
        with self.assertRaises(RiskCalculationError):
            calculate_volume(
                equity=10000,
                risk_percent=1,
                entry_price=2000,
                stop_loss=2000,
                instrument=self.spec,
            )


class BotSafetyTests(unittest.TestCase):
    def test_lock_blocks_pending_orders_and_lockdown_is_sticky(self):
        bot = MT5TradingBot()
        bot.system_locked = True
        with self.assertRaises(RiskCalculationError):
            asyncio.run(
                bot.add_pending_order(
                    order_type="BUY_LIMIT",
                    lot_size=0.01,
                    trigger_price=100,
                )
            )

        bot.system_locked = False
        asyncio.run(bot.emergency_lockdown())
        self.assertTrue(bot.system_locked)

    def test_usoil_has_simulation_fallback_quote(self):
        bot = MT5TradingBot()
        self.assertEqual(bot.get_symbol_point("USOIL"), 0.01)
        self.assertEqual(bot.get_symbol_multiplier("USOIL"), 100.0)


class ConfigurationTests(unittest.TestCase):
    def test_supported_symbols_are_explicit(self):
        self.assertEqual(SUPPORTED_SYMBOLS, frozenset({"XAUUSD", "USOIL", "EURUSD", "GBPUSD"}))

    def test_cors_and_agents_are_fail_closed(self):
        old_origins = os.environ.pop("ALLOWED_ORIGINS", None)
        old_agents = os.environ.pop("ENABLE_SDLC_AGENTS", None)
        try:
            self.assertEqual(allowed_origins(), ["http://127.0.0.1:8000"])
            self.assertFalse(agents_enabled())
        finally:
            if old_origins is not None:
                os.environ["ALLOWED_ORIGINS"] = old_origins
            if old_agents is not None:
                os.environ["ENABLE_SDLC_AGENTS"] = old_agents


class AccountIsolationTests(unittest.TestCase):
    def test_account_states_are_isolated(self):
        repository = InMemoryAccountRepository()
        first = repository.get("account-a")
        first.pending_orders[1] = {"ticket": 1}
        repository.save(first)
        self.assertEqual(repository.get("account-b").pending_orders, {})
        with self.assertRaises(ValueError):
            repository.get("")

    def test_session_requires_account_scope_and_expires_on_revoke(self):
        service = InMemorySessionService()
        session = service.create(Principal("account-a", "user-a"))
        self.assertEqual(service.authenticate(session.token).account_id, "account-a")
        service.revoke(session.token)
        with self.assertRaises(AuthenticationError):
            service.authenticate(session.token)


class ConnectorContractTests(unittest.TestCase):
    def test_protocol_round_trip_preserves_scope(self):
        message = ConnectorMessage("pending.snapshot", "account-a", "connector-a", "request-a", {"orders": []})
        decoded = ConnectorMessage.from_json(message.to_json())
        self.assertEqual(decoded.account_id, "account-a")

    def test_protocol_rejects_unscoped_message(self):
        with self.assertRaises(ProtocolError):
            ConnectorMessage("order.submit", "", "connector-a", "request-a", {})

    def test_reconciliation_is_account_scoped(self):
        result = reconcile_pending_orders(
            {1: {"ticket": 1}, 2: {"ticket": 2}},
            [{"ticket": 1, "account_id": "account-a"}, {"ticket": 3, "account_id": "account-b"}],
            account_id="account-a",
        )
        self.assertEqual(set(result.active), {1})
        self.assertEqual(result.disappeared, (2,))
        self.assertEqual(result.broker_only, ())


if __name__ == "__main__":
    unittest.main()