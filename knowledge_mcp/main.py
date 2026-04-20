import argparse
import os
import sys
import logging
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from pathlib import Path

from .db import KnowledgeDB
# НЕ импортируем Indexer на верхнем уровне!
# Indexer -> embeddings.py -> sentence_transformers -> torch (~30 сек импорт).
# Для MCP-команды Indexer не нужен вообще, поэтому импортируем лениво.

# ВАЖНО: Весь логгер должен идти в sys.stderr, иначе MCP (общающийся по stdout) сломается!
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

app = FastAPI(title="Knowledge Base Sync Trigger")

db = None
indexer = None

class SyncRequest(BaseModel):
    repo_id: str
    repo_path: str

def init_components(db_path: str):
    global db, indexer
    from .indexer import Indexer  # Ленивый импорт — только для sync/serve
    db = KnowledgeDB(db_path)
    indexer = Indexer(db)

@app.post("/sync")
async def sync_endpoint(req: SyncRequest, background_tasks: BackgroundTasks):
    """
    HTTP/Webhook Триггер. 
    Выполняет дельта-скан в фоне, не блокируя ответ планировщику (cron/github hooks).
    """
    if not db or not indexer:
        raise HTTPException(status_code=500, detail="Components not initialized")
    
    background_tasks.add_task(indexer.sync_repo, req.repo_id, req.repo_path)
    return {"status": "Sync queued", "repo_id": req.repo_id, "background": True}

def main():
    parser = argparse.ArgumentParser(description="Knowledge MCP - Indexer & Trigger Server")
    # Дефолт берётся из переменной окружения DB_PATH (Docker задаёт её автоматически),
    # а без неё — data/knowledge.db для локального запуска
    parser.add_argument("--db-path", type=str,
                        default=os.environ.get("DB_PATH", "data/knowledge.db"),
                        help="Path to SQLite DB (default: $DB_PATH or data/knowledge.db)")
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Режим ручного запуска (CLI-команда)
    sync_parser = subparsers.add_parser("sync", help="Run strict delta-sync for repository (CLI trigger)")
    sync_parser.add_argument("--repo-id", required=True, type=str, help="Repository ID")
    sync_parser.add_argument("--repo-path", required=True, type=str, help="Path to repository root")
    sync_parser.add_argument("--all", action="store_true", help="Index all directories without interactive prompt")
    
    # Режим сервера (HTTP endpoint)
    serve_parser = subparsers.add_parser("serve", help="Start HTTP server to listen for /sync webhook triggers")
    serve_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    
    # Режим MCP-сервера (Codex, Claude, Gemini)
    # По умолчанию эмбеддинги ОТКЛЮЧЕНЫ: загрузка torch (~30 сек) блокирует Python GIL,
    # что убивает MCP-протокол. FTS5 прекрасно справляется с поиском по коду.
    mcp_parser = subparsers.add_parser("mcp", help="Start the MCP stdio server")
    mcp_parser.add_argument("--with-embeddings", action="store_true",
                            help="Enable vector search (WARNING: loads ~400MB model, blocks for ~30s)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "mcp":
        # MCP-сервер имеет собственную лёгкую инициализацию (без Indexer/LocalEmbedder),
        # чтобы handshake проходил мгновенно и не блокировался загрузкой ML-модели.
        import asyncio
        from .server import start_mcp_server
        
        # Проверка: если БД не существует, MCP сервер может запуститься вхолостую
        if not os.path.exists(args.db_path):
            logger.error(f"Database file not found at {args.db_path}. Please check your volume mounts.")
            # Мы не выходим с ошибкой, так как SQLite создаст пустую БД, 
            # но мы хотя бы предупреждаем в stderr.
            
        use_emb = getattr(args, 'with_embeddings', False)
        asyncio.run(start_mcp_server(args.db_path, enable_embeddings=use_emb))
    else:
        # Для sync и serve нужен полный стек (включая Indexer с эмбеддингами)
        init_components(args.db_path)

        if args.command == "sync":
            allowed_top_level = None
            if not getattr(args, "all", False):
                import questionary
                repo_path_obj = Path(args.repo_path).resolve()
                if not repo_path_obj.exists():
                    logger.error(f"Path does not exist: {repo_path_obj}")
                    sys.exit(1)
                
                spec = indexer._get_ignore_spec(repo_path_obj)
                
                # Собираем элементы первого уровня
                choices = []
                for entry in os.scandir(repo_path_obj):
                    rel_p = entry.name + ('/' if entry.is_dir() else '')
                    if not spec.match_file(rel_p):
                        choices.append(entry.name)
                
                if not choices:
                    logger.warning("No unignored files/directories found at top level.")
                else:
                    selected = questionary.checkbox(
                        "Выберите папки/файлы для индексации (Enter - выбрать все отмеченные):",
                        choices=[questionary.Choice(c, checked=True) for c in sorted(choices)]
                    ).ask()
                    
                    if selected is None:
                        logger.info("Sync cancelled by user.")
                        sys.exit(0)
                        
                    allowed_top_level = selected

            logger.info(f"Manual CLI trigger logic for {args.repo_id}")
            indexer.sync_repo(args.repo_id, args.repo_path, allowed_top_level=allowed_top_level)
        elif args.command == "serve":
            logger.info(f"Starting trigger HTTP endpoint on {args.host}:{args.port}")
            uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
