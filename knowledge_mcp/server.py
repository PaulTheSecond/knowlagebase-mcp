import asyncio
import logging
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types

from .db import KnowledgeDB
from .embeddings import LocalEmbedder

logger = logging.getLogger(__name__)

server = Server("knowledge-mcp")
db: KnowledgeDB = None
embedder: LocalEmbedder = None
use_embeddings = True

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
            if use_embeddings and embedder:
                vector = embedder.embed_text(query)
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
        
    else:
        raise ValueError(f"Unknown tool: {name}")

async def start_mcp_server(db_path: str, enable_embeddings: bool = True):
    """Инициализация БД и запуск MCP-сервера по STDIO (стандарт для локальных агентов)."""
    global db, embedder, use_embeddings
    
    db = KnowledgeDB(db_path)
    use_embeddings = enable_embeddings
    if use_embeddings:
        embedder = LocalEmbedder()
        
    # MCP-протокол общения через потоки stdin/stdout
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )
