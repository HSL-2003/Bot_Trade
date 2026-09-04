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
from core.risk import InstrumentSpec, RiskCalculationError, calculate_volume
from services.auth_service import AuthenticationError, InMemorySessionService, Principal, SessionService
from services.auth_dependencies import (
    get_bearer_token, get_current_principal, get_current_principal_optional,
    get_account_id, require_admin, authenticate_websocket, set_session_cookie,
)
from connectors.connector_protocol import ConnectorMessage, ProtocolError
from connectors.pending_reconciliation import reconcile_pending_orders
from repositories.persistence import InMemoryAccountRepository
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

    def test_cors_wildcard_is_rejected_with_credentials(self):
        # A "*" origin must never pair with allow_credentials=True; fail closed.
        old_origins = os.environ.get("ALLOWED_ORIGINS", None)
        try:
            os.environ["ALLOWED_ORIGINS"] = "*"
            with self.assertRaises(RuntimeError):
                allowed_origins()
            # Wildcard mixed with an explicit list is filtered down to the list.
            os.environ["ALLOWED_ORIGINS"] = "*,http://example.com"
            self.assertEqual(allowed_origins(), ["http://example.com"])
        finally:
            if old_origins is not None:
                os.environ["ALLOWED_ORIGINS"] = old_origins
            else:
                os.environ.pop("ALLOWED_ORIGINS", None)


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


class AuthDisplayNameTests(unittest.TestCase):
    TEST_EMAILS = (
        "sectest@example.com",
        "smoke@example.com",
        "hdr@example.com",
        "hdr2@example.com",
    )

    def test_register_without_display_name_uses_full_email(self):
        service = InMemorySessionService()
        for email in self.TEST_EMAILS:
            with self.subTest(email=email):
                service.register(email, "StrongPass1!")
                self.assertEqual(service._users[email]["display_name"], email)

    def test_register_with_display_name_keeps_custom_name(self):
        service = InMemorySessionService()
        service.register("sectest@example.com", "StrongPass1!", display_name="Sec Test")
        self.assertEqual(service._users["sectest@example.com"]["display_name"], "Sec Test")


class AuthSocialLoginTests(unittest.TestCase):
    def test_magic_link_not_supported_in_dev_mode(self):
        service = InMemorySessionService()
        with self.assertRaises(AuthenticationError):
            service.magic_link("sectest@example.com")

    def test_oauth_token_exchange_not_supported_in_dev_mode(self):
        service = InMemorySessionService()
        with self.assertRaises(AuthenticationError):
            service.login_with_supabase_token("dummy-supabase-token")


class AuthDependencyTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        self.service = InMemorySessionService()
        self.app = FastAPI()
        self.app.state.session_service = self.service
        self.request = type("Request", (), {
            "app": self.app, "cookies": {}, "headers": {},
            "query_params": {}, "state": type("State", (), {})(),
            "client": None,
        })()

    def test_session_service_protocol(self):
        self.assertIsInstance(self.service, SessionService)

    def test_bearer_token_header_and_cookie(self):
        from fastapi.security import HTTPAuthorizationCredentials
        self.request.headers = {"Authorization": "Bearer abc"}
        token = asyncio.run(get_bearer_token(self.request, HTTPAuthorizationCredentials(scheme="Bearer", credentials="abc")))
        self.assertEqual(token, "abc")
        self.request.headers = {}
        self.request.cookies = {"session_token": "cookie-token"}
        self.assertEqual(asyncio.run(get_bearer_token(self.request, None)), "cookie-token")

    def test_bearer_token_requires_authentication(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_bearer_token(self.request, None))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_principal_dependencies_and_account_scope(self):
        session = self.service.create(Principal("acc-2", "user-2"))
        principal = asyncio.run(get_current_principal(session.token, self.service))
        self.assertEqual(principal.account_id, "acc-2")
        self.request.headers = {"Authorization": f"Bearer {session.token}"}
        self.request.query_params = {}
        self.assertEqual(asyncio.run(get_current_principal_optional(self.request, self.service)).user_id, "user-2")
        self.assertEqual(asyncio.run(get_account_id(self.request, principal)), "acc-2")
        self.request.headers = {"Authorization": f"Bearer {session.token}", "X-Account-Id": "other"}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_account_id(self.request, principal))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_dependency_and_websocket_auth(self):
        session = self.service.create(Principal("admin-acc", "admin-user", frozenset({"admin"})))
        self.request.client = None
        principal = asyncio.run(get_current_principal(session.token, self.service))
        self.assertEqual(asyncio.run(require_admin(self.request, principal)), principal)
        self.assertEqual(authenticate_websocket(session.token, self.service), principal)


