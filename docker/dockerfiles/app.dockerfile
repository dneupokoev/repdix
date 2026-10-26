# docker/dockerfiles/app.dockerfile
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Системные зависимости (минимальный набор для aiomysql, asyncpg, aiosqlite)
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN ln -snf /usr/share/zoneinfo/Europe/Moscow /etc/localtime

# Устанавливаем Python-зависимости
COPY ./requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Копируем код приложения
COPY ./app ./app

# Копируем README (опционально)
COPY README.md .

# Создаём директории для конфигов, логов и данных
RUN mkdir -p /app/config /app/logs /app/data

# Команда запуска
CMD ["python3", "-m", "app.main"]