FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Данные, которые бот пишет во время работы (кэш «виденных» лотов и настройки),
# складываем в /data, чтобы удобно было монтировать томом.
ENV SEEN_FILE=/data/seen_listings.json \
    OVERRIDES_FILE=/data/runtime_settings.json
RUN mkdir -p /data

CMD ["python", "main.py"]