class AuthOAuthEndpointTests(unittest.TestCase):
    def test_google_endpoint_redirects_to_supabase(self):
        from fastapi.testclient import TestClient
        import app as module
        client = TestClient(module.app)
        old_url = os.environ.get("SUPABASE_URL")
        os.environ["SUPABASE_URL"] = "https://example.supabase.co"
        try:
            resp = client.get("/api/auth/google", follow_redirects=False)
        finally:
            if old_url is None:
                os.environ.pop("SUPABASE_URL", None)
            else:
                os.environ["SUPABASE_URL"] = old_url
        self.assertEqual(resp.status_code, 307)
        location = resp.headers.get("location", "")
        self.assertIn("provider=google", location)
        self.assertIn("/auth/v1/authorize", location)
        self.assertIn("redirect_to=", location)

    def test_auth_endpoint_contracts(self):
        from fastapi.testclient import TestClient
        import uuid
        import app as module
        client = TestClient(module.app)
        email = f"auth_{uuid.uuid4().hex[:10]}@example.com"
        registered = client.post("/api/auth/register", json={"email": email, "password": "StrongPassword123!"})
        self.assertEqual(registered.status_code, 201)
        token = registered.json()["access_token"]
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 200)
        logged_in = client.post("/api/auth/login", json={"email": email, "password": "StrongPassword123!"})
        self.assertEqual(logged_in.status_code, 200)
        logged_token = logged_in.json()["access_token"]
        self.assertEqual(client.post("/api/auth/logout", headers={"Authorization": f"Bearer {logged_token}"}).status_code, 204)
        self.assertEqual(client.get("/api/auth/me", headers={"Authorization": f"Bearer {logged_token}"}).status_code, 401)

    def test_magic_link_and_social_callback_contracts(self):
        from fastapi.testclient import TestClient
        import app as module
        client = TestClient(module.app)
        magic = client.post("/api/auth/magic-link", json={"email": "auth@example.com"})
        social = client.post("/api/auth/social/callback", json={"token": "invalid"})
        self.assertEqual(magic.status_code, 400)
        self.assertEqual(social.status_code, 401)


