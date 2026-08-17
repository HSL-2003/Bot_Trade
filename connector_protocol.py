"""Versioned cloud-to-Windows-MT5 connector message contract.

This is only serialization/validation. The cloud must not execute MT5 calls.
"""

from dataclasses import dataclass
from typing import Any
import json


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectorMessage:
    message_type: str
    account_id: str
    connector_id: str
    request_id: str
    payload: dict[str, Any]
    version: int = 1

    def __post_init__(self):
        if self.version != 1 or not all((self.account_id, self.connector_id, self.request_id)):
            raise ProtocolError("Version 1 messages require account, connector and request scopes")
        if not self.message_type or not isinstance(self.payload, dict):
            raise ProtocolError("Message type and object payload are required")

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "ConnectorMessage":
        try:
            data = json.loads(raw)
            return cls(**data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProtocolError("Invalid connector message") from exc