#!/bin/sh

# Ожидание БД (в реальном проекте лучше использовать wait-for-it)
# sleep 5

# Запуск приложения
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
