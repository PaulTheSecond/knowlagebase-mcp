import asyncio
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from .db import KnowledgeDB
# НЕ импортируем LocalEmbedder на верхнем уровне!
# embeddings.py -> sentence_transformers -> torch (~30 сек на импорт).
# Импортируем лениво, только при первом вызове knowledge_search.

logger = logging.getLogger(__name__)

server = Server("knowledge-mcp")
db: KnowledgeDB = None
embedder = None  # Будет LocalEmbedder, загруженный лениво
use_embeddings = True


def _init_embedder_sync():
    """Синхронная инициализация модели (вызывается в отдельном потоке).
    
    КРИТИЧЕСКИ ВАЖНО: перенаправляем stdout -> stderr на время загрузки!
    torch и sentence_transformers печатают progress bars и warnings в stdout,
    что ломает MCP JSON-RPC протокол (MCP общается через stdout).
    """
    import sys
    original_stdout = sys.stdout
    sys.stdout = sys.stderr  # Всё что хочет напечатать torch — уйдёт в stderr
    try:
        from .embeddings import LocalEmbedder
        return LocalEmbedder()
    finally:
        sys.stdout = original_stdout  # Восстанавливаем stdout для MCP


def _embed_text_sync(emb, text: str):
    """Синхронный вызов inference (вызывается в отдельном потоке)."""
    return emb.embed_text(text)

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Экспортируем MCP инструменты с универсальным описанием для любых LLM (Codex, Claude, Gemini)."""
    return [
        types.Tool(
            name="knowledge_search",
            description=(
                "Search the local knowledge base across multiple repositories. "
                "Uses hybrid search (Full-Text + Vector Embeddings) for high recall. "
                "Returns relevant code and documentation chunks with trust levels ('verified' for code, 'hint' for docs)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (concept, code snippet, or natural language question)."},
                    "repo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of repo_ids to restrict the search. If omitted, searches all repositories."
                    },
                    "limit": {"type": "integer", "description": "Max results to return (default 10)."}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="knowledge_get_chunk",
            description="Retrieve detailed information and raw content of a specific knowledge base chunk by its ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string", "description": "The unique ID of the chunk."}
                },
                "required": ["chunk_id"]
            }
        ),
        types.Tool(
            name="knowledge_delete_repo",
            description="Delete all knowledge base data associated with a specific repository ID. This removes all scanned files, chunks, and embeddings for that repository.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "The unique ID of the repository to delete."}
                },
                "required": ["repo_id"]
            }
        ),
        types.Tool(
            name="knowledge_sync_repo",
            description="Force an immediate delta-sync (update) of the local knowledge base for a specific repository. Use this after you have made code changes to ensure the knowledge base is up to date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string", "description": "The unique ID of the repository."},
                    "repo_path": {"type": "string", "description": "The absolute local path to the repository root directory."}
                },
                "required": ["repo_id", "repo_path"]
            }
        ),
        types.Tool(
            name="knowledge_find_symbol",
            description="Find a code symbol (class, method, interface, etc.) by its name or pattern. Use asterisks for wildcards (e.g. '*Repository*').",
            inputSchema={
                "type": "object",
                "properties": {
                    "name_pattern": {"type": "string", "description": "The name or wildcard pattern of the symbol."},
                    "kind": {"type": "string", "description": "Optional kind of symbol (class, method, interface, property, etc)."},
                    "repo_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of repo_ids to restrict the search."
                    }
                },
                "required": ["name_pattern"]
            }
        ),
        types.Tool(
            name="knowledge_get_callers",
            description="Find all symbols that call or reference the specified symbol by its numeric ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_id": {"type": "integer", "description": "The exact numeric ID of the target symbol."}
                },
                "required": ["symbol_id"]
            }
        ),
        types.Tool(
            name="knowledge_get_callees",
            description="Find all symbols that are called or referenced by the specified symbol by its numeric ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_id": {"type": "integer", "description": "The exact numeric ID of the source symbol."}
                },
                "required": ["symbol_id"]
            }
        ),
        types.Tool(
            name="knowledge_get_hierarchy",
            description="Get the inheritance and implementation hierarchy for the specified symbol by its numeric ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_id": {"type": "integer", "description": "The exact numeric ID of the symbol."}
                },
                "required": ["symbol_id"]
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Обработчик всех MCP-инструментов. Возвращает форматированный markdown без специфичных тегов."""
    if name == "knowledge_search":
        query = arguments.get("query")
        if not query:
            return [types.TextContent(type="text", text="Error: 'query' argument is required.")]
            
        repo_ids = arguments.get("repo_ids", [])
        limit = arguments.get("limit", 10)
        
        try:
            results = []
            if use_embeddings:
                global embedder
                if embedder is None:
                    logger.info("Lazy loading LocalEmbedder in background thread (~30s first time)...")
                    # asyncio.to_thread: не блокируем event loop, MCP-протокол остаётся живым
                    embedder = await asyncio.to_thread(_init_embedder_sync)
                    logger.info("Embedder ready!")
                # Inference тоже в отдельном потоке (CPU-bound операция)
                vector = await asyncio.to_thread(_embed_text_sync, embedder, query)
                if vector:
                    results = db.search_chunks_hybrid(query, vector, repo_ids=repo_ids, limit=limit)
                else:
                    results = db.search_chunks_fts(query, repo_ids=repo_ids, limit=limit)
            else:
                results = db.search_chunks_fts(query, repo_ids=repo_ids, limit=limit)
                
            if not results:
                return [types.TextContent(type="text", text="No results found in the knowledge base. Try altering your query or removing repo_ids limits.")]
                
            formatted = []
            for r in results:
                formatted.append(
                    f"--- Chunk ID: {r['id']} ---\n"
                    f"**Repository**: `{r['repo_id']}`\n"
                    f"**File**: `{r['path']}` (Lines: {r['line_start']}-{r['line_end']})\n"
                    f"**Context Trust Level**: {r['trust']} (Source: {r['source_kind']})\n\n"
                    f"```\n{r['content']}\n```\n"
                )
            # Возвращаем склеенный текст без привязки к конкретному агенту
            return [types.TextContent(type="text", text="\n".join(formatted))]
            
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return [types.TextContent(type="text", text=f"Knowledge Base DB Error: {e}")]
            
    elif name == "knowledge_get_chunk":
        chunk_id = arguments.get("chunk_id")
        if not chunk_id:
            return [types.TextContent(type="text", text="Error: 'chunk_id' argument is required.")]
            
        try:
            cursor = db.conn.cursor()
            cursor.execute('''
                SELECT c.*, f.repo_id, f.path 
                FROM chunks c 
                JOIN files f ON c.file_id = f.id 
                WHERE c.id = ?
            ''', (chunk_id,))
            row = cursor.fetchone()
            
            if not row:
                return [types.TextContent(type="text", text=f"Chunk '{chunk_id}' not found.")]
                
            text = (
                f"**Repository**: `{row['repo_id']}`\n"
                f"**File**: `{row['path']}`\n"
                f"**Trust Level**: {row['trust']}\n"
                f"**File Hash**: {row['sha']}\n\n"
                f"```\n{row['content']}\n```"
            )
            return [types.TextContent(type="text", text=text)]
            
        except Exception as e:
            logger.error(f"Get chunk failed: {e}")
            return [types.TextContent(type="text", text=f"Database query error: {e}")]
            
    elif name == "knowledge_delete_repo":
        repo_id = arguments.get("repo_id")
        if not repo_id:
            return [types.TextContent(type="text", text="Error: 'repo_id' argument is required.")]
            
        try:
            deleted_files = db.delete_repo(repo_id)
            if deleted_files > 0:
                return [types.TextContent(type="text", text=f"Successfully deleted all data for repository '{repo_id}' ({deleted_files} files removed from index).")]
            else:
                return [types.TextContent(type="text", text=f"Repository '{repo_id}' not found or already empty.")]
        except Exception as e:
            logger.error(f"Delete repo failed: {e}")
            return [types.TextContent(type="text", text=f"Database error while deleting repository: {e}")]
            
    elif name == "knowledge_sync_repo":
        repo_id = arguments.get("repo_id")
        repo_path = arguments.get("repo_path")
        if not repo_id or not repo_path:
            return [types.TextContent(type="text", text="Error: 'repo_id' and 'repo_path' arguments are required.")]
            
        try:
            from .indexer import Indexer
            import sys
            
            def _run_sync():
                # КРИТИЧЕСКИ ВАЖНО: перенаправляем stdout -> stderr, 
                # чтобы torch и progress bars не сломали MCP JSON-RPC
                original_stdout = sys.stdout
                sys.stdout = sys.stderr
                try:
                    indexer = Indexer(db, use_embeddings=use_embeddings)
                    indexer.sync_repo(repo_id, repo_path, allowed_top_level=None)
                finally:
                    sys.stdout = original_stdout

            logger.info(f"Triggering background sync for '{repo_id}' at '{repo_path}'")
            # Ждём завершения синхронизации, чтобы агент имел актуальные данные сразу после ответа
            await asyncio.to_thread(_run_sync)
            return [types.TextContent(type="text", text=f"Successfully synchronized repository '{repo_id}'. The knowledge base is now up to date.")]
        except Exception as e:
            logger.error(f"Sync failed: {e}")
            return [types.TextContent(type="text", text=f"Failed to synchronize repository: {e}")]
            
    elif name == "knowledge_find_symbol":
        pattern = arguments.get("name_pattern")
        kind = arguments.get("kind")
        repo_ids = arguments.get("repo_ids", [])
        
        if not pattern:
            return [types.TextContent(type="text", text="Error: 'name_pattern' argument is required.")]
            
        try:
            results = db.find_symbols(pattern, kind=kind, repo_ids=repo_ids, limit=20)
            if not results:
                return [types.TextContent(type="text", text=f"No symbols found matching '{pattern}'.")]
                
            formatted = []
            for r in results:
                formatted.append(
                    f"--- Symbol ID: {r['id']} ---\n"
                    f"**Name**: `{r['name']}` (Qualified: `{r['qualified_name']}`)\n"
                    f"**Repository**: `{r['repo_id']}`\n"
                    f"**File**: `{r['path']}` (Lines: {r['line_start']}-{r['line_end']})\n"
                    f"**Kind**: {r['kind']} ({r['language']})\n"
                    f"**Signature**: `{r['signature']}`\n"
                )
            return [types.TextContent(type="text", text="\n".join(formatted))]
        except Exception as e:
            logger.error(f"Find symbol failed: {e}")
            return [types.TextContent(type="text", text=f"Database query error: {e}")]

    elif name in ("knowledge_get_callers", "knowledge_get_callees", "knowledge_get_hierarchy"):
        symbol_id = arguments.get("symbol_id")
        if not symbol_id:
            return [types.TextContent(type="text", text="Error: 'symbol_id' is required.")]
            
        try:
            if name == "knowledge_get_callers":
                results = db.get_callers(symbol_id)
                header = f"Callers for symbol ID {symbol_id}:"
            elif name == "knowledge_get_callees":
                results = db.get_callees(symbol_id)
                header = f"Callees of symbol ID {symbol_id}:"
            else:
                results = db.get_hierarchy(symbol_id)
                header = f"Hierarchy for symbol ID {symbol_id}:"
                
            if not results:
                return [types.TextContent(type="text", text=f"No graph edges found for symbol {symbol_id} in this context.")]
                
            formatted = [header]
            for r in results:
                edge_kind = r['edge_kind']
                formatted.append(
                    f"- [{edge_kind}] ID: {r['id']} | `{r['qualified_name']}` ({r['kind']} in {r['repo_id']}/{r['path']})"
                )
            return [types.TextContent(type="text", text="\n".join(formatted))]
        except Exception as e:
            logger.error(f"Graph query failed: {e}")
            return [types.TextContent(type="text", text=f"Database query error: {e}")]
        
    else:
        raise ValueError(f"Unknown tool: {name}")

async def start_mcp_server(db_path: str, enable_embeddings: bool = True):
    """Инициализация БД и запуск MCP-сервера по STDIO (стандарт для локальных агентов)."""
    global db, use_embeddings
    
    db = KnowledgeDB(db_path)
    use_embeddings = enable_embeddings
    # Ленивая загрузка embedder = LocalEmbedder() перенесена внутрь call_tool, 
    # чтобы избежать 10-секундного таймаута при инициализации (handshake) протокола MCP.
        
    # MCP-протокол общения через потоки stdin/stdout
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )
