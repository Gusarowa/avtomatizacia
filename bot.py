#!/usr/bin/env python3
"""Telegram-бот: триггерит агента по сообщению и присылает отчёт.

Запуск:
  export TELEGRAM_BOT_TOKEN=...   # или положи в .env
  python3 bot.py

Команды в чате:
  /start, /help          — справка
  /find <запрос>         — найти вакансии по запросу (реальные данные trudvsem)
  <любой текст>          — то же самое, текст используется как поисковый запрос
  (пусто/просто /find)   — поиск по умолчанию: «продуктовый аналитик»

Бот работает на long polling (getUpdates), никаких вебхуков и зависимостей.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from src import subscriptions
from src.dotenv import load_dotenv
from src.pipeline import DEFAULT_QUERIES, run_pipeline
from src.profile import Profile, get_profile, save_profile
from src.report import build_digest
from src.telegram import (
    answer_callback_query,
    get_updates,
    inline_keyboard,
    send_message,
    send_report,
)

log = logging.getLogger("agent.bot")

REPORT_PATH = "reports/report.md"
HELP_TEXT = (
    "Привет! Я матчу junior-вакансии и стажировки под твою анкету "
    "на реальных данных портала «Работа России».\n\n"
    "Команды:\n"
    "• /anketa — заполнить анкету (направление, навыки, опыт, город, зарплата)\n"
    "• /me — показать мою анкету и статус подписки\n"
    "• /find — подобрать вакансии под анкету\n"
    "• /find data analyst — поиск по конкретному запросу\n"
    "• /subscribe — ежедневный дайджест новых вакансий\n"
    "• /unsubscribe — отключить дайджест\n"
    "• просто пришли текст — тоже поиск\n\n"
    "После анкеты пришлю персональный разбор (apply/maybe/skip) со скилл-гэпом "
    "и файл с полным отчётом."
)

# Шаги мастера анкеты: поле, вопрос и (опционально) кнопки [(подпись, значение)].
# Кнопки ускоряют ввод и убирают ошибки; текстом ответить тоже можно.
WIZARD_STEPS: list[dict] = [
    {
        "field": "direction",
        "q": "1/7 Какое направление ищем? Выбери кнопкой или напиши свой вариант.",
        "options": [
            [("Продуктовый аналитик", "продуктовый аналитик")],
            [("Аналитик данных", "аналитик данных")],
            [("Системный аналитик", "системный аналитик")],
            [("Бизнес-аналитик", "бизнес аналитик")],
        ],
    },
    {
        "field": "skills",
        "q": "2/7 Твои навыки через запятую (например: sql, python, excel)",
        "options": None,
    },
    {
        "field": "experience_years",
        "q": "3/7 Сколько лет опыта? Выбери или напиши число.",
        "options": [
            [("Нет опыта", "0"), ("1 год", "1"), ("2 года", "2")],
            [("3 года", "3"), ("4 года", "4"), ("5+ лет", "5")],
        ],
    },
    {
        "field": "city",
        "q": "4/7 Город (например: Москва) или нажми «Удалёнка».",
        "options": [
            [("Москва", "Москва"), ("Санкт-Петербург", "Санкт-Петербург")],
            [("Удалёнка", "удалёнка")],
        ],
    },
    {
        "field": "remote",
        "q": "5/7 Удалёнка важна?",
        "options": [
            [("Да, плюс", "да"), ("Не важно", "нет"), ("Только удалёнка", "только")],
        ],
    },
    {
        "field": "min_salary",
        "q": "6/7 Желаемая зарплата в ₽. Выбери или напиши число.",
        "options": [
            [("Не важно", "0"), ("от 60к", "60000"), ("от 80к", "80000")],
            [("от 100к", "100000"), ("от 150к", "150000")],
        ],
    },
    {
        "field": "about",
        "q": "7/7 Пара слов о себе (или нажми «Пропустить»).",
        "options": [
            [("Пропустить", "-")],
        ],
    },
]

# Состояние мастера по chat_id: {"step": int, "draft": {...}}.
_wizard: dict[int, dict] = {}

# --- Rate-limit на пользователя ---
MIN_MESSAGE_INTERVAL_SEC = 1.0   # антифлуд: чаще — молча игнорируем
DEDUP_WINDOW_SEC = 5.0           # одинаковое сообщение подряд в этом окне — игнор
SEARCH_COOLDOWN_SEC = 20.0       # минимум между тяжёлыми поисками на пользователя

# chat_id -> (timestamp последнего обработанного сообщения, его текст)
_last_msg: dict[int, tuple[float, str]] = {}
# chat_id -> timestamp последнего поиска
_last_search: dict[int, float] = {}

# --- Подписка / ежедневный дайджест ---
# Как часто слать дайджест одному подписчику (по умолчанию раз в сутки).
DIGEST_INTERVAL_SEC = float(os.environ.get("AGENT_DIGEST_INTERVAL_SEC", 24 * 3600))
# Как часто воркер просыпается и проверяет, кому пора (по умолчанию каждые 15 мин).
DIGEST_TICK_SEC = float(os.environ.get("AGENT_DIGEST_TICK_SEC", 15 * 60))

# run_pipeline пишет общие файлы (reports/, data/) — сериализуем доступ из потоков.
_pipeline_lock = threading.Lock()


def _throttled(chat_id: int, text: str, now: float | None = None) -> bool:
    """Антифлуд + дедуп. True -> сообщение надо проигнорировать."""
    import time as _t
    now = now if now is not None else _t.time()
    last_t, last_text = _last_msg.get(chat_id, (0.0, ""))
    delta = now - last_t
    if delta < MIN_MESSAGE_INTERVAL_SEC:
        return True
    if text and text == last_text and delta < DEDUP_WINDOW_SEC:
        return True
    _last_msg[chat_id] = (now, text)
    return False


def start_health_server() -> None:
    """Поднимает простой HTTP-эндпоинт, если задан PORT (нужно хостингам типа Koyeb).

    Бот сам по себе не веб-сервис (long polling), но облака проверяют живость по порту.
    """
    port = os.environ.get("PORT")
    if not port:
        return

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # глушим access-логи
            return

    try:
        server = HTTPServer(("0.0.0.0", int(port)), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Health-сервер слушает порт %s", port)
    except OSError as e:
        log.error("Не удалось поднять health-сервер на порту %s: %s", port, e)


def setup_logging() -> None:
    os.makedirs("reports", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler("reports/bot.log", encoding="utf-8"),
        ],
    )


def validate_field(field: str, raw: str) -> tuple[bool, object]:
    """Проверяет и преобразует ответ анкеты.

    Возвращает (True, значение) при успехе или (False, текст-подсказку) при ошибке,
    чтобы бот мог переспросить вопрос вместо приёма мусора.
    """
    raw = (raw or "").strip()

    if field == "direction":
        if len(raw) < 2:
            return False, "Напиши направление текстом, например: продуктовый аналитик"
        if len(raw) > 100:
            return False, "Слишком длинно — уложись в 100 символов."
        return True, raw

    if field == "skills":
        items = [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]
        if not items:
            return False, "Перечисли хотя бы один навык через запятую, например: sql, python, excel"
        return True, items[:30]

    if field == "experience_years":
        low = raw.lower()
        if low in ("нет", "ноль", "no", "none", "-", "—"):
            return True, 0
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return False, "Нужно число — сколько лет опыта? Напиши, например, 0, 1 или 3."
        years = int(digits)
        if years > 60:
            return False, "Похоже на опечатку. Введи реальное число лет (0–60)."
        return True, years

    if field == "city":
        if len(raw) < 2:
            return False, "Напиши город (например: Москва) или «удалёнка»."
        if len(raw) > 60:
            return False, "Слишком длинно для города — покороче, пожалуйста."
        return True, raw

    if field == "remote":
        low = raw.lower()
        if low.startswith(("тол", "only")):
            return True, "only"
        if low.startswith(("нет", "no", "офис", "офл")):
            return True, "no"
        if low.startswith(("да", "yes", "ok", "ок", "+", "удал", "можно", "норм")):
            return True, "ok"
        return False, "Не понял. Ответь одним словом: да / нет / только"

    if field == "min_salary":
        low = raw.lower()
        if low in ("0", "-", "—", "не важно", "неважно", "нет", "none"):
            return True, None
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            return False, "Введи число — желаемую зарплату в ₽ (или 0, если не важно)."
        return True, int(digits)

    if field == "about":
        if raw in ("-", "—", ""):
            return True, ""
        if len(raw) > 500:
            return False, "Слишком длинно — пары предложений достаточно (до 500 символов)."
        return True, raw

    return True, raw


def _step_keyboard(step_index: int) -> dict | None:
    """Inline-клавиатура для шага анкеты (или None, если ввод только текстом)."""
    options = WIZARD_STEPS[step_index].get("options")
    if not options:
        return None
    rows = [
        [(label, f"w:{step_index}:{value}") for label, value in row]
        for row in options
    ]
    return inline_keyboard(rows)


def _ask_step(token: str, chat_id: int, step_index: int) -> None:
    send_message(token, chat_id, WIZARD_STEPS[step_index]["q"], _step_keyboard(step_index))


def start_wizard(token: str, chat_id: int) -> None:
    _wizard[chat_id] = {"step": 0, "draft": {}}
    send_message(token, chat_id, "Заполним анкету (7 коротких вопросов). Напиши /cancel, чтобы выйти.")
    _ask_step(token, chat_id, 0)


def advance_wizard(token: str, chat_id: int, text: str) -> None:
    state = _wizard[chat_id]
    step = state["step"]
    field = WIZARD_STEPS[step]["field"]

    ok, result = validate_field(field, text)
    if not ok:
        send_message(token, chat_id, "⚠️ " + str(result))
        _ask_step(token, chat_id, step)  # переспрашиваем тот же вопрос
        return

    state["draft"][field] = result
    state["step"] += 1

    if state["step"] < len(WIZARD_STEPS):
        _ask_step(token, chat_id, state["step"])
        return

    # Анкета заполнена.
    profile = Profile(**state["draft"])
    save_profile(chat_id, profile)
    _wizard.pop(chat_id, None)
    send_message(token, chat_id, "Готово! " + profile.human())
    send_message(
        token, chat_id,
        "Теперь напиши /find — подберу вакансии под анкету. "
        "А /subscribe включит ежедневный дайджест новых вакансий.",
    )


def do_find(token: str, chat_id: int, query_override: str | None = None) -> None:
    if query_override is not None and len(query_override.strip()) < 2:
        send_message(token, chat_id, "Запрос слишком короткий. Напиши, что ищем, например: продуктовый аналитик")
        return

    # Кулдаун: поиск тяжёлый (сеть + LLM), не даём дёргать его слишком часто.
    now = time.time()
    wait = SEARCH_COOLDOWN_SEC - (now - _last_search.get(chat_id, 0.0))
    if wait > 0:
        send_message(token, chat_id, f"⏳ Поиск недавно запускался — подожди ещё {int(wait) + 1} сек.")
        return
    _last_search[chat_id] = now

    profile = get_profile(chat_id)
    criteria = profile.to_criteria() if profile else None

    if query_override:
        query = query_override
    elif profile and profile.direction:
        query = profile.direction
    else:
        query = DEFAULT_QUERIES[0]
        send_message(
            token, chat_id,
            "Анкета не заполнена — ищу по умолчанию. Заполни /anketa для персонального подбора.",
        )

    persona = "под твою анкету" if (profile and not query_override) else f"по запросу «{query}»"
    send_message(token, chat_id, f"🔎 Подбираю вакансии {persona}. Минутку…")

    try:
        with _pipeline_lock:
            result = run_pipeline(
                source="trudvsem",
                queries=[query],
                criteria=criteria,
                top_n=5,
            )
    except Exception as e:  # пайплайн не должен ронять бота
        log.exception("Ошибка пайплайна: %s", e)
        send_message(token, chat_id, f"⚠️ Не получилось обработать запрос: {e}")
        return

    os.makedirs(os.path.dirname(REPORT_PATH) or ".", exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(result.report_md)

    if not result.top:
        send_message(token, chat_id, "Ничего подходящего не нашлось. Попробуй другой запрос или обнови /anketa.")
        return

    send_report(summary=result.summary, report_path=REPORT_PATH, token=token, chat_id=chat_id)


def _interval_human() -> str:
    hours = DIGEST_INTERVAL_SEC / 3600
    if hours >= 24 and hours % 24 == 0:
        days = int(hours // 24)
        return "раз в день" if days == 1 else f"раз в {days} дн."
    return f"раз в {int(hours)} ч." if hours >= 1 else f"раз в {int(DIGEST_INTERVAL_SEC / 60)} мин."


def subscribe(token: str, chat_id: int) -> None:
    profile = get_profile(chat_id)
    if not profile or not profile.direction:
        send_message(
            token, chat_id,
            "Чтобы подписаться на дайджест, сначала заполни анкету — /anketa. "
            "Иначе я не знаю, что тебе искать.",
        )
        return
    subscriptions.set_enabled(chat_id, True)
    send_message(
        token, chat_id,
        f"✅ Подписка включена. Буду присылать новые подходящие вакансии {_interval_human()}.\n"
        "Отключить — /unsubscribe.",
    )


def unsubscribe(token: str, chat_id: int) -> None:
    subscriptions.set_enabled(chat_id, False)
    send_message(token, chat_id, "🔕 Подписка отключена. Включить снова — /subscribe.")


def run_digest_for(token: str, chat_id: int) -> None:
    """Считает свежие вакансии под анкету и шлёт только новые (не показанные ранее)."""
    profile = get_profile(chat_id)
    if not profile or not profile.direction:
        subscriptions.mark_run(chat_id, [])  # нечем персонализировать — просто не дёргаем дальше
        return

    query = profile.direction
    try:
        with _pipeline_lock:
            result = run_pipeline(
                source="trudvsem",
                queries=[query],
                criteria=profile.to_criteria(),
                top_n=5,
            )
    except Exception as e:
        log.exception("Дайджест: пайплайн упал для %s: %s", chat_id, e)
        return

    keyed = [(subscriptions.vacancy_key(s.vacancy), s, d)
             for s, d in zip(result.top, result.decisions)]
    new_set = set(subscriptions.filter_new(chat_id, [k for k, _, _ in keyed]))
    new_items = [(s, d) for k, s, d in keyed if k in new_set]

    if new_items:
        send_message(token, chat_id, build_digest(new_items, query, result.today))
    subscriptions.mark_run(chat_id, list(new_set))


def digest_worker(token: str) -> None:
    """Фоновый поток: периодически рассылает дайджест тем, кому пора."""
    log.info("Воркер дайджеста запущен (интервал %.0f c, тик %.0f c)",
             DIGEST_INTERVAL_SEC, DIGEST_TICK_SEC)
    while True:
        try:
            for cid in subscriptions.enabled_chat_ids():
                if subscriptions.due_for_digest(cid, DIGEST_INTERVAL_SEC):
                    log.info("Дайджест для chat_id=%s", cid)
                    try:
                        run_digest_for(token, int(cid))
                    except Exception:
                        log.exception("Дайджест для %s упал", cid)
        except Exception:
            log.exception("Сбой воркера дайджеста")
        time.sleep(DIGEST_TICK_SEC)


def dispatch_callback(token: str, chat_id: int, data: str) -> None:
    """Обработка нажатия inline-кнопки."""
    if data.startswith("w:"):
        # Ответ на шаг анкеты: w:<step>:<value>
        try:
            _, step_str, value = data.split(":", 2)
            step = int(step_str)
        except ValueError:
            return
        if chat_id not in _wizard:
            send_message(token, chat_id, "Анкета уже завершена. Запусти заново — /anketa.")
            return
        if step != _wizard[chat_id]["step"]:
            return  # устаревшая кнопка из прошлого сообщения — игнорируем
        advance_wizard(token, chat_id, value)
        return

    if data.startswith("m:"):
        action = data[2:]
        if action == "anketa":
            start_wizard(token, chat_id)
        elif action == "find":
            do_find(token, chat_id, None)
        elif action == "me":
            _send_me(token, chat_id)
        elif action == "sub_on":
            subscribe(token, chat_id)
        elif action == "sub_off":
            unsubscribe(token, chat_id)
        return


def _send_me(token: str, chat_id: int) -> None:
    profile = get_profile(chat_id)
    if not profile:
        send_message(token, chat_id, "Анкета пока пустая. Заполни её командой /anketa.")
        return
    sub = "включена ✅" if subscriptions.is_enabled(chat_id) else "выключена 🔕"
    send_message(token, chat_id, profile.human() + f"\n• Дайджест: {sub}")


def _main_menu() -> dict:
    return inline_keyboard([
        [("📝 Анкета", "m:anketa"), ("🔎 Найти", "m:find")],
        [("👤 Моя анкета", "m:me")],
        [("🔔 Подписаться", "m:sub_on"), ("🔕 Отписаться", "m:sub_off")],
    ])


def dispatch(token: str, chat_id: int, text: str) -> None:
    """Маршрутизация одного входящего сообщения."""
    raw = (text or "").strip()
    low = raw.lower()

    # Антифлуд/дедуп: слишком частые или повторяющиеся сообщения молча отбрасываем.
    if _throttled(chat_id, raw):
        log.info("Throttled сообщение от %s", chat_id)
        return

    # Если идёт мастер анкеты — почти всё считаем ответом на текущий вопрос.
    if chat_id in _wizard:
        if low in ("/cancel", "отмена", "/стоп"):
            _wizard.pop(chat_id, None)
            send_message(token, chat_id, "Окей, анкета отменена.")
            return
        advance_wizard(token, chat_id, raw)
        return

    if low in ("/start", "/help", "start", "help"):
        send_message(token, chat_id, HELP_TEXT, _main_menu())
        return
    if low in ("/anketa", "/анкета", "/profile", "/профиль"):
        start_wizard(token, chat_id)
        return
    if low in ("/me", "/моя", "/мояанкета"):
        _send_me(token, chat_id)
        return
    if low in ("/subscribe", "/подписка", "/sub"):
        subscribe(token, chat_id)
        return
    if low in ("/unsubscribe", "/отписка", "/unsub"):
        unsubscribe(token, chat_id)
        return
    if low.startswith("/find"):
        q = raw[len("/find"):].strip()
        do_find(token, chat_id, q or None)
        return
    if raw.startswith("/"):
        send_message(token, chat_id, HELP_TEXT)
        return

    # Просто текст -> поиск по этому запросу.
    do_find(token, chat_id, raw)


def main() -> int:
    load_dotenv()
    setup_logging()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN не задан (положи в .env или экспортируй)")
        return 1

    start_health_server()
    threading.Thread(target=digest_worker, args=(token,), daemon=True).start()
    log.info("Бот запущен. Жду сообщений…")
    offset: int | None = None
    try:
        while True:
            updates = get_updates(token, offset=offset, timeout=25)
            for upd in updates:
                offset = upd["update_id"] + 1

                # Нажатие inline-кнопки.
                cq = upd.get("callback_query")
                if cq:
                    cq_id = cq.get("id")
                    data = cq.get("data", "")
                    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
                    if cq_id:
                        answer_callback_query(token, cq_id)
                    if chat_id is not None:
                        log.info("Callback от %s: %r", chat_id, data)
                        dispatch_callback(token, chat_id, data)
                    continue

                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text", "")
                if chat_id is None:
                    continue
                log.info("Сообщение от %s: %r", chat_id, text)
                dispatch(token, chat_id, text)
            if not updates:
                time.sleep(1)  # лёгкая пауза, если long-poll вернул пусто
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
