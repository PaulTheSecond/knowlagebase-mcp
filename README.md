# Knowledge MCP

A local knowledge base server (RAG) for integrating your codebase with Model Context Protocol (MCP) clients (such as Codex, Claude Code, Gemini CLI, etc.).

## Features

1. **Incremental repository indexing**: Delta-sync mechanism based on `mtime` and `SHA-256` prevents redundant processing.
2. **Context-aware parsing**: Extracts source code and Markdown documentation, assigning rigorous trust levels: `verified` for code and `hint` for docs.
3. **Autonomous Embeddings**: In-process embedding generation using built-in `sentence-transformers` ML models (vector search works natively without relying on external services like Ollama).
4. **Hybrid Search**: Fuses Full-Text Search (FTS5) and Semantic Vector Search (`sqlite-vec`) through Reciprocal Rank Fusion, exposed via the MCP protocol (`knowledge_search` tool).

## Requirements

- Python 3.10+
- Core dependencies: `mcp`, `sqlite-vec`, `fastapi`, `uvicorn`, `sentence-transformers`, `pathspec`.

## Quick Start (Local Setup)

1. **Create and activate a virtual environment, then install dependencies:**
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\Activate.ps1
   # On macOS/Linux:
   # source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Perform initial repository synchronization:**
   ```bash
   python -m knowledge_mcp.main sync --repo-id my-project --repo-path ./src/my-project
   ```

3. **(Optional) Start the HTTP webhook server for remote triggers:**
   ```bash
   python -m knowledge_mcp.main serve --port 8000
   ```
   *You can then trigger synchronization via API:*
   `POST http://localhost:8000/sync` (Body: `{"repo_id": "my-project", "repo_path": "/repos/my-project"}`)

## Connecting as an MCP Server

MCP clients typically use the `stdio` mode for local, offline interaction. Add the following to your AI client's configuration file (e.g., `mcp.json` / Codex settings):

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "python",
      "args": ["-m", "knowledge_mcp.main", "mcp"],
      "cwd": "/absolute/path/to/knowledgebase-mcp"
    }
  }
}
```
*Note: If you want to disable vector search and rely solely on FTS5 (for lighter CPU usage), append the `--no-embeddings` flag to the `args` array.*

## Running via Docker (Recommended for Universal Usage)

1. **Build and spin up the background webhook daemon:**
   ```bash
   docker compose up -d
   ```

2. **Mount your repositories:**
   Place your projects inside the `./src` directory.
   *(Alternatively, create a `.env` file in the root directory and explicitly set your path: `REPOS_DIR=/absolute/path/to/your/projects`).*

3. **Sync your mounted project via the API:**
   ```bash
   curl -X POST http://localhost:8000/sync \
        -H "Content-Type: application/json" \
        -d '{"repo_id": "my-project", "repo_path": "/repos/my-project"}'
   ```

4. **Connect your MCP Client to the running Docker instance:**
   To start an ephemeral MCP session with your AI agent (Codex, Claude) using the pre-warmed background container, add this command to your client config:
   ```json
   {
     "mcpServers": {
       "knowledge-mcp": {
         "command": "docker",
         "args": [
           "exec",
           "-i",
           "knowlagebase-mcp-knowledge-mcp-1",
           "python",
           "-m",
           "knowledge_mcp.main",
           "mcp"
         ]
       }
     }
   }
   ```
