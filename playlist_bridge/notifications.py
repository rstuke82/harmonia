"""Generic webhook notification support for Harmonia 2.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class WebhookSettings:
    enabled: bool = False
    url: str = ""
    notify_sync_failures: bool = True
    notify_lost_matches: bool = True
    notify_unresolved: bool = True
    notify_source_changes: bool = True
    notify_successful_syncs: bool = False
    notify_sync_all_summary: bool = True

    @classmethod
    def from_dict(cls, value: Optional[dict]) -> "WebhookSettings":
        value = value if isinstance(value, dict) else {}
        allowed = {
            field_name
            for field_name in cls.__dataclass_fields__
        }
        return cls(
            **{
                key: value[key]
                for key in allowed
                if key in value
            }
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }


class WebhookNotifier:
    """Send generic JSON webhook payloads without coupling to one service."""

    def __init__(self, settings: WebhookSettings):
        self.settings = settings

    def send(
        self,
        event: str,
        message: str,
        data: Optional[dict] = None,
    ) -> bool:
        if not self.settings.enabled or not self.settings.url:
            return False

        response = requests.post(
            self.settings.url,
            json={
                "application": "Harmonia",
                "event": event,
                "message": message,
                "data": data or {},
            },
            timeout=15,
        )
        response.raise_for_status()
        return True
