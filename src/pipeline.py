"""Переиспользуемый пайплайн агента (общий для CLI и Telegram-бота)."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

from . import trudvsem
from .criteria import Criteria, load_criteria
from .llm import Decision, decide
from .loader import LoadStats, load_vacancies
from .report import build_report, build_telegram_summary
from .scorer import Scored, matches_direction, rank

log = logging.getLogger("agent.pipeline")

DEFAULT_QUERIES = ["продуктовый аналитик"]
DEFAULT_FALLBACK_QUERIES = ["системный аналитик", "бизнес аналитик"]


@dataclass
class PipelineResult:
    top: list[Scored]
    decisions: list[Decision]
    crit: Criteria
    stats: LoadStats
    today: date
    llm_used: bool
    filtered_out: int
    skipped_count: int
    report_md: str
    summary: str


def run_pipeline(
    *,
    source: str = "file",
    input_path: str = "data/vacancies.csv",
    queries: list[str] | None = None,
    fallback_queries: list[str] | None = None,
    limit: int = 50,
    min_results: int = 10,
    criteria_path: str = "criteria.md",
    criteria: Criteria | None = None,
    top_n: int = 5,
    today: date | None = None,
    raw_out_path: str = "data/vacancies_trudvsem.json",
) -> PipelineResult:
    today = today or date.today()
    fallback_queries = fallback_queries if fallback_queries is not None else DEFAULT_FALLBACK_QUERIES

    # 1. Данные: файл или реальный API trudvsem.
    override_directions: list[str] | None = None
    if source == "trudvsem":
        qs = queries or DEFAULT_QUERIES
        raw, used = trudvsem.fetch(qs, fallback_queries, limit, min_results)
        os.makedirs(os.path.dirname(raw_out_path) or ".", exist_ok=True)
        with open(raw_out_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        log.info("Сырые вакансии сохранены: %s", raw_out_path)
        override_directions = [q.lower() for q in used] or None
        input_path = raw_out_path

    vacancies, stats = load_vacancies(input_path)

    # 2. Критерии: готовая анкета (Criteria) важнее файла criteria.md.
    crit = criteria if criteria is not None else load_criteria(criteria_path)
    if override_directions:
        crit.directions = override_directions
        log.info("Направление переопределено запросами источника: %s", override_directions)

    # 2.5 Жёсткий фильтр по направлению.
    if crit.directions:
        kept = [v for v in vacancies if matches_direction(v, crit)]
        filtered_out = len(vacancies) - len(kept)
        log.info("Фильтр по направлению %s: оставлено %s, отсеяно %s",
                 crit.directions, len(kept), filtered_out)
    else:
        kept, filtered_out = vacancies, 0

    # 3. Скоринг
    scored = rank(kept, crit, today)

    # 4. Решение + отсев skip (пул шире топ-N, чтобы топ заполнился).
    pool = scored[: max(top_n * 3, top_n)]
    pool_decisions = decide(pool, crit)
    llm_used = any(d.source == "llm" for d in pool_decisions)

    pairs = [(s, d) for s, d in zip(pool, pool_decisions) if d.verdict != "skip"]
    skipped_count = len(pool_decisions) - len(pairs)
    log.info("Отсеяно по вердикту skip: %s", skipped_count)

    top_pairs = pairs[: max(top_n, 0)]
    top = [s for s, _ in top_pairs]
    decisions = [d for _, d in top_pairs]

    # 5. Отчёт + краткое summary
    report_md = build_report(top, decisions, crit, stats, today, llm_used, filtered_out, skipped_count)
    summary = build_telegram_summary(top, decisions, crit, stats, today, filtered_out, skipped_count)

    return PipelineResult(
        top=top, decisions=decisions, crit=crit, stats=stats, today=today,
        llm_used=llm_used, filtered_out=filtered_out, skipped_count=skipped_count,
        report_md=report_md, summary=summary,
    )
