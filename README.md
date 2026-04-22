# Knowledge MCP

![Version v1.2](https://img.shields.io/badge/version-v1.2-blue.svg)

A high-precision local knowledge base server (RAG) that implements the **Model Context Protocol (MCP)**. It enables AI agents (Codex, Claude Code, Gemini CLI, etc.) to navigate, search, and reason about complex codebases using a hybrid approach combining semantic, lexical, and structural analysis.

## 🚀 Key Features

1. **Hybrid Triple Search**: Fuses three distinct retrieval channels through **Reciprocal Rank Fusion (RRF)** for maximum recall:
   - **Full-Text Search (FTS5)**: Handles exact name matches and specific keywords.
   - **Semantic Vector Search (`sqlite-vec`)**: Understands concepts and natural language intent.
   - **Graph-Based Retrieval**: Provides a 2x relevance boost to actual code symbols and their relationships.
2. **Deep Semantic Indexing**:
   - **C# / .NET**: Integrated **Roslyn** analysis for precise symbol extraction and dependency graphs.
   - **Polyglot Support**: **Tree-sitter** integration for high-quality parsing of TS, JS, Python, Go, and more.
   - **Markdown**: Section-aware chunking for documentation.
3. **Knowledge Graph**: Tracks relationships between symbols: `CALLS`, `INHERITS`, `IMPLEMENTS`, and `IMPORTS`. Supports recursive **Impact Analysis** to estimate the blast radius of code changes.
4. **Autonomous Embeddings**: In-process generation using `sentence-transformers` (mpnet-base-v2). Works natively inside Docker without external API dependencies.
5. **Incremental Sync**: Delta-sync mechanism via `mtime` and `SHA-256` ensures only modified files are processed, significantly speeding up updates.

## 🛠 Available MCP Tools

### Search & Retrieval
- **`knowledge_search`**: Triple hybrid search across all repositories. Returns chunks with trust levels (`verified` for code, `hint` for docs).
- **`knowledge_get_chunk`**: Retrieve detailed content and metadata for a specific knowledge chunk.

### Symbol & Graph Navigation
- **`knowledge_find_symbol`**: Locate classes, methods, and interfaces using wildcards (e.g., `*Repository`).
- **`knowledge_get_callers` / `knowledge_get_callees`**: Navigate the call graph of any symbol.
- **`knowledge_get_hierarchy`**: Explore inheritance and interface implementations.
- **`knowledge_impact_analysis`**: Perform recursive dependency analysis to find everything affected by a symbol change.

### Management
- **`knowledge_sync_repo`**: Trigger a background delta-sync for a repository to update the AI's "memory" after code changes.
- **`knowledge_delete_repo`**: Wipe all indexed data for a specific repository.

## 📋 Requirements

- **Python 3.10+**
- **.NET 8.0 SDK** (Required for Roslyn-based C# analysis)
- **Docker** (Recommended for easiest deployment)

## 🐳 Quick Start (Docker)

### 1. Configure your repository path

Create a `.env` file in the project root:
```env
# The host directory that will be mounted as /repos inside the container.
# Set this to the PARENT folder of your repositories.
REPOS_DIR=C:\Repos
```

### 2. Build and start the container
```bash
docker compose up -d
```

### 3. Index a specific project

**Option A — PowerShell script** (recommended, see [Scripts](#-scripts)):
```powershell
.\scripts\Reindex-Repo.ps1 -Wait
```

**Option B — direct HTTP call:**
```bash
curl -X POST http://localhost:8000/sync \
     -H "Content-Type: application/json" \
     -d '{"repo_id": "my-app", "repo_path": "/repos/my-app"}'
```

> ⚠️ `repo_path` is the path **inside the container** (e.g., `/repos/my-app`), not the host path.

### 4. Connect your MCP Client

Add this to your AI client's config (e.g., `mcp.json`):
```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "docker",
      "args": ["exec", "-i", "knowledge-mcp", "python", "-m", "knowledge_mcp.main", "mcp"]
    }
  }
}
```

---

## 🔧 Scripts

### `scripts/Reindex-Repo.ps1`

A PowerShell helper script that triggers re-indexing of a repository by sending a POST request to the running `knowledge-mcp` server.

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `-RepoId` | `ImpactOS.Core.Lib` | Unique repository identifier used as a key in the database |
| `-RepoPath` | `/repos/ImpactOS.Core.Lib` | Path to the repository **inside the Docker container** |
| `-ServerUrl` | `http://localhost:8000` | URL of the running knowledge-mcp server |
| `-Wait` | `$false` | If set, streams container logs after triggering sync |

**Usage examples:**

```powershell
# Trigger re-indexing with default settings (ImpactOS.Core.Lib)
.\scripts\Reindex-Repo.ps1

# Trigger and watch progress in real time
.\scripts\Reindex-Repo.ps1 -Wait

# Index a different repository
.\scripts\Reindex-Repo.ps1 -RepoId "MyOtherLib" -RepoPath "/repos/MyOtherLib"

# Point to a remote server
.\scripts\Reindex-Repo.ps1 -ServerUrl "http://192.168.1.100:8000" -Wait
```

**How it works:**
1. Verifies the server is reachable at `ServerUrl`.
2. Sends a `POST /sync` request with `repo_id` and `repo_path`.
3. Indexing runs in the background inside the container.
4. With `-Wait`, streams live logs via `docker logs -f`.

> 💡 **Path mapping:** If `REPOS_DIR=C:\Repos`, then `C:\Repos\MyLib` on the host
> is accessible inside the container as `/repos/MyLib`.

---

## 📐 Architecture & Decisions

- [Roadmap: Autonomous Sync & Webhooks](docs/roadmap_autonomous_sync.md)

For deep dives into the technical design, see our Architecture Decision Records:
- [ADR-001: Local MCP RAG Foundation](docs/adr-001-local-mcp-rag-knowledge-base.md)
- [ADR-002: Hybrid Semantic & Graph Indexing](docs/adr-002-hybrid-semantic-graph-indexing.md)
