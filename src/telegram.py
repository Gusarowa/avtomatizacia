"""Отправка отчёта в Telegram через Bot API (только стандартная библиотека).

Учётные данные берутся из окружения:
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHAT_ID   — id чата/пользователя (узнать можно у @userinfobot)

Без них отправка тихо пропускается (агент не падает). Сетевые ошибки и ответы
не-200 логируются и не роняют пайплайн.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("agent.telegram")

_TG_LIMIT = 4096  # лимит длины текстового сообщения Telegram
_API = "https://api.telegram.org/bot{token}/{method}"


def _get_json(token: str, method: str, params: dict | None = None, timeout: int = 20) -> dict | None:
    url = _API.format(token=token, method=method)
    if params:
        import urllib.parse
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
        log.error("Telegram %s ошибка: %s", method, e)
        return None


def get_updates(token: str, offset: int | None = None, timeout: int = 25) -> list[dict]:
    """Long polling: возвращает список апдейтов (пустой при ошибке)."""
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    # http-таймаут чуть больше long-poll, чтобы соединение не рвалось раньше времени
    data = _get_json(token, "getUpdates", params, timeout=timeout + 10)
    if not data or not data.get("ok"):
        return []
    return data.get("result", [])


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Строит inline-клавиатуру из строк кнопок вида (подпись, callback_data)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def send_message(
    token: str,
    chat_id: str | int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    """Отправка текстового сообщения (с разбивкой по лимиту).

    reply_markup (если задан) прикрепляется только к последнему фрагменту,
    чтобы кнопки оказались под итоговым текстом.
    """
    ok = True
    chunks = _split_message(text, _TG_LIMIT)
    for i, chunk in enumerate(chunks):
        payload: dict = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        ok = _post_json(token, "sendMessage", payload) and ok
    return ok


def answer_callback_query(token: str, callback_query_id: str, text: str = "") -> bool:
    """Гасит «часики» на нажатой inline-кнопке (по желанию показывает текст)."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    return _post_json(token, "answerCallbackQuery", payload)


def resolve_chat_id(token: str) -> str | None:
    """Пытается определить chat id из последних апдейтов (если юзер написал боту)."""
    data = _get_json(token, "getUpdates")
    if not data or not data.get("ok"):
        return None
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            log.info("Определён chat_id=%s из getUpdates", chat["id"])
            return str(chat["id"])
    return None


def _post_json(token: str, method: str, payload: dict) -> bool:
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ok = resp.status == 200
            if not ok:
                log.warning("Telegram %s вернул статус %s", method, resp.status)
            return ok
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.error("Telegram %s не отправлен: %s", method, e)
        return False


def _post_document(token: str, chat_id: str, file_path: str) -> bool:
    """sendDocument через multipart/form-data, собранный вручную."""
    if not os.path.exists(file_path):
        return False
    url = _API.format(token=token, method="sendDocument")
    boundary = uuid.uuid4().hex
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    add_field("chat_id", str(chat_id))
    add_field("caption", "Полный отчёт по вакансиям")
    parts.append(
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
            "Content-Type: text/markdown\r\n\r\n"
        ).encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        log.error("Telegram sendDocument не отправлен: %s", e)
        return False


def send_report(
    summary: str,
    report_path: str | None = None,
    token: str | None = None,
    chat_id: str | None = None,
    attach_file: bool = True,
) -> bool:
    """Отправляет краткое summary и (опционально) файл отчёта. True при успехе текста."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    if not token:
        log.warning("TELEGRAM_BOT_TOKEN не задан — отправка в Telegram пропущена")
        return False

    if not chat_id:
        log.info("TELEGRAM_CHAT_ID не задан — пробую определить через getUpdates")
        chat_id = resolve_chat_id(token)
    if not chat_id:
        log.warning(
            "Не удалось определить chat_id. Напишите боту /start или задайте TELEGRAM_CHAT_ID"
        )
        return False

    # Текст может превышать лимит — режем на части по границам строк.
    chunks = _split_message(summary, _TG_LIMIT)
    ok = True
    for chunk in chunks:
        ok = _post_json(token, "sendMessage", {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }) and ok

    if attach_file and report_path:
        doc_ok = _post_document(token, chat_id, report_path)
        log.info("Telegram: файл отчёта отправлен=%s", doc_ok)

    if ok:
        log.info("Telegram: summary отправлено в чат %s", chat_id)
    return ok


def _split_message(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks
