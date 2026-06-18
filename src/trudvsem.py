"""Реальный источник вакансий: открытый API портала «Работа России» (trudvsem).

Документация: https://trudvsem.ru/opendata/api
- HTTP GET, JSON, без ключа.
- Эндпоинт: http://opendata.trudvsem.ru/api/v1/vacancies?text=...&limit=...&offset=...

Используется ТОЛЬКО официальное открытое API (никакого парсинга страниц/капчи).
Ответ маппится в нашу внутреннюю схему вакансии, чтобы дальше работал общий пайплайн.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("agent.trudvsem")

_API = "http://opendata.trudvsem.ru/api/v1/vacancies"
_UA = {"User-Agent": "vacancy-agent/1.0 (educational test task)"}


def _exp_to_level(exp) -> str:
    """Грубая нормализация опыта (лет) в наш уровень."""
    try:
        years = int(exp)
    except (TypeError, ValueError):
        return ""
    if years <= 0:
        return "junior"
    if years >= 3:
        return "middle"
    return ""


def _map_vacancy(v: dict, query: str) -> dict:
    company = v.get("company") or {}
    requirement = v.get("requirement") or {}
    region = (v.get("region") or {}).get("name", "")
    duty = v.get("duty") or ""
    requirements = v.get("requirements") or ""
    description = f"{duty} {requirements}".strip()

    return {
        "id": v.get("id") or "",
        "title": v.get("job-name") or "",
        "company": company.get("name", ""),
        # направление = поисковый запрос, по которому нашли вакансию
        "direction": query,
        "level": _exp_to_level(requirement.get("experience")),
        "city": region,
        "remote": "",  # trudvsem не отдаёт признак удалёнки
        "published": v.get("creation-date", ""),
        "salary": v.get("salary_min"),
        "url": v.get("vac_url", ""),
        "skills": [],  # списка навыков нет; навыки ищем в тексте описания
        "description": description,
    }


def fetch_query(query: str, limit: int = 50, timeout: int = 25) -> list[dict]:
    params = {"text": query, "limit": min(max(limit, 1), 100), "offset": 0}
    url = f"{_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vacs = ((data.get("results") or {}).get("vacancies")) or []
    return [_map_vacancy(item.get("vacancy", {}), query) for item in vacs if item.get("vacancy")]


def fetch(
    queries: list[str],
    fallback_queries: list[str] | None = None,
    limit: int = 50,
    min_results: int = 10,
) -> tuple[list[dict], list[str]]:
    """Тянет вакансии по основным запросам; если мало — добирает fallback.

    Возвращает (список вакансий в нашей схеме, список реально использованных запросов).
    Сетевые ошибки логируются и не роняют процесс (вернётся то, что успели собрать).
    """
    collected: list[dict] = []
    used: list[str] = []

    def _run(qs: list[str]) -> None:
        for q in qs:
            try:
                items = fetch_query(q, limit)
                log.info("trudvsem '%s': получено %s вакансий", q, len(items))
                collected.extend(items)
                used.append(q)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError, ValueError) as e:
                log.error("trudvsem '%s' ошибка запроса: %s", q, e)

    _run(queries)
    if len(collected) < min_results and fallback_queries:
        log.info(
            "Мало результатов (%s < %s) — добираю fallback-запросы: %s",
            len(collected), min_results, fallback_queries,
        )
        _run(fallback_queries)

    log.info("trudvsem: всего собрано %s вакансий по запросам %s", len(collected), used)
    return collected, used
