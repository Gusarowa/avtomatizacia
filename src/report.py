"""Сборка итогового Markdown-отчёта."""
from __future__ import annotations

from datetime import date

from .criteria import Criteria
from .llm import Decision
from .loader import LoadStats
from .scorer import Scored

_VERDICT_BADGE = {
    "apply": "✅ Откликаться",
    "maybe": "🟡 Под вопросом",
    "skip": "⛔️ Пропустить",
}


def _fmt_salary(salary: int | None) -> str:
    return f"{salary:,}".replace(",", " ") + " ₽" if salary else "—"


def build_telegram_summary(
    top: list[Scored],
    decisions: list[Decision],
    crit: Criteria,
    stats: LoadStats,
    today: date,
    filtered_out: int = 0,
    skipped_count: int = 0,
) -> str:
    """Компактное текстовое summary для Telegram (без Markdown-разметки)."""
    direction = ", ".join(crit.directions) or "любое направление"
    lines = [
        f"🔎 Вакансии: {direction} — {today.isoformat()}",
        f"Подходящих: {len(top)} (из {stats.loaded} валидных; "
        f"отсев по направлению {filtered_out}, skip {skipped_count}).",
        "",
    ]
    if not top:
        lines.append("⚠️ Подходящих вакансий не найдено.")
        return "\n".join(lines)

    for i, (s, d) in enumerate(zip(top, decisions), 1):
        v = s.vacancy
        badge = _VERDICT_BADGE.get(d.verdict, d.verdict)
        summary = d.summary if len(d.summary) <= 220 else d.summary[:217] + "..."
        lines.append(f"{i}. {badge} — {v.title} ({v.company or '—'}) · score {s.score}")
        lines.append(f"   📍 {v.city or '—'} · 💰 {_fmt_salary(v.salary)}")
        lines.append(f"   {summary}")
        if v.url:
            lines.append(f"   {v.url}")
        lines.append("")
    return "\n".join(lines)


def build_digest(items: list[tuple[Scored, Decision]], query: str, today: date) -> str:
    """Компактный текст «свежие вакансии» для ежедневного дайджеста."""
    lines = [
        f"🆕 Свежие вакансии под анкету ({query}) — {today.isoformat()}:",
        "",
    ]
    for i, (s, d) in enumerate(items, 1):
        v = s.vacancy
        badge = _VERDICT_BADGE.get(d.verdict, d.verdict)
        summary = d.summary if len(d.summary) <= 180 else d.summary[:177] + "..."
        lines.append(f"{i}. {badge} — {v.title} ({v.company or '—'}) · score {s.score}")
        lines.append(f"   📍 {v.city or '—'} · 💰 {_fmt_salary(v.salary)}")
        lines.append(f"   {summary}")
        if v.url:
            lines.append(f"   {v.url}")
        lines.append("")
    return "\n".join(lines)


def build_report(
    top: list[Scored],
    decisions: list[Decision],
    crit: Criteria,
    stats: LoadStats,
    today: date,
    llm_used: bool,
    filtered_out: int = 0,
    skipped_count: int = 0,
) -> str:
    lines: list[str] = []
    a = lines.append

    a("# Отчёт агента по вакансиям")
    a("")
    a(f"_Дата запуска: {today.isoformat()}_  ")
    a(f"_Источник решений: {'LLM (' + 'gpt' + ')' if llm_used else 'правила (fallback без LLM)'}_")
    a("")

    # --- Сводка по входным данным ---
    a("## Что на входе")
    a("")
    a(f"- Строк в файле: **{stats.total_rows}**")
    a(f"- Загружено валидных: **{stats.loaded}**")
    a(f"- Битых/пропущено: **{stats.skipped_broken}**")
    a(f"- Дублей удалено: **{stats.duplicates}**")
    a(f"- Отсеяно по направлению: **{filtered_out}**")
    a(f"- Прошло фильтр направления: **{stats.loaded - filtered_out}**")
    a(f"- Скрыто с вердиктом skip: **{skipped_count}**")
    a("")
    a("**Критерии пользователя:** "
      f"уровни={crit.levels or '—'}, направления={crit.directions or 'любые'}, "
      f"города={crit.cities or '—'}, удалёнка={crit.remote}, "
      f"навыки={crit.skills or '—'}, свежесть≤{crit.max_age_days} дн.")
    a("")

    if not top:
        a("> ⚠️ Подходящих вакансий не найдено или входные данные пустые/битые.")
        if stats.problems:
            a("")
            a("Проблемы при загрузке:")
            for p in stats.problems[:10]:
                a(f"- {p}")
        return "\n".join(lines)

    # --- Топ-5 ---
    a(f"## Топ-{len(top)} вакансий")
    a("")
    for i, (s, d) in enumerate(zip(top, decisions), 1):
        v = s.vacancy
        a(f"### {i}. {v.title} — {v.company or '—'}")
        a("")
        a(f"{_VERDICT_BADGE.get(d.verdict, d.verdict)} · **score {s.score}**")
        a("")
        a(f"- 📍 {v.city or '—'} · удалёнка: {v.remote or '—'} · 💰 {_fmt_salary(v.salary)}")
        a(f"- 🗂 направление: {v.direction or '—'} · уровень: {v.level or '—'} · опубликовано: {v.published or '—'}")
        if v.url:
            a(f"- 🔗 {v.url}")
        a("")
        a(f"**Вывод агента:** {d.summary}")
        a("")
        if s.matched:
            a("**Почему подходит:**")
            for m in s.matched:
                a(f"- {m}")
            a("")
        if s.concerns:
            a("**Что смущает:**")
            for c in s.concerns:
                a(f"- {c}")
            a("")
        a("<details><summary>Разбор баллов</summary>")
        a("")
        for k, val in s.breakdown.items():
            a(f"- `{k}`: {val}")
        a("")
        a("</details>")
        a("")

    # --- Итоговая таблица ---
    a("## Сводная таблица")
    a("")
    a("| # | Вакансия | Score | Вердикт |")
    a("|---|----------|-------|---------|")
    for i, (s, d) in enumerate(zip(top, decisions), 1):
        a(f"| {i} | {s.vacancy.title} | {s.score} | {_VERDICT_BADGE.get(d.verdict, d.verdict)} |")
    a("")

    return "\n".join(lines)
