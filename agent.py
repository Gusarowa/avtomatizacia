#!/usr/bin/env python3
"""Точка входа CLI-агента.

Пайплайн:
  1. Загрузка вакансий (CSV/JSON) с обработкой пустых/битых файлов и дублей.
  2. Чтение критериев пользователя из criteria.md (или дефолты).
  3. Детерминированный скоринг по правилам.
  4. LLM-вердикт по топ-N (apply/maybe/skip) с fallback на правила.
  5. Markdown-отчёт + лог действий.

Примеры:
  python agent.py --input data/vacancies.csv --criteria criteria.md
  python agent.py --input data/vacancies.json --top 3 --output reports/out.md
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime

from src.dotenv import load_dotenv
from src.pipeline import run_pipeline
from src.telegram import send_report


def setup_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Агент для разбора junior-вакансий / стажировок")
    p.add_argument("--input", "-i", default="data/vacancies.csv", help="CSV или JSON с вакансиями")
    p.add_argument("--source", choices=["file", "trudvsem"], default="file",
                   help="Источник: file (CSV/JSON) или trudvsem (реальный API «Работа России»)")
    p.add_argument("--query", "-q", action="append", default=None,
                   help="Поисковый запрос для trudvsem (можно несколько раз)")
    p.add_argument("--limit", type=int, default=50, help="Сколько вакансий тянуть на запрос (trudvsem)")
    p.add_argument("--min-results", type=int, default=10,
                   help="Если результатов меньше — добрать fallback-запросы (trudvsem)")
    p.add_argument("--criteria", "-c", default="criteria.md", help="Файл критериев пользователя")
    p.add_argument("--output", "-o", default="reports/report.md", help="Куда сохранить Markdown-отчёт")
    p.add_argument("--top", "-n", type=int, default=5, help="Сколько вакансий показать в отчёте")
    p.add_argument("--today", default=None, help="Дата отсчёта свежести YYYY-MM-DD (по умолчанию сегодня)")
    p.add_argument("--log", default="reports/agent.log", help="Файл лога")
    p.add_argument("--telegram", action="store_true", help="Отправить отчёт в Telegram")
    p.add_argument("--tg-token", default=None, help="Токен бота (или env TELEGRAM_BOT_TOKEN)")
    p.add_argument("--tg-chat", default=None, help="Chat id (или env TELEGRAM_CHAT_ID)")
    p.add_argument("--tg-no-file", action="store_true", help="Не прикреплять файл отчёта в Telegram")
    p.add_argument("--tg-dry-run", action="store_true", help="Показать Telegram-сообщение без отправки")
    return p.parse_args(argv)


def resolve_today(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        logging.getLogger("agent").warning("Неверная дата --today=%s, беру сегодня", value)
        return date.today()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()
    setup_logging(args.log)
    log = logging.getLogger("agent")
    today = resolve_today(args.today)

    log.info("=== Старт агента ===")
    log.info("Источник: %s | критерии: %s | топ: %s | дата: %s", args.source, args.criteria, args.top, today)

    result = run_pipeline(
        source=args.source,
        input_path=args.input,
        queries=args.query,
        limit=args.limit,
        min_results=args.min_results,
        criteria_path=args.criteria,
        top_n=args.top,
        today=today,
    )

    # Сохранение отчёта
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(result.report_md)

    log.info("Отчёт сохранён: %s", args.output)
    log.info("Итог: загружено=%s, в отчёте=%s, LLM=%s",
             result.stats.loaded, len(result.top), result.llm_used)

    # Доставка в Telegram (опционально)
    tg_sent = False
    if args.telegram or args.tg_dry_run:
        if args.tg_dry_run:
            print("\n--- Предпросмотр Telegram-сообщения ---\n")
            print(result.summary)
            print("--- конец предпросмотра ---")
        else:
            tg_sent = send_report(
                summary=result.summary,
                report_path=args.output,
                token=args.tg_token,
                chat_id=args.tg_chat,
                attach_file=not args.tg_no_file,
            )

    log.info("=== Готово ===")

    print(f"\nГотово. Загружено {result.stats.loaded} вакансий, в отчёте топ-{len(result.top)}.")
    print(f"Решения: {'LLM' if result.llm_used else 'правила (fallback)'}.")
    print(f"Отчёт: {args.output}\nЛог: {args.log}")
    if args.telegram:
        print(f"Telegram: {'отправлено' if tg_sent else 'не отправлено (см. лог)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
