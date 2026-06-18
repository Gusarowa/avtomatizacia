"""Общие помощники хранения JSON-данных на диске.

Каталог данных берётся из переменной окружения AGENT_DATA_DIR (по умолчанию
«data»). На хостингах с эфемерной файловой системой (Railway, Koyeb) сюда
монтируют постоянный том (volume), чтобы анкеты и подписки переживали редеплой.

Запись атомарная: пишем во временный файл рядом и заменяем целевой через
os.replace — так файл не бьётся, если процесс упал на середине записи.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

log = logging.getLogger("agent.storage")


def data_dir() -> str:
    return os.environ.get("AGENT_DATA_DIR", "data")


def data_path(name: str) -> str:
    return os.path.join(data_dir(), name)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or default
    except (json.JSONDecodeError, OSError) as e:
        log.error("Не удалось прочитать %s: %s", path, e)
        return default


def save_json(path: str, payload) -> bool:
    """Атомарно сохраняет JSON. True при успехе."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True
    except OSError as e:
        log.error("Не удалось сохранить %s: %s", path, e)
        return False
