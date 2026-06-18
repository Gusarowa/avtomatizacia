# Деплой бота на хостинг (Koyeb)

Бот работает на long polling, поэтому ему нужен один всегда-включённый процесс.
Ниже — деплой на **Koyeb** из GitHub (бесплатный always-on инстанс). Файлы `Dockerfile`
и health-эндпоинт уже готовы.

## Важно
- Должен крутиться **ровно один** экземпляр бота. Если параллельно запустить второй
  (например, локально на ноуте) — Telegram отдаст ошибку `409 Conflict`. Перед деплоем
  останови локального бота.
- Секреты (`TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`) задаются в UI Koyeb, а не в git.
- Файловая система эфемерная: без тома `data/profiles.json` и `data/subscriptions.json`
  сбрасываются при каждом передеплое (анкеты и подписки обнулятся!).

## Постоянное хранилище (важно)

Анкеты и подписки лежат в каталоге `AGENT_DATA_DIR` (по умолчанию `data`). Чтобы они
переживали редеплой, подключи постоянный том и направь туда переменную:

- **Koyeb**: создай Volume и примонтируй, например, в `/data`. Затем задай env
  `AGENT_DATA_DIR=/data`.
- **Railway**: Service → **Variables** → добавь `AGENT_DATA_DIR=/data`; Service →
  **Settings → Volumes** → New Volume с mount path `/data`.

Без этого бот продолжит работать, но будет «забывать» анкеты после каждого деплоя.

## Ежедневный дайджест

Бот сам раз в сутки ищет новые вакансии под анкету подписчиков (`/subscribe`) и
присылает только то, чего раньше не показывал. Настройки (необязательно):

- `AGENT_DIGEST_INTERVAL_SEC` — период на подписчика (по умолчанию `86400` = раз в день).
- `AGENT_DIGEST_TICK_SEC` — как часто воркер проверяет очередь (по умолчанию `900` = 15 мин).

## Шаги

1. Залей проект в GitHub (см. ниже «Git → GitHub»).
2. Зайди на https://app.koyeb.com → **Create Service** → **GitHub** → выбери репозиторий.
3. Build: Koyeb сам увидит `Dockerfile` (Builder = Dockerfile).
4. Тип сервиса: **Web Service** (health-эндпоинт уже отвечает на `$PORT`).
   - Health check: HTTP, path `/`, port — тот, что Koyeb положит в `PORT` (по умолчанию 8000).
5. Instance: подойдёт самый маленький (Free / Nano).
6. Environment variables (вкладка Environment):
   - `TELEGRAM_BOT_TOKEN` = токен бота
   - `OPENROUTER_API_KEY` = ключ OpenRouter (необязательно)
   - `AGENT_LLM_MODEL` = список бесплатных моделей (см. `.env.example`)
   - `AGENT_DATA_DIR` = путь к смонтированному тому (например, `/data`) — для персиста анкет/подписок
7. Deploy. В логах должно появиться: `Health-сервер слушает порт ...` и `Бот запущен. Жду сообщений…`.
8. Проверь бота в Telegram: `/start`, затем `/anketa` и `/find`.

## Git → GitHub

```bash
git init
git add .
git commit -m "Агент подбора junior-вакансий: CLI, trudvsem, Telegram-бот, анкета, LLM"
# создай пустой репозиторий на github.com, затем:
git remote add origin https://github.com/<USER>/<REPO>.git
git branch -M main
git push -u origin main
```

Либо через GitHub CLI:

```bash
gh repo create <REPO> --public --source=. --remote=origin --push
```

## Альтернативы
- **Railway**: New Project → Deploy from GitHub → задать env-переменные. Тоже видит Dockerfile.
- **Fly.io**: `fly launch` (использует Dockerfile), затем `fly secrets set TELEGRAM_BOT_TOKEN=...`.
- **Oracle Cloud Always Free**: VM + `systemd`-сервис, запускающий `python bot.py` под `.venv`.
