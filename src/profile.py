"""Анкета кандидата (профиль) и её хранение.

Профиль заполняется через Telegram-бота и сохраняется по chat_id в JSON.
Из профиля строятся критерии (Criteria), под которые матчатся вакансии.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from .criteria import Criteria
from .storage import data_path, load_json, save_json

log = logging.getLogger("agent.profile")

PROFILES_FILE = "profiles.json"


@dataclass
class Profile:
    direction: str = ""
    skills: list[str] = field(default_factory=list)
    experience_years: int = 0
    city: str = ""
    remote: str = "ok"          # ok | no | only
    min_salary: int | None = None
    about: str = ""

    def to_criteria(self) -> Criteria:
        years = self.experience_years or 0
        if years <= 1:
            levels = ["intern", "junior"]
        elif years <= 2:
            levels = ["junior"]
        else:
            levels = ["junior", "middle"]

        is_remote_only_city = "удал" in self.city.lower()
        cities = [] if (not self.city or is_remote_only_city) else [self.city.lower()]

        free = (
            f"Опыт: {years} лет. "
            f"Навыки: {', '.join(self.skills) or '—'}. "
            f"Направление: {self.direction or '—'}. "
            f"О себе: {self.about or '—'}"
        ).strip()

        return Criteria(
            levels=levels,
            directions=[self.direction.lower()] if self.direction else [],
            cities=cities,
            remote=self.remote or "ok",
            skills=[s.lower() for s in self.skills],
            max_age_days=90,  # реальные данные trudvsem бывают не самые свежие
            min_salary=self.min_salary,
            free_text=free,
        )

    def human(self) -> str:
        sal = f"{self.min_salary} ₽" if self.min_salary else "не важно"
        return (
            "🧾 Твоя анкета:\n"
            f"• Направление: {self.direction or '—'}\n"
            f"• Навыки: {', '.join(self.skills) or '—'}\n"
            f"• Опыт: {self.experience_years} лет\n"
            f"• Город: {self.city or '—'}\n"
            f"• Удалёнка: {self.remote}\n"
            f"• Зарплата: {sal}\n"
            f"• О себе: {self.about or '—'}"
        )


def _load_all() -> dict:
    return load_json(data_path(PROFILES_FILE), {})


def get_profile(chat_id: int | str) -> Profile | None:
    data = _load_all().get(str(chat_id))
    if not data:
        return None
    try:
        return Profile(**data)
    except TypeError:
        return None


def save_profile(chat_id: int | str, profile: Profile) -> None:
    all_profiles = _load_all()
    all_profiles[str(chat_id)] = asdict(profile)
    if save_json(data_path(PROFILES_FILE), all_profiles):
        log.info("Профиль сохранён для chat_id=%s", chat_id)
