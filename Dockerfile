# ── Stage 1: Сборка RoslynParser ───────────────────────────────────────────
# Используем официальный SDK-образ — не нужен wget + bash-скрипт (~210 MB)
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS dotnet-builder

WORKDIR /roslyn
COPY RoslynParser/ .
# publish создаёт плоский каталог со всеми DLL-зависимостями Roslyn
RUN dotnet publish -c Release -o /roslyn/out

# ── Stage 2: финальный образ — SDK нужен RoslynParser (MSBuildLocator) ────────
# Используем dotnet SDK как базу и доустанавливаем Python.
# dotnet/runtime:8.0 НЕ подходит: MSBuildLocator ищет SDK при запуске и падает.
FROM mcr.microsoft.com/dotnet/sdk:8.0

# Устанавливаем Python 3.11 и libicu (нужна для Roslyn Unicode-обработки)
RUN apt-get update && apt-get install -y \
    python3.11 python3.11-venv python3-pip libicu-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

WORKDIR /app

# ── Python зависимости (отдельные слои = максимальный кеш) ─────────────────
COPY requirements.txt .

# torch в отдельном RUN — инвалидируется только при смене версии torch,
# а не при любом изменении requirements.txt
RUN pip install --no-cache-dir --break-system-packages torch --index-url https://download.pytorch.org/whl/cpu

# Остальные зависимости — инвалидируются только при изменении requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# ── Код приложения ──────────────────────────────────────────────────────────
# Копируется последним — изменение кода не инвалидирует кеш pip и dotnet
COPY knowledge_mcp/ /app/knowledge_mcp/

# Копируем собранный RoslynParser из stage 1 в путь, который ожидает indexer.py
# indexer.py: Path(__file__).parent.parent / "RoslynParser" / "bin" / "Release" / "net8.0" / "RoslynParser.dll"
COPY --from=dotnet-builder /roslyn/out /app/RoslynParser/bin/Release/net8.0/

RUN mkdir -p /data
ENV DB_PATH=/data/knowledge.db

CMD ["python", "-m", "knowledge_mcp.main", "--db-path", "/data/knowledge_roslyn.db", "serve", "--host", "0.0.0.0", "--port", "8000"]
