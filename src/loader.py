"""Загрузка и нормализация входных данных.

Главная задача модуля — НЕ упасть на пустом или битом файле: всё, что нельзя
прочитать, аккуратно пропускается и попадает в статистику, а не роняет процесс.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("agent.loader")


@dataclass
class Vacancy:
    id: str
    title: str
    company: str = ""
    direction: str = ""
    level: str = ""
    city: str = ""
    remote: str = ""          # full | partial | no
    published: str = ""       # YYYY-MM-DD
    salary: int | None = None
    url: str = ""
    skills: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class LoadStats:
    total_rows: int = 0
    loaded: int = 0
    skipped_broken: int = 0
    duplicates: int = 0
    problems: list[str] = field(default_factory=list)


def _parse_skills(raw: Any) -> list[str]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = raw.replace(";", ",").split(",")
    else:
        items = []
    return [s.strip().lower() for s in items if str(s).strip()]


def _parse_salary(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        digits = "".join(ch for ch in str(raw) if ch.isdigit())
        return int(digits) if digits else None
    except (ValueError, TypeError):
        return None


def _row_to_vacancy(row: dict[str, Any]) -> Vacancy | None:
    """Превращает словарь в Vacancy. Возвращает None, если строка бесполезна."""
    title = str(row.get("title") or "").strip()
    if not title:
        # Без заголовка вакансию невозможно ни показать, ни оценить.
        return None

    vid = str(row.get("id") or "").strip()
    if not vid:
        vid = str(abs(hash(title + str(row.get("company", "")))) % 10_000_000)

    return Vacancy(
        id=vid,
        title=title,
        company=str(row.get("company") or "").strip(),
        direction=str(row.get("direction") or "").strip().lower(),
        level=str(row.get("level") or "").strip().lower(),
        city=str(row.get("city") or "").strip(),
        remote=str(row.get("remote") or "").strip().lower(),
        published=str(row.get("published") or "").strip(),
        salary=_parse_salary(row.get("salary")),
        url=str(row.get("url") or "").strip(),
        skills=_parse_skills(row.get("skills")),
        description=str(row.get("description") or "").strip(),
    )


def _read_raw(path: str) -> list[dict[str, Any]]:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "r", encoding="utf-8") as f:
        if ext == ".json":
            data = json.load(f)
            if isinstance(data, dict):
                # Допускаем {"vacancies": [...]} и просто [...]
                data = data.get("vacancies") or data.get("items") or [data]
            if not isinstance(data, list):
                raise ValueError("JSON должен быть списком вакансий")
            return [r for r in data if isinstance(r, dict)]
        # по умолчанию CSV
        return list(csv.DictReader(f))


def load_vacancies(path: str) -> tuple[list[Vacancy], LoadStats]:
    """Читает CSV/JSON, нормализует, дедуплицирует. Никогда не бросает наружу."""
    stats = LoadStats()

    if not path or not os.path.exists(path):
        stats.problems.append(f"Файл не найден: {path!r}")
        log.error("Файл не найден: %s", path)
        return [], stats

    if os.path.getsize(path) == 0:
        stats.problems.append("Файл пустой")
        log.error("Файл пустой: %s", path)
        return [], stats

    try:
        raw_rows = _read_raw(path)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        stats.problems.append(f"Не удалось разобрать файл: {e}")
        log.error("Ошибка парсинга %s: %s", path, e)
        return [], stats

    stats.total_rows = len(raw_rows)
    seen: set[tuple[str, str]] = set()
    vacancies: list[Vacancy] = []

    for i, row in enumerate(raw_rows, start=1):
        try:
            vac = _row_to_vacancy(row)
        except Exception as e:  # на случай совсем неожиданной строки
            stats.skipped_broken += 1
            stats.problems.append(f"Строка {i}: {e}")
            log.warning("Строка %s пропущена: %s", i, e)
            continue

        if vac is None:
            stats.skipped_broken += 1
            stats.problems.append(f"Строка {i}: нет заголовка вакансии")
            log.warning("Строка %s пропущена: нет заголовка", i)
            continue

        # Дедупликация по (нормализованный заголовок, компания) или url.
        key = (vac.title.lower(), vac.company.lower())
        url_key = (vac.url.lower(), "") if vac.url else None
        if key in seen or (url_key and url_key in seen):
            stats.duplicates += 1
            log.info("Дубль пропущен: %s @ %s", vac.title, vac.company)
            continue
        seen.add(key)
        if url_key:
            seen.add(url_key)

        vacancies.append(vac)
        stats.loaded += 1

    log.info(
        "Загрузка завершена: всего=%s, ок=%s, битых=%s, дублей=%s",
        stats.total_rows, stats.loaded, stats.skipped_broken, stats.duplicates,
    )
    return vacancies, stats
