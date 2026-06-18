"""LLM-слой: превращает скоринг в РЕШЕНИЕ (вердикт + объяснение).

Здесь агент перестаёт «просто считать» и начинает помогать принять решение:
для каждой топ-вакансии выдаётся вердикт apply / maybe / skip и короткое
человекочитаемое обоснование.

Если нет ключа OPENAI_API_KEY или пакета openai — используется детерминированный
fallback на основе скоринга. Прототип воспроизводим без сети.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .criteria import Criteria
from .scorer import Scored

log = logging.getLogger("agent.llm")

APPLY_THRESHOLD = 60
MAYBE_THRESHOLD = 35


@dataclass
class Decision:
    verdict: str        # apply | maybe | skip
    summary: str        # короткое объяснение «почему / что смущает»
    source: str         # "llm" или "rules"


def _rule_verdict(score: float) -> str:
    if score >= APPLY_THRESHOLD:
        return "apply"
    if score >= MAYBE_THRESHOLD:
        return "maybe"
    return "skip"


def _fallback_decision(s: Scored) -> Decision:
    verdict = _rule_verdict(s.score)
    why = "; ".join(s.matched[:3]) or "явных совпадений мало"
    concern = "; ".join(s.concerns[:2]) or "критичных рисков не видно"
    verdict_ru = {"apply": "стоит откликнуться", "maybe": "под вопросом", "skip": "скорее мимо"}[verdict]
    summary = f"{verdict_ru}. Подходит: {why}. Смущает: {concern}."
    return Decision(verdict=verdict, summary=summary, source="rules")


def _build_prompt(top: list[Scored], crit: Criteria) -> str:
    items = []
    for i, s in enumerate(top, 1):
        v = s.vacancy
        items.append({
            "n": i,
            "title": v.title,
            "company": v.company,
            "score": s.score,
            "matched": s.matched,
            "concerns": s.concerns,
            "skills": v.skills,
        })
    return (
        "Ты карьерный помощник. Матчишь вакансии под АНКЕТУ конкретного кандидата "
        "и помогаешь решить, куда откликаться.\n"
        f"АНКЕТА кандидата: {crit.free_text or 'junior, ищет первую работу'}\n"
        f"Навыки кандидата: {crit.skills or '—'}; целевые уровни: {crit.levels}.\n\n"
        "Вот предотобранные вакансии (с тем, что уже совпало, и рисками):\n"
        f"{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
        "Для КАЖДОЙ вакансии верни вердикт и персональное обоснование (1-2 предложения): "
        "почему подходит ИМЕННО этому кандидату и какого навыка/опыта из анкеты "
        "не хватает под вакансию (скилл-гэп). Вердикт строго: apply | maybe | skip.\n"
        'Ответ строго как JSON-массив объектов '
        '{"n": int, "verdict": "apply|maybe|skip", "summary": "..."} без лишнего текста.'
    )


def _extract_json_array(content: str) -> list:
    """Достаёт JSON-массив из ответа модели, даже если вокруг есть лишний текст.

    Бесплатные/reasoning-модели часто добавляют пояснения или ```-обёртки.
    """
    text = (content or "").strip()
    # снимаем markdown-обёртку ```json ... ```
    if "```" in text:
        text = text.replace("```json", "```")
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("["):
                text = part
                break
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # последний шанс: вырезаем подстроку от первой '[' до последней ']'
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("в ответе модели не найден JSON-массив")


def _llm_config() -> tuple[str | None, str | None, str]:
    """Конфиг LLM из окружения.

    Поддерживает OpenAI и любой OpenAI-совместимый провайдер (DeepSeek, OpenRouter,
    Together, локальный Ollama и т.д.) через base_url.

    Приоритет ключа: AGENT_LLM_API_KEY -> DEEPSEEK_API_KEY -> OPENAI_API_KEY.
    base_url: AGENT_LLM_BASE_URL -> OPENAI_BASE_URL (если пусто — облако OpenAI).
    model:    AGENT_LLM_MODEL (по умолчанию gpt-4o-mini).
    """
    api_key = (
        os.environ.get("AGENT_LLM_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    base_url = os.environ.get("AGENT_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    # Подставим эндпоинт по ключу, если base_url явно не задан.
    if not base_url:
        if os.environ.get("OPENROUTER_API_KEY"):
            base_url = "https://openrouter.ai/api/v1"
        elif os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
            base_url = "https://api.deepseek.com"

    if base_url and "openrouter" in base_url:
        # Несколько бесплатных моделей: перебираем по очереди (free-пул часто 429).
        default_model = (
            "google/gemma-4-31b-it:free,"
            "meta-llama/llama-3.3-70b-instruct:free,"
            "qwen/qwen3-next-80b-a3b-instruct:free"
        )
    elif base_url and "deepseek" in base_url:
        default_model = "deepseek-chat"
    else:
        default_model = "gpt-4o-mini"
    model = os.environ.get("AGENT_LLM_MODEL", default_model)
    return api_key, base_url, model


def _call_model(client, model: str, prompt: str) -> dict:
    """Один вызов модели -> словарь {n: item}. Бросает исключение при ошибке."""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    content = resp.choices[0].message.content or ""
    parsed = _extract_json_array(content)
    return {int(item["n"]): item for item in parsed}


def _try_llm(top: list[Scored], crit: Criteria) -> list[Decision] | None:
    api_key, base_url, model_cfg = _llm_config()
    if not api_key:
        log.info("Ключ LLM не задан — слой отключён, работаю на правилах")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("Пакет openai не установлен — fallback на правила")
        return None

    # max_retries=0 — не ждём долгих ретраев на 429, а сразу пробуем следующую модель.
    kwargs = {"api_key": api_key, "max_retries": 0, "timeout": 45.0}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    prompt = _build_prompt(top, crit)
    models = [m.strip() for m in model_cfg.split(",") if m.strip()]

    by_n: dict | None = None
    model = ""
    for candidate in models:
        try:
            by_n = _call_model(client, candidate, prompt)
            model = candidate
            break
        except Exception as e:  # 429/404/сеть/кривой JSON — пробуем следующую модель
            log.warning("Модель %s не ответила (%s) — пробую следующую", candidate, e)
    if by_n is None:
        log.warning("Все LLM-модели недоступны — fallback на правила")
        return None

    decisions: list[Decision] = []
    for i, s in enumerate(top, 1):
        item = by_n.get(i)
        if not item or item.get("verdict") not in ("apply", "maybe", "skip"):
            decisions.append(_fallback_decision(s))
            continue
        decisions.append(Decision(
            verdict=item["verdict"],
            summary=str(item.get("summary", "")).strip() or _fallback_decision(s).summary,
            source="llm",
        ))
    log.info("LLM вынес решения по %s вакансиям (модель %s)", len(decisions), model)
    return decisions


def decide(top: list[Scored], crit: Criteria) -> list[Decision]:
    """Главная точка входа: пробуем LLM, иначе детерминированный fallback."""
    if not top:
        return []
    llm_result = _try_llm(top, crit)
    if llm_result is not None:
        return llm_result
    return [_fallback_decision(s) for s in top]
