"""Подписки на ежедневный дайджест вакансий.

Состояние по chat_id хранится в subscriptions.json в каталоге данных:

    {
      "<chat_id>": {
        "enabled": true,
        "last_run": 1718000000.0,   # когда последний раз слали дайджест
        "seen": ["<key>", ...]       # ключи уже показанных вакансий (антидубль)
      }
    }

Ключ вакансии — её URL, а если его нет — «title|company». Список seen
ограничен сверху, чтобы файл не разрастался.
"""
from __future__ import annotations

import logging
import time

from .storage import data_path, load_json, save_json

log = logging.getLogger("agent.subscriptions")

SUBS_FILE = "subscriptions.json"
SEEN_CAP = 300  # сколько ключей вакансий помним на пользователя


def _load_all() -> dict:
    return load_json(data_path(SUBS_FILE), {})


def _save_all(data: dict) -> bool:
    return save_json(data_path(SUBS_FILE), data)


def _entry(data: dict, chat_id: int | str) -> dict:
    return data.setdefault(str(chat_id), {"enabled": False, "last_run": 0.0, "seen": []})


def is_enabled(chat_id: int | str) -> bool:
    return bool(_load_all().get(str(chat_id), {}).get("enabled"))


def set_enabled(chat_id: int | str, enabled: bool) -> None:
    data = _load_all()
    e = _entry(data, chat_id)
    e["enabled"] = enabled
    _save_all(data)
    log.info("Подписка chat_id=%s enabled=%s", chat_id, enabled)


def enabled_chat_ids() -> list[str]:
    return [cid for cid, e in _load_all().items() if e.get("enabled")]


def get_state(chat_id: int | str) -> dict:
    return _load_all().get(str(chat_id), {"enabled": False, "last_run": 0.0, "seen": []})


def due_for_digest(chat_id: int | str, interval_sec: float, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    last = float(_load_all().get(str(chat_id), {}).get("last_run", 0.0))
    return (now - last) >= interval_sec


def vacancy_key(vac) -> str:
    """Стабильный ключ вакансии для дедупликации в дайджесте."""
    url = (getattr(vac, "url", "") or "").strip()
    if url:
        return url
    title = (getattr(vac, "title", "") or "").strip().lower()
    company = (getattr(vac, "company", "") or "").strip().lower()
    return f"{title}|{company}"


def filter_new(chat_id: int | str, keys: list[str]) -> list[str]:
    """Возвращает только те ключи, которых ещё не показывали этому пользователю."""
    seen = set(_load_all().get(str(chat_id), {}).get("seen", []))
    return [k for k in keys if k not in seen]


def mark_run(chat_id: int | str, new_keys: list[str], now: float | None = None) -> None:
    """Фиксирует факт запуска дайджеста и добавляет показанные ключи в seen."""
    now = now if now is not None else time.time()
    data = _load_all()
    e = _entry(data, chat_id)
    e["last_run"] = now
    if new_keys:
        seen = e.get("seen", [])
        seen.extend(k for k in new_keys if k not in seen)
        e["seen"] = seen[-SEEN_CAP:]
    _save_all(data)
