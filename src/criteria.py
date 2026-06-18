"""Чтение критериев пользователя из criteria.md (или значения по умолчанию)."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger("agent.criteria")

_KNOWN_KEYS = {
    "level", "direction", "city", "remote", "skills", "max_age_days", "min_salary",
}


@dataclass
class Criteria:
    levels: list[str] = field(default_factory=lambda: ["junior", "intern"])
    directions: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    remote: str = "ok"            # ok | no | only
    skills: list[str] = field(default_factory=list)
    max_age_days: int = 60
    min_salary: int | None = None
    free_text: str = ""           # свободный комментарий для LLM


def _split_list(value: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[,/;]", value) if p.strip()]


def load_criteria(path: str | None) -> Criteria:
    """Парсит markdown вида `- key: value`. На отсутствие файла — дефолты."""
    crit = Criteria()
    if not path or not os.path.exists(path):
        log.warning("criteria.md не найден (%s) — использую значения по умолчанию", path)
        return crit

    free_lines: list[str] = []
    in_free_block = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.error("Не удалось прочитать критерии: %s", e)
        return crit

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## свободный") or stripped.lower().startswith("## free"):
            in_free_block = True
            continue
        if in_free_block:
            if stripped:
                free_lines.append(stripped)
            continue

        m = re.match(r"^[-*]?\s*([a-zA-Zа-яА-Я_]+)\s*:\s*(.+?)\s*$", stripped)
        if not m:
            continue
        key = m.group(1).strip().lower()
        # убираем inline-комментарии вида "ok   # пояснение"
        value = m.group(2).split("#", 1)[0].strip()
        if key not in _KNOWN_KEYS or not value:
            continue

        if key == "level":
            crit.levels = _split_list(value)
        elif key == "direction":
            crit.directions = _split_list(value)
        elif key == "city":
            crit.cities = _split_list(value)
        elif key == "remote":
            crit.remote = value.lower()
        elif key == "skills":
            crit.skills = _split_list(value)
        elif key == "max_age_days":
            crit.max_age_days = _safe_int(value, crit.max_age_days)
        elif key == "min_salary":
            crit.min_salary = _safe_int(value, None)

    crit.free_text = " ".join(free_lines).strip()
    log.info(
        "Критерии: levels=%s, directions=%s, cities=%s, remote=%s, skills=%s, max_age=%s, min_salary=%s",
        crit.levels, crit.directions, crit.cities, crit.remote,
        crit.skills, crit.max_age_days, crit.min_salary,
    )
    return crit


def _safe_int(value: str, default):
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else default
