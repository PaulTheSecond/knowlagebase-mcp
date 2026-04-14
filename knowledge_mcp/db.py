import sqlite3
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

import sqlite_vec

logger = logging.getLogger(__name__)

class KnowledgeDB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        
        # Загрузка расширения sqlite-vec
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        
        self.vec_dim = 768  # Утвержден для Ollama nomic-embed-text
        self._in_transaction = False  # Флаг: внутри явной транзакции (skip individual commits)
        self._apply_migrations()

    def _apply_migrations(self):
        """EF Core стайл миграции схемы БД."""
        cursor = self.conn.cursor()
        
        # Таблица истории миграций
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS __migrations_history (
                id TEXT PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

        cursor.execute("SELECT id FROM __migrations_history")
        applied_migrations = {row['id'] for row in cursor.fetchall()}

        migrations = [
            {
                "id": "20260319_01_initial",
                "up": """
                    CREATE TABLE IF NOT EXISTS files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        repo_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        hash TEXT NOT NULL,
                        UNIQUE(repo_id, path)
                    );
                    CREATE TABLE IF NOT EXISTS chunks (
                        id TEXT PRIMARY KEY,
                        file_id INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        line_start INTEGER,
                        line_end INTEGER,
                        source_kind TEXT NOT NULL,
                        trust TEXT NOT NULL,
                        sha TEXT,
                        FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
                    );
                """
            },
            {
                "id": "20260319_02_fts_and_vec",
                "up": f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        content
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                        embedding float[{self.vec_dim}]
                    );

                    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                        DELETE FROM chunks_fts WHERE rowid = old.rowid;
                        DELETE FROM chunks_vec WHERE rowid = old.rowid;
                    END;
                    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                        DELETE FROM chunks_fts WHERE rowid = old.rowid;
                        DELETE FROM chunks_vec WHERE rowid = old.rowid;
                        INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
                    END;
                """
            },
            {
                "id": "20260326_03_fix_triggers",
                "up": """
                    DROP TRIGGER IF EXISTS chunks_ad;
                    DROP TRIGGER IF EXISTS chunks_au;

                    CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
                        DELETE FROM chunks_fts WHERE rowid = old.rowid;
                        DELETE FROM chunks_vec WHERE rowid = old.rowid;
                    END;

                    CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
                        DELETE FROM chunks_fts WHERE rowid = old.rowid;
                        DELETE FROM chunks_vec WHERE rowid = old.rowid;
                        INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
                    END;
                """
            }
        ]

        # Добавляем новую миграцию
        migrations.append({
            "id": "20260414_04_symbols_graph",
            "up": """
                CREATE TABLE IF NOT EXISTS symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT,
                    kind TEXT NOT NULL,
                    language TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    signature TEXT,
                    chunk_id TEXT,
                    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
                    FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS symbol_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES symbols(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_id) REFERENCES symbols(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS unresolved_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    target_qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    FOREIGN KEY(source_id) REFERENCES symbols(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);
                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
                CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON symbol_edges(source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON symbol_edges(target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_kind ON symbol_edges(kind);
            """
        })

        for migration in migrations:
            if migration["id"] not in applied_migrations:
                logger.info(f"Applying sqlite migration: {migration['id']}")
                self.conn.executescript(migration["up"])
                cursor.execute("INSERT INTO __migrations_history (id) VALUES (?)", (migration["id"],))
                self.conn.commit()
    
    def close(self):
        """Закрывает соединение с БД."""
        self.conn.close()

    def begin_transaction(self):
        """Начинает явную транзакцию для пакетных операций (избегаем тысяч отдельных commit)."""
        self.conn.execute("BEGIN")
        self._in_transaction = True

    def commit_transaction(self):
        """Фиксирует текущую транзакцию."""
        self.conn.commit()
        self._in_transaction = False

    def rollback_transaction(self):
        """Откатывает текущую транзакцию при ошибке."""
        self.conn.rollback()
        self._in_transaction = False

    def _auto_commit(self):
        """Вызывает commit() только если мы НЕ внутри явной транзакции."""
        if not self._in_transaction:
            self.conn.commit()

    def get_known_files(self, repo_id: str) -> Dict[str, sqlite3.Row]:
        """Возвращает словарь известных файлов репозитория: path -> Row(id, mtime, hash)."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, path, mtime, hash FROM files WHERE repo_id = ?", (repo_id,))
        return {row['path']: row for row in cursor.fetchall()}

    def upsert_file(self, repo_id: str, path: str, mtime: float, file_hash: str) -> int:
        """Обновляет или вставляет запись о файле. Возвращает file_id."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO files (repo_id, path, mtime, hash)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo_id, path) DO UPDATE SET
                mtime=excluded.mtime,
                hash=excluded.hash
        """, (repo_id, path, mtime, file_hash))
        self._auto_commit()
        
        cursor.execute("SELECT id FROM files WHERE repo_id = ? AND path = ?", (repo_id, path))
        return cursor.fetchone()['id']

    def delete_file(self, file_id: int):
        """Удаляет файл и каскадно (благодаря FOREIGN KEY) удаляет все его чанки."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self._auto_commit()

    def delete_repo(self, repo_id: str) -> int:
        """Удаляет все файлы и чанки (каскадно), связанные с репозиторием."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
        deleted_count = cursor.rowcount
        self._auto_commit()
        return deleted_count

    def clear_file_chunks(self, file_id: int):
        """Удаляет все чанки, привязанные к заданному файлу (перед реиндексацией 'грязного' файла)."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        self._auto_commit()

    def clear_file_symbols(self, file_id: int):
        """Удаляет все символы, привязанные к файлу (связи удалятся каскадно)."""
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        self._auto_commit()

    def add_symbol(self, file_id: int, name: str, qualified_name: str, kind: str, 
                   language: str, line_start: int, line_end: int, signature: str, chunk_id: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO symbols (file_id, name, qualified_name, kind, language, line_start, line_end, signature, chunk_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (file_id, name, qualified_name, kind, language, line_start, line_end, signature, chunk_id))
        self._auto_commit()
        return cursor.lastrowid

    def add_symbol_edge(self, source_id: int, target_id: int, kind: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO symbol_edges (source_id, target_id, kind)
            VALUES (?, ?, ?)
        """, (source_id, target_id, kind))
        self._auto_commit()

    def add_unresolved_ref(self, source_id: int, target_qualified_name: str, kind: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO unresolved_refs (source_id, target_qualified_name, kind)
            VALUES (?, ?, ?)
        """, (source_id, target_qualified_name, kind))
        self._auto_commit()

    def find_symbols(self, name_pattern: str, kind: Optional[str] = None, repo_ids: Optional[List[str]] = None, limit: int = 20) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        sql = '''
            SELECT s.*, f.repo_id, f.path
            FROM symbols s
            JOIN files f ON s.file_id = f.id
            WHERE (s.name LIKE ? OR s.qualified_name LIKE ?)
        '''
        # Replace * with % for SQL LIKE
        sql_pattern = name_pattern.replace('*', '%')
        if '%' not in sql_pattern:
            sql_pattern = f"%{sql_pattern}%"
            
        params = [sql_pattern, sql_pattern]
        
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
            
        if repo_ids:
            placeholders = ','.join('?' for _ in repo_ids)
            sql += f" AND f.repo_id IN ({placeholders})"
            params.extend(repo_ids)
            
        sql += " LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        return cursor.fetchall()
        
    def get_callers(self, symbol_id: int) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT s.*, f.repo_id, f.path, e.kind as edge_kind
            FROM symbol_edges e
            JOIN symbols s ON e.source_id = s.id
            JOIN files f ON s.file_id = f.id
            WHERE e.target_id = ?
        ''', (symbol_id,))
        return cursor.fetchall()

    def get_callees(self, symbol_id: int) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT s.*, f.repo_id, f.path, e.kind as edge_kind
            FROM symbol_edges e
            JOIN symbols s ON e.target_id = s.id
            JOIN files f ON s.file_id = f.id
            WHERE e.source_id = ?
        ''', (symbol_id,))
        return cursor.fetchall()

    def get_hierarchy(self, symbol_id: int) -> List[sqlite3.Row]:
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT s.*, f.repo_id, f.path, e.kind as edge_kind
            FROM symbol_edges e
            JOIN symbols s ON e.target_id = s.id
            JOIN files f ON s.file_id = f.id
            WHERE e.source_id = ? AND e.kind IN ('INHERITS', 'IMPLEMENTS')
        ''', (symbol_id,))
        return cursor.fetchall()

    def add_chunk(self, chunk_id: str, file_id: int, content: str, 
                  source_kind: str, trust: str, 
                  line_start: Optional[int] = None, line_end: Optional[int] = None, 
                  sha: Optional[str] = None) -> int:
        """Добавляет чанк в базу и возвращает его sqlite internal rowid (нужно для vec0)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO chunks (id, file_id, content, line_start, line_end, source_kind, trust, sha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (chunk_id, file_id, content, line_start, line_end, source_kind, trust, sha))
        self._auto_commit()
        return cursor.lastrowid

    def add_embedding(self, chunk_rowid: int, vector: List[float]):
        """Добавляет векторное представление в виртуальную vec0 таблицу."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO chunks_vec(rowid, embedding)
            VALUES (?, ?)
        """, (chunk_rowid, sqlite_vec.serialize_float32(vector)))
        self.conn.commit()

    def add_embeddings_batch(self, records: List[tuple[int, List[float]]]):
        """Пакетно добавляет векторные представления для группы чанков (существенное ускорение)."""
        if not records:
            return
        cursor = self.conn.cursor()
        
        # Подготавливаем сериализованные данные для executemany
        batch_data = [
            (rowid, sqlite_vec.serialize_float32(vector))
            for rowid, vector in records
        ]
        
        cursor.executemany("""
            INSERT INTO chunks_vec(rowid, embedding)
            VALUES (?, ?)
        """, batch_data)
        self._auto_commit()
        
    def search_chunks_fts(self, query: str, repo_ids: Optional[List[str]] = None, limit: int = 10) -> List[sqlite3.Row]:
        """Полнотекстовый поиск по чанкам."""
        cursor = self.conn.cursor()
        
        # Санитизация запроса: удаляем кавычки и оборачиваем каждое слово,
        # чтобы FTS5 не падал на спецсимволах типа '.' или '-'
        safe_terms = [f'"{term}"' for term in query.replace('"', ' ').split() if term]
        safe_query = ' '.join(safe_terms)
        if not safe_query:
            return []

        sql = '''
            SELECT c.id, c.content, c.source_kind, c.trust, c.line_start, c.line_end, 
                   f.repo_id, f.path, bm25(chunks_fts) as rank
            FROM chunks_fts fts
            JOIN chunks c ON fts.rowid = c.rowid
            JOIN files f ON c.file_id = f.id
            WHERE chunks_fts MATCH ?
        '''
        params = [safe_query]
        
        if repo_ids:
            placeholders = ','.join('?' for _ in repo_ids)
            sql += f" AND f.repo_id IN ({placeholders})"
            params.extend(repo_ids)
            
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        return cursor.fetchall()

    def search_chunks_hybrid(self, query: str, query_vector: List[float], repo_ids: Optional[List[str]] = None, limit: int = 10) -> List[sqlite3.Row]:
        """Гибридный поиск RRF: объединяет FTS5 и KNN по векторам."""
        cursor = self.conn.cursor()
        
        fts_res = self.search_chunks_fts(query, repo_ids, limit=50)
        
        vec_sql = '''
            SELECT c.id, c.content, c.source_kind, c.trust, c.line_start, c.line_end, 
                   f.repo_id, f.path, vec_distance_L2(cv.embedding, ?) as distance
            FROM chunks_vec cv
            JOIN chunks c ON cv.rowid = c.rowid
            JOIN files f ON c.file_id = f.id
        '''
        params = [sqlite_vec.serialize_float32(query_vector)]
        if repo_ids:
            placeholders = ','.join('?' for _ in repo_ids)
            vec_sql += f" WHERE f.repo_id IN ({placeholders})"
            params.extend(repo_ids)
            
        vec_sql += " ORDER BY distance ASC LIMIT 50"
        
        cursor.execute(vec_sql, params)
        vec_res = cursor.fetchall()
        
        # Reciprocal Rank Fusion алгоритм
        K = 60
        scores = {}
        chunks_map = {}
        
        for i, row in enumerate(fts_res):
            chunk_id = row['id']
            chunks_map[chunk_id] = row
            scores[chunk_id] = 1.0 / (K + i + 1)
            
        for i, row in enumerate(vec_res):
            chunk_id = row['id']
            if chunk_id not in chunks_map:
                chunks_map[chunk_id] = row
            scores[chunk_id] = scores.get(chunk_id, 0) + (1.0 / (K + i + 1))
            
        sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [chunks_map[cid] for cid, _ in sorted_results[:limit]]
