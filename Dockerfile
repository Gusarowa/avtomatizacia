FROM python:3.12-slim

WORKDIR /app

# Сначала зависимости — для кэширования слоёв
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код
COPY . .

# Бот не падает без PORT, но если хостинг его задаёт — поднимем health-эндпоинт.
ENV PYTHONUNBUFFERED=1

# Запускаем именно бота (long polling). Один экземпляр!
CMD ["python", "bot.py"]
