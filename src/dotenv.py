"""Минимальный загрузчик .env (без зависимостей)."""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    """Грузит переменные из .env. Не перезатирает уже заданные в окружении."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass
