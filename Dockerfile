FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Копирование файла зависимостей
COPY requirements.txt .

# Устанавливаем системные зависимости для .NET
RUN apt-get update && apt-get install -y wget ca-certificates libicu-dev && \
    wget https://dot.net/v1/dotnet-install.sh -O dotnet-install.sh && \
    bash ./dotnet-install.sh --channel 8.0 --install-dir /usr/share/dotnet && \
    rm dotnet-install.sh && \
    ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet && \
    rm -rf /var/lib/apt/lists/*

# Устанавливаем CPU-only версию PyTorch (экономия ~1.5 ГБ RAM и диска по сравнению с полным CUDA-пакетом)
# Затем остальные зависимости
RUN pip install --no-cache-dir \
        torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Копируем исходники приложения
COPY knowledge_mcp/ /app/knowledge_mcp/

# Копируем и собираем C# парсер солюшенов
COPY RoslynParser/ /app/RoslynParser/
RUN cd /app/RoslynParser && dotnet build -c Release

# Создаем папку для базы данных по умолчанию
RUN mkdir -p /data
ENV DB_PATH=/data/knowledge.db

# Настройка по умолчанию: старт HTTP-сервера триггеров.
# Для использования агентами через stdio нужно будет запускать с командой `mcp`
CMD ["python", "-m", "knowledge_mcp.main", "--db-path", "/data/knowledge_roslyn.db", "serve", "--host", "0.0.0.0", "--port", "8000"]
