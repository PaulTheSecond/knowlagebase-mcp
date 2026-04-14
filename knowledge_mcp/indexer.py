import os
import hashlib
import logging
import uuid
import json
from pathlib import Path
from typing import Generator
import concurrent.futures

import pathspec

from .db import KnowledgeDB
from .embeddings import LocalEmbedder
from .code_parser import CodeParser, LANGUAGE_MAP
from .markdown_parser import MarkdownParser

logger = logging.getLogger(__name__)

def hash_file(filepath: Path, chunk_size: int = 8192) -> str:
    """Вычисляет SHA-256 хэш содержимого файла."""
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

# Размер суб-батча для обработки эмбеддингов порциями (экономия RAM)
EMBED_SUB_BATCH_SIZE = 64
# Максимальное количество потоков для параллельного хэширования
MAX_HASH_WORKERS = 8


class Indexer:
    def __init__(self, db: KnowledgeDB, use_embeddings: bool = True):
        self.db = db
        self.use_embeddings = use_embeddings
        self.embedder = LocalEmbedder() if use_embeddings else None

    def _get_ignore_spec(self, root_path: Path) -> pathspec.PathSpec:
        """Считывает .gitignore и добавляет системные исключения."""
        patterns = [
            # Системные
            '.git/', 'node_modules/', '.idea/', '.vs/', 'venv/', '__pycache__/', '*.log', '*.db',
            # AI-агенты и их конфиги
            '_bmad/', '.agents/', '.ai/', '.bmad-core/', '.claude/', '.gemini/',
            '.opencode/', '.roo/', '.github/', '.nuget/',
            'mcp.json', 'opencode.json', '.task-cache.json',
            'gitlab-task-collector.config.json',
            'AGENTS.md', 'Claude.md', 'gemini.md',
            # Конфиги сборки и IDE
            '.editorconfig', '.arscontexta', '.mailmap', '.roomodes',
            'Directory.Build.props', 'Directory.Packages.props',
            'nuget.config', 'packages.txt',
        ]
        
        gitignore_path = root_path / '.gitignore'
        if gitignore_path.exists():
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                patterns.extend(f.readlines())
                
        return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, patterns)

    def _walk_files(self, root_path: Path, spec: pathspec.PathSpec, allowed_top_level: list[str] = None) -> Generator[Path, None, None]:
        """Генератор, рекурсивно обходящий файлы с исключением путей по gitignore."""
        for dirpath, dirnames, filenames in os.walk(root_path):
            dir_rel_path = os.path.relpath(dirpath, root_path)
            if dir_rel_path == '.':
                dir_rel_path = ''
                
            # Если мы в корне и задан список top-level, оставляем только их
            if dir_rel_path == '' and allowed_top_level is not None:
                dirnames[:] = [d for d in dirnames if d in allowed_top_level]
                filenames = [f for f in filenames if f in allowed_top_level]
                
            # Исключаем директории, соответствующие паттернам, чтобы os.walk внутрь не заходил
            dirnames[:] = [d for d in dirnames if not spec.match_file((os.path.join(dir_rel_path, d) + '/').replace('\\', '/'))]
            
            for f in filenames:
                file_rel_path = os.path.join(dir_rel_path, f).replace('\\', '/')
                if not spec.match_file(file_rel_path):
                    yield Path(dirpath) / f

    def sync_repo(self, repo_id: str, repo_path: str | Path, allowed_top_level: list[str] = None):
        """
        Инкрементальная синхронизация репозитория (дельта-скан).
        Сравнивает mtime и hash, удаляет старые файлы, парсит новые/измененные.
        """
        repo_path = Path(repo_path).resolve()
        logger.info(f"Starting sync for repo '{repo_id}' at {repo_path}")
        
        if not repo_path.exists():
            logger.error(f"Path does not exist: {repo_path}")
            return

        spec = self._get_ignore_spec(repo_path)
        # Fix #3: Конвертируем sqlite3.Row в чистые dict для thread-safety
        known_files_raw = self.db.get_known_files(repo_id)
        known_files = {path: dict(row) for path, row in known_files_raw.items()}
        
        current_files = set()
        updated_count = 0
        added_count = 0
        unchanged_count = 0
        deleted_count = 0
        
        files_to_check = []
        for filepath in self._walk_files(repo_path, spec, allowed_top_level):
            if filepath.is_file():
                rel_path = filepath.relative_to(repo_path).as_posix()
                files_to_check.append((filepath, rel_path))
                current_files.add(rel_path)

        def check_file(item):
            """Чистая функция: только чтение с диска и хэширование (никаких обращений к БД)."""
            filepath, rel_path = item
            try:
                mtime = filepath.stat().st_mtime
                if rel_path in known_files:
                    known = known_files[rel_path]
                    if known['mtime'] == mtime:
                        return (rel_path, filepath, mtime, None, 'unchanged', None)
                    file_hash = hash_file(filepath)
                    if known['hash'] == file_hash:
                        return (rel_path, filepath, mtime, file_hash, 'touch', None)
                    return (rel_path, filepath, mtime, file_hash, 'updated', known['id'])
                else:
                    file_hash = hash_file(filepath)
                    return (rel_path, filepath, mtime, file_hash, 'added', None)
            except Exception as e:
                return (rel_path, filepath, None, None, 'error', str(e))

        sync_buffer = {rel_path: 'pending' for _, rel_path in files_to_check}
        chunks_to_embed = []  # Список кортежей (content, chunk_rowid, rel_path)
        
        # Fix #5: Оборачиваем весь цикл в одну транзакцию вместо тысяч отдельных commit()
        self.db.begin_transaction()
        try:
            # Fix #4: Ограничиваем количество потоков для предотвращения 'Too many open files'
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_HASH_WORKERS) as executor:
                # Исполняем IO-интенсивную задачу хэширования параллельно
                results = executor.map(check_file, files_to_check)
                
                for res in results:
                    rel_path, filepath, mtime, file_hash, status, old_id = res
                    if status == 'error':
                        logger.error(f"Error processing {rel_path}: {old_id}")
                        sync_buffer[rel_path] = 'error'
                    elif status == 'unchanged':
                        unchanged_count += 1
                        sync_buffer[rel_path] = 'completed'
                    elif status == 'touch':
                        self.db.upsert_file(repo_id, rel_path, mtime, file_hash)
                        unchanged_count += 1
                        sync_buffer[rel_path] = 'completed'
                    elif status in ('updated', 'added'):
                        if status == 'updated':
                            updated_count += 1
                        else:
                            added_count += 1
                        file_id = self.db.upsert_file(repo_id, rel_path, mtime, file_hash)
                        
                        # Fix #7: _reindex_file возвращает 3 состояния (True/False/None)
                        reindex_result = self._reindex_file(file_id, filepath, rel_path, chunks_to_embed)
                        if reindex_result is True:
                            sync_buffer[rel_path] = 'waiting_for_embedding'
                        elif reindex_result is None:
                            sync_buffer[rel_path] = 'skipped_parse_error'
                        else:
                            sync_buffer[rel_path] = 'completed'

            # Удаляем из БД ушедшие с диска файлы
            for rel_path, known in known_files.items():
                if rel_path not in current_files:
                    self.db.delete_file(known['id'])
                    deleted_count += 1

            self.db.commit_transaction()
        except Exception as e:
            self.db.rollback_transaction()
            logger.error(f"Transaction failed during sync of '{repo_id}': {e}")
            raise

        # Fix #1: Батч-векторизация порциями (sub-batches) для экономии RAM
        if self.use_embeddings and self.embedder and chunks_to_embed:
            total_chunks = len(chunks_to_embed)
            logger.info(f"Batch computing local embeddings for {total_chunks} chunks (sub-batches of {EMBED_SUB_BATCH_SIZE})...")
            
            for batch_start in range(0, total_chunks, EMBED_SUB_BATCH_SIZE):
                batch_slice = chunks_to_embed[batch_start:batch_start + EMBED_SUB_BATCH_SIZE]
                texts = [item[0] for item in batch_slice]
                rowids = [item[1] for item in batch_slice]
                rp_list = [item[2] for item in batch_slice]
                
                vectors = self.embedder.embed_batch(texts, batch_size=32)
                
                records = []
                completed_rp = set()
                for i, vector in enumerate(vectors):
                    if vector:
                        records.append((rowids[i], vector))
                        completed_rp.add(rp_list[i])
                        
                if records:
                    self.db.add_embeddings_batch(records)
                    
                for rp in rp_list:
                    if rp in completed_rp:
                        sync_buffer[rp] = 'completed'
                    else:
                        sync_buffer[rp] = 'error_embedding'
                
                # Освобождаем ссылки на тексты текущей порции
                del texts, vectors, records
            
            # Освобождаем весь буфер после завершения
            chunks_to_embed.clear()

        # Финальная верификация
        # Fix #6: Санитизируем repo_id для безопасного имени файла
        safe_repo_id = repo_id.replace('/', '_').replace('\\', '_').replace('..', '_')
        pending_or_error = {k: v for k, v in sync_buffer.items() if v != 'completed'}
        report_path = self.db.db_path.parent / f"sync_report_{safe_repo_id}.json"
        
        if not pending_or_error:
            logger.info(f"Verification successful: 100% of {len(sync_buffer)} files processed correctly.")
            if report_path.exists():
                report_path.unlink()
        else:
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "total": len(sync_buffer),
                    "successful": len(sync_buffer) - len(pending_or_error),
                    "issues": pending_or_error
                }, f, indent=2)
            logger.error(f"Verification failed: {len(pending_or_error)} items failed. Report dumped to {report_path}")

        # Разрешаем повисшие коннекшены (cross-repo/forward references)
        try:
            resolved_count = self.db.resolve_pending_references()
            if resolved_count > 0:
                logger.info(f"Resolved {resolved_count} pending cross-repo references.")
        except Exception as e:
            logger.error(f"Error resolving pending references: {e}")

        logger.info(f"Sync complete for '{repo_id}': +{added_count} ~{updated_count} -{deleted_count} (={unchanged_count} unchanged)")

    def _reindex_file(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list):
        """Очищает старые чанки и символы файла и нарезает/парсит новые.
        
        Returns:
            True  — чанк добавлен в очередь на векторизацию
            False — файл обработан успешно, но эмбеддинги не нужны (пустой файл или выключены)
            None  — ошибка парсинга (бинарник, проблема кодировки)
        """
        self.db.clear_file_chunks(file_id)
        self.db.clear_file_symbols(file_id)
        
        ext = filepath.suffix.lower()

        try:
            added_embedding = False
            
            if ext in LANGUAGE_MAP:
                # Symbol-based chunking для кода
                parser = CodeParser()
                symbols = parser.parse_file(filepath, LANGUAGE_MAP[ext])
                # СЛОВАРЬ для маппинга source_name -> symbol_id
                local_symbols_map = {}
                
                for symbol in symbols:
                    if not symbol.body.strip():
                        continue
                        
                    chunk_id = str(uuid.uuid4())
                    chunk_rowid = self.db.add_chunk(
                        chunk_id=chunk_id, file_id=file_id, content=symbol.body, 
                        source_kind='code', trust='verified',
                        line_start=symbol.line_start, line_end=symbol.line_end
                    )
                    symbol_id = self.db.add_symbol(
                        file_id, symbol.name, symbol.qualified_name,
                        symbol.kind, symbol.language, 
                        symbol.line_start, symbol.line_end, 
                        symbol.signature, chunk_id
                    )
                    local_symbols_map[symbol.qualified_name] = symbol_id
                    if not local_symbols_map.get("file_level"):
                        local_symbols_map["file_level"] = symbol_id
                    
                    if self.use_embeddings and self.embedder:
                        chunks_to_embed.append((symbol.body, chunk_rowid, rel_path))
                        added_embedding = True
                
                # Связи
                edges = parser.extract_edges(filepath, LANGUAGE_MAP[ext])
                for edge in edges:
                    source_id = local_symbols_map.get(edge.source_name)
                    if not source_id:
                        continue # Cannot link edge if source symbol wasn't saved
                        
                    # 1. Пытаемся найти локально (в этом же файле)
                    target_id = local_symbols_map.get(edge.target_name)
                    
                    # 2. Пытаемся найти в базе
                    if not target_id:
                        targets = self.db.find_symbols(edge.target_name, limit=1)
                        if targets:
                            target_id = targets[0]['id']
                            
                    # 3. Сохраняем edge или unresolved ref
                    if target_id:
                        self.db.add_symbol_edge(source_id, target_id, edge.kind)
                    else:
                        self.db.add_unresolved_ref(source_id, edge.target_name, edge.kind)

            elif ext in ('.md', '.mdx') or rel_path.startswith(('docs/', 'knowledge/')):
                # Section-based chunking для документации
                md_parser = MarkdownParser()
                sections = md_parser.parse_file(filepath)
                for section in sections:
                    if not section.content.strip():
                        continue
                        
                    chunk_id = str(uuid.uuid4())
                    chunk_rowid = self.db.add_chunk(
                        chunk_id=chunk_id, file_id=file_id, content=section.content,
                        source_kind='docs', trust='hint',
                        line_start=section.line_start, line_end=section.line_end
                    )
                    if self.use_embeddings and self.embedder:
                        chunks_to_embed.append((section.content, chunk_rowid, rel_path))
                        added_embedding = True
            
            else:
                # Fallback: файл целиком
                content = filepath.read_text(encoding='utf-8')
                if not content.strip():
                    return False
                    
                chunk_id = str(uuid.uuid4())
                lines_count = len(content.splitlines())
                
                source_kind = 'code' # Фаллбек для прочих текстовых файлов (txt, json, yaml etc)
                trust = 'verified'
                
                chunk_rowid = self.db.add_chunk(
                    chunk_id=chunk_id, file_id=file_id, content=content,
                    source_kind=source_kind, trust=trust,
                    line_start=1, line_end=lines_count
                )
                
                if self.use_embeddings and self.embedder:
                    chunks_to_embed.append((content, chunk_rowid, rel_path))
                    added_embedding = True

            return added_embedding
        except Exception as e:
            # Скипаем в случае проблем кодировок (бинарники)
            logger.warning(f"Failed parsing file {rel_path}: {e}")
            return None
