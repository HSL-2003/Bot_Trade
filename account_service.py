"""Account-scoped bot sessions and trading facade."""

from dataclasses import dataclass
from typing import Optional
from bot import MT5TradingBot
from persistence import AccountRepository


@dataclass
class BotSession:
    account_id: str
    bot: MT5TradingBot


class TradingAccountService:
    def __init__(self, repository: AccountRepository):
        self.repository = repository
        self._sessions: dict[str, BotSession] = {}

    def get_session(self, account_id: str) -> BotSession:
        if not account_id or not account_id.strip():
            raise ValueError("Account scope is required")
        account_id = account_id.strip()
        self.repository.get(account_id)
        if account_id not in self._sessions:
            self._sessions[account_id] = BotSession(account_id, MT5TradingBot())
        return self._sessions[account_id]

    def get_bot(self, account_id: str) -> MT5TradingBot:
        return self.get_session(account_id).bot

    def active_sessions(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    async def shutdown(self) -> None:
        for session in self._sessions.values():
            if session.bot.is_running:
                await session.bot.stop()