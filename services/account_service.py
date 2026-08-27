"""Account-scoped bot sessions and trading facade."""

from dataclasses import dataclass
from typing import Optional
from bot import MT5TradingBot
from repositories.persistence import AccountRepository


@dataclass
class BotSession:
    account_id: str
    bot: MT5TradingBot


class TradingAccountService:
    def __init__(self, repository: AccountRepository):
        self.repository = repository
        self._sessions: dict[str, BotSession] = {}

    def _wire_bot_callbacks(self, account_id: str, bot_instance: MT5TradingBot, user_id: Optional[str] = None):
        bot_instance.account_id = account_id
        if user_id:
            bot_instance.user_id = user_id
        
        if hasattr(self.repository, "record_trade_open"):
            bot_instance.on_trade_open = lambda acc, usr, trade: self.repository.record_trade_open(acc, usr, trade)
        if hasattr(self.repository, "record_trade_close"):
            bot_instance.on_trade_close = lambda acc, usr, ticket, info: self.repository.record_trade_close(acc, usr, ticket, info)
        if hasattr(self.repository, "set_lock_state"):
            bot_instance.on_lock_change = lambda acc, lock_state, reason=None: self.repository.set_lock_state(acc, lock_state, reason)

    def register_bot(self, account_id: str, bot_instance: MT5TradingBot, user_id: Optional[str] = None) -> BotSession:
        if not account_id or not account_id.strip():
            raise ValueError("Account scope is required")
        account_id = account_id.strip()
        self._wire_bot_callbacks(account_id, bot_instance, user_id)
        session = BotSession(account_id, bot_instance)
        self._sessions[account_id] = session
        return session

    def get_session(self, account_id: str, user_id: Optional[str] = None) -> BotSession:
        if not account_id or not account_id.strip():
            raise ValueError("Account scope is required")
        account_id = account_id.strip()
        if account_id in self._sessions:
            session = self._sessions[account_id]
            if user_id:
                session.bot.user_id = user_id
            return session
        try:
            self.repository.get(account_id)
        except Exception:
            pass
        if account_id not in self._sessions:
            new_bot = MT5TradingBot()
            self._wire_bot_callbacks(account_id, new_bot, user_id)
            # Re-apply any persisted (Supabase) lock state so a soft/hard lock
            # survives a process restart.
            try:
                lock_info = self.repository.get_lock_state(account_id)
                new_bot.lock_state = lock_info.get("lock_state") or "unlocked"
                new_bot.lock_reason = lock_info.get("lock_reason")
            except Exception:
                pass
            self._sessions[account_id] = BotSession(account_id, new_bot)
        return self._sessions[account_id]

    def get_bot(self, account_id: str, user_id: Optional[str] = None) -> MT5TradingBot:
        return self.get_session(account_id, user_id).bot

    def active_sessions(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            if session.bot.is_running:
                await session.bot.stop()