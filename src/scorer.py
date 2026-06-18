"""Детерминированный скоринг вакансий по правилам (без LLM).

Это «обычная логика» пайплайна: прозрачные веса и понятные причины.
LLM поверх этого только объясняет/выносит вердикт, но не выдумывает цифры.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .criteria import Criteria
from .loader import Vacancy

log = logging.getLogger("agent.scorer")

# Признаки того, что под вывеской junior на деле прячется middle/senior.
_SENIOR_FLAGS = ("3+", "5+", "senior", "middle", "опыт от", "лет опыта", "ведущ")


def _stem(word: str) -> str:
    """Грубый стемминг: берём корень (для рус. морфологии «аналитика/аналитику»)."""
    return word[:6] if len(word) > 6 else word


def matches_direction(vac: Vacancy, crit: Criteria) -> bool:
    """Совпадает ли направление вакансии с критериями.

    Сопоставляем не только по грубому полю `direction`, но и по тексту
    (title + description), потому что «продуктовая аналитика», «дата-инженерия»
    и «аналитика данных» в исходных данных все помечены как `data`.
    Фраза считается совпавшей, если все её значимые слова (по корню) есть в тексте.
    """
    if not crit.directions:
        return True
    hay = f"{vac.direction} {vac.title} {vac.description}".lower()
    for phrase in crit.directions:
        words = [w for w in re.split(r"[\s,/]+", phrase) if len(w) >= 4]
        if not words:
            if phrase in hay:
                return True
            continue
        if all(_stem(w) in hay for w in words):
            return True
    return False


@dataclass
class Scored:
    vacancy: Vacancy
    score: float
    matched: list[str] = field(default_factory=list)      # почему подходит
    concerns: list[str] = field(default_factory=list)     # что смущает
    breakdown: dict[str, float] = field(default_factory=dict)


def _age_days(published: str, today: date) -> int | None:
    if not published:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(published, fmt).date()
            return (today - d).days
        except ValueError:
            continue
    return None


def score_vacancy(vac: Vacancy, crit: Criteria, today: date) -> Scored:
    sc = Scored(vacancy=vac, score=0.0)
    add = sc.breakdown

    text = f"{vac.title} {vac.description}".lower()

    # --- Уровень (самый важный сигнал для junior/стажировок) ---
    if vac.level and vac.level in crit.levels:
        add["level"] = 35
        sc.matched.append(f"уровень «{vac.level}» совпадает с целевым")
    elif vac.level in ("middle", "senior", "lead"):
        add["level"] = -40
        sc.concerns.append(f"уровень «{vac.level}» выше junior — скорее всего не подойдёт")
    else:
        add["level"] = 5  # уровень не указан — нейтрально

    # Скрытый сениор в тексте даже при метке junior.
    if any(flag in text for flag in _SENIOR_FLAGS):
        add["seniority_text"] = -15
        sc.concerns.append("в описании есть требования к опыту (middle/senior по факту)")

    # --- Направление (по ключевым словам, а не только по полю direction) ---
    if crit.directions:
        if matches_direction(vac, crit):
            add["direction"] = 20
            sc.matched.append(f"направление подходит: {', '.join(crit.directions)}")
        else:
            add["direction"] = -10
            sc.concerns.append(f"направление вне критериев ({', '.join(crit.directions)})")
    else:
        add["direction"] = 8

    # --- Локация / удалёнка ---
    add["location"] = _score_location(vac, crit, sc)

    # --- Навыки ---
    add["skills"] = _score_skills(vac, crit, sc, text)

    # --- Свежесть ---
    age = _age_days(vac.published, today)
    if age is None:
        add["recency"] = 0
        sc.concerns.append("нет даты публикации")
    elif age <= crit.max_age_days:
        # чем свежее, тем больше (от 0 до 10)
        add["recency"] = round(10 * (1 - age / max(crit.max_age_days, 1)), 1)
        if age <= 3:
            sc.matched.append("свежая вакансия (≤3 дней)")
    else:
        add["recency"] = -10
        sc.concerns.append(f"старая вакансия ({age} дн., лимит {crit.max_age_days})")

    # --- Зарплата ---
    if crit.min_salary and vac.salary is not None:
        if vac.salary >= crit.min_salary:
            add["salary"] = 5
        else:
            add["salary"] = -8
            sc.concerns.append(
                f"зарплата {vac.salary} ниже желаемой {crit.min_salary}"
            )
    else:
        add["salary"] = 0

    sc.score = round(sum(add.values()), 1)
    return sc


def _city_matches(vac_city: str, cities: list[str]) -> bool:
    """Подстроковое совпадение: «москва» матчит «Город Москва», «Москва и МО» и т.п."""
    city = vac_city.lower()
    return any(c in city or city in c for c in cities if c)


def _score_location(vac: Vacancy, crit: Criteria, sc: Scored) -> float:
    is_remote = vac.remote in ("full", "partial") or "удал" in vac.city.lower()

    if crit.remote == "only":
        if vac.remote == "full":
            sc.matched.append("полная удалёнка")
            return 15
        sc.concerns.append("нужна полная удалёнка, а тут офис/гибрид")
        return -15

    if crit.remote == "no":
        # пользователю важен офис в его городе
        if crit.cities and _city_matches(vac.city, crit.cities):
            sc.matched.append(f"офис в городе «{vac.city}»")
            return 15
        return 0

    # remote == "ok": удалёнка приветствуется, город — бонус
    score = 0.0
    if is_remote:
        sc.matched.append("есть удалёнка/гибрид")
        score += 12
    if crit.cities and _city_matches(vac.city, crit.cities):
        sc.matched.append(f"подходящий город «{vac.city}»")
        score += 8
    elif crit.cities and not is_remote:
        sc.concerns.append(f"город «{vac.city or '—'}» вне списка и нет удалёнки")
        score -= 8
    return score


def _score_skills(vac: Vacancy, crit: Criteria, sc: Scored, text: str) -> float:
    if not crit.skills:
        return 0.0
    vac_skills = set(vac.skills)
    matched = []
    for skill in crit.skills:
        if skill in vac_skills or skill in text:
            matched.append(skill)
    missing = [s for s in crit.skills if s not in matched]
    coverage = len(matched) / len(crit.skills)
    points = round(25 * coverage, 1)

    if matched:
        sc.matched.append("навыки: " + ", ".join(matched))
    if missing:
        sc.concerns.append("нет в требованиях: " + ", ".join(missing))
    return points


def rank(vacancies: list[Vacancy], crit: Criteria, today: date) -> list[Scored]:
    scored = [score_vacancy(v, crit, today) for v in vacancies]
    scored.sort(key=lambda s: s.score, reverse=True)
    log.info("Отранжировано вакансий: %s", len(scored))
    return scored
