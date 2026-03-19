import argparse
import sys
import logging
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from .db import KnowledgeDB
from .indexer import Indexer

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
    parser.add_argument("--db-path", type=str, default="knowledge.db", help="Path to SQLite DB")
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Режим ручного запуска (CLI-команда)
    sync_parser = subparsers.add_parser("sync", help="Run strict delta-sync for repository (CLI trigger)")
    sync_parser.add_argument("--repo-id", required=True, type=str, help="Repository ID")
    sync_parser.add_argument("--repo-path", required=True, type=str, help="Path to repository root")
    
    # Режим сервера (HTTP endpoint)
    serve_parser = subparsers.add_parser("serve", help="Start HTTP server to listen for /sync webhook triggers")
    serve_parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    
    # Режим MCP-сервера (Codex, Claude, Gemini)
    mcp_parser = subparsers.add_parser("mcp", help="Start the MCP stdio server")
    mcp_parser.add_argument("--no-embeddings", action="store_true", help="Disable heavy vector search")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    init_components(args.db_path)

    if args.command == "sync":
        logger.info(f"Manual CLI trigger logic for {args.repo_id}")
        indexer.sync_repo(args.repo_id, args.repo_path)
    elif args.command == "serve":
        logger.info(f"Starting trigger HTTP endpoint on {args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "mcp":
        # Запускаем асинхронный сервер MCP
        import asyncio
        from .server import start_mcp_server
        asyncio.run(start_mcp_server(args.db_path, enable_embeddings=not args.no_embeddings))

if __name__ == "__main__":
    main()