class UserTradePersistenceTests(unittest.TestCase):
    def test_in_memory_repository_records_open_and_close_per_user(self):
        repo = InMemoryAccountRepository()
        
        # User 1 opens a trade
        open_data = {
            "ticket": 1001,
            "symbol": "XAUUSD",
            "type": "BUY",
            "volume": 0.05,
            "open_price": 2050.0,
            "sl": 2045.0,
            "tp": 2065.0,
        }
        opened = repo.record_trade_open("acc-1", "user-1", open_data)
        self.assertEqual(opened["status"], "filled")
        self.assertEqual(opened["broker_ticket"], 1001)
        self.assertEqual(opened["quantity"], 0.05)

        # User 2 opens another trade
        open_data_2 = {
            "ticket": 1002,
            "symbol": "EURUSD",
            "type": "SELL",
            "volume": 0.1,
            "open_price": 1.0850,
            "sl": 1.0900,
            "tp": 1.0700,
        }
        repo.record_trade_open("acc-2", "user-2", open_data_2)

        # Check isolation: user-1 sees only their trade
        user_1_trades = repo.get_user_trades("acc-1", "user-1")
        self.assertEqual(len(user_1_trades), 1)
        self.assertEqual(user_1_trades[0]["broker_ticket"], 1001)

        user_2_trades = repo.get_user_trades("acc-2", "user-2")
        self.assertEqual(len(user_2_trades), 1)
        self.assertEqual(user_2_trades[0]["broker_ticket"], 1002)

        # Close user 1's trade
        close_info = {
            "symbol": "XAUUSD",
            "type": "BUY",
            "volume": 0.05,
            "open_price": 2050.0,
            "close_price": 2060.0,
            "profit": 50.0,
        }
        closed = repo.record_trade_close("acc-1", "user-1", 1001, close_info)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["profit"], 50.0)
        self.assertEqual(closed["close_price"], 2060.0)

        # Verify updated status
        updated = repo.get_user_trades("acc-1", "user-1")
        self.assertEqual(updated[0]["status"], "closed")
        self.assertEqual(updated[0]["profit"], 50.0)

    def test_get_user_trades_endpoint(self):
        import uuid
        from fastapi.testclient import TestClient
        import app as module
        client = TestClient(module.app)
        
        # Register a test user with unique email
        test_email = f"trade_{uuid.uuid4().hex[:8]}@example.com"
        resp = client.post("/api/auth/register", json={"email": test_email, "password": "StrongPassword123!"})
        self.assertIn(resp.status_code, (200, 201))
        token = resp.json().get("access_token")
        self.assertIsNotNone(token)
        
        # Query user trade history with bearer token
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.get("/api/user/trades", headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("trades", data)
        self.assertIn("total_trades", data)
        self.assertIn("win_rate", data)
        self.assertIn("total_profit", data)

        # Test /api/history/analytics endpoint
        analytics_resp = client.get("/api/history/analytics?period=all", headers=headers)
        self.assertEqual(analytics_resp.status_code, 200)
        analytics_data = analytics_resp.json()
        self.assertIn("gross_profit", analytics_data)
        self.assertIn("gross_loss", analytics_data)
        self.assertIn("net_profit", analytics_data)
        self.assertIn("total_pips", analytics_data)
        self.assertIn("win_rate", analytics_data)
        self.assertIn("trades", analytics_data)

        # Test /api/dashboard/data endpoint
        dash_resp = client.get("/api/dashboard/data?days=30", headers=headers)
        self.assertEqual(dash_resp.status_code, 200)
        dash_data = dash_resp.json()
        self.assertIn("summary", dash_data)
        self.assertIn("points", dash_data)
        self.assertIn("trades", dash_data)

    def test_close_position_resolves_valid_close_price_and_profit(self):
        bot = MT5TradingBot()
        bot.simulation_mode = True
        bot.history = []
        bot.positions = []
        
        # Add a simulated position for EURUSD
        pos = {
            "ticket": 999123,
            "symbol": "EURUSD",
            "type": "BUY",
            "volume": 0.30,
            "open_price": 1.16884,
            "current_price": 1.16950,
            "sl": 1.16500,
            "tp": 1.17500,
            "profit": 0.0,
            "open_time": "2026-08-24T10:00:00"
        }
        bot.positions.append(pos)
        
        recorded_close = {}
        def mock_on_close(acc, usr, ticket, info):
            recorded_close.update(info)
        bot.on_trade_close = mock_on_close
        
        # Close position
        asyncio.run(bot.close_position(999123))
        
        self.assertEqual(len(bot.positions), 0)
        self.assertEqual(len(bot.history), 1)
        closed_trade = bot.history[0]
        
        # Assert close_price is positive and valid (not 0.0)
        self.assertGreater(closed_trade["close_price"], 1.0)
        self.assertGreater(recorded_close.get("close_price", 0.0), 1.0)
        self.assertEqual(closed_trade["ticket"], 999123)
        self.assertEqual(closed_trade["symbol"], "EURUSD")

    def test_modify_position_sltp_and_calculate_trade_pips(self):
        bot = MT5TradingBot()
        bot.simulation_mode = True
        pos = {
            "ticket": 888777,
            "symbol": "XAUUSD",
            "type": "BUY",
            "volume": 0.10,
            "open_price": 2650.00,
            "current_price": 2655.00,
            "sl": 2640.00,
            "tp": 2670.00,
            "profit": 50.0,
            "open_time": "2026-08-24T10:00:00"
        }
        bot.positions.append(pos)
        
        # Test modifying SL/TP
        success = asyncio.run(bot.modify_position_sltp(888777, 2645.50, 2680.00))
        self.assertTrue(success)
        self.assertEqual(pos["sl"], 2645.50)
        self.assertEqual(pos["tp"], 2680.00)
        
        # Test calculate_trade_pips
        trade_buy = {"symbol": "XAUUSD", "type": "BUY", "open_price": 2650.00, "close_price": 2655.00}
        pips_buy = bot.calculate_trade_pips(trade_buy)
        self.assertEqual(pips_buy, 50.0)
        
        trade_sell = {"symbol": "XAUUSD", "type": "SELL", "open_price": 2650.00, "close_price": 2645.00}
        pips_sell = bot.calculate_trade_pips(trade_sell)
        self.assertEqual(pips_sell, 50.0)

    def test_modify_sltp_endpoint(self):
        from app import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        
        # Call modify-sltp API with headers to pass CSRF
        resp = client.post(
            "/api/modify-sltp/777666",
            json={"sl": 1.16250, "tp": 1.18500},
            headers={"Authorization": "Bearer test-token", "Origin": "http://localhost:8000"}
        )
        # Position not found on non-mocked scoped bot or unauthenticated request returns 400/401
        self.assertIn(resp.status_code, [200, 400, 401])


class AuthDependencyCoverageTests(unittest.TestCase):
    """Branch coverage for services/auth_dependencies.py (Plan-2 clean-up)."""

    def setUp(self):
        from fastapi import FastAPI
        self.service = InMemorySessionService()
        self.app = FastAPI()
        self.app.state.session_service = self.service
        self.request = type("Request", (), {
            "app": self.app,
            "cookies": {},
            "headers": {},
            "query_params": {},
            "state": type("State", (), {})(),
            "client": None,
            "url": type("URL", (), {"scheme": "http"})(),
        })()

    def tearDown(self):
        os.environ.pop("REQUIRE_AUTH", None)
        os.environ.pop("ENVIRONMENT", None)
        os.environ.pop("ADMIN_ACCOUNT_IDS", None)

    def test_account_id_defaults_without_token_in_development(self):
        default_account = os.getenv("DEFAULT_ACCOUNT_ID", "demo-account")
        self.assertEqual(asyncio.run(get_account_id(self.request, None)), default_account)

    def test_account_id_honors_scope_hint_without_token(self):
        self.request.query_params = {"account_id": "acc-9"}
        self.assertEqual(asyncio.run(get_account_id(self.request, None)), "acc-9")

    def test_account_id_requires_token_when_auth_enforced(self):
        from fastapi import HTTPException
        os.environ["REQUIRE_AUTH"] = "true"
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_account_id(self.request, None))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_admin_dependency_blocks_plain_trader(self):
        from fastapi import HTTPException
        principal = Principal("acc-1", "user-1", frozenset({"trader"}))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(require_admin(self.request, principal))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_dependency_allows_allowlisted_account(self):
        os.environ["ADMIN_ACCOUNT_IDS"] = "acc-x, acc-y"
        principal = Principal("acc-y", "user-1", frozenset({"trader"}))
        self.assertIs(asyncio.run(require_admin(self.request, principal)), principal)

    def test_session_cookie_is_httponly_and_strict(self):
        from fastapi import Response
        response = Response()
        set_session_cookie(response, {"access_token": "tok-1"}, self.request)
        cookie_header = response.headers.get("set-cookie", "")
        self.assertIn("session_token=tok-1", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("samesite=strict", cookie_header.lower())
        self.assertIn("Path=/", cookie_header)

    def test_websocket_auth_rejects_unknown_token(self):
        with self.assertRaises(AuthenticationError):
            authenticate_websocket("not-a-real-token", self.service)

    def test_optional_principal_returns_none_without_token(self):
        self.assertIsNone(asyncio.run(get_current_principal_optional(self.request, self.service)))


if __name__ == "__main__":
    unittest.main()