FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Копирование и установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY knowledge_mcp/ /app/knowledge_mcp/

# Создание директории для базы данных (volume mount point)
RUN mkdir -p /data
ENV DB_PATH=/data/knowledge.db

# Настройка по умолчанию: старт HTTP-сервера триггеров.
# Для использования агентами через stdio нужно будет запускать с командой `mcp`
CMD ["python", "-m", "knowledge_mcp.main", "serve", "--host", "0.0.0.0", "--port", "8000", "--db-path", "/data/knowledge.db"]
