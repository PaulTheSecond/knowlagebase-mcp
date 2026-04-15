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

# Размер суб-батча для обработки эмбеддингов порциями (строгая экономия RAM для 2GB WSL)
EMBED_SUB_BATCH_SIZE = 16
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
        
        # Запуск Roslyn для .NET проектов (Semantic Precision)
        import subprocess, json
        self.csharp_cache = {}
        
        # Оптимизированный поиск .sln файлов с уважением к .gitignore
        csharp_projects = []
        for filepath in self._walk_files(repo_path, spec):
            if filepath.suffix == '.sln':
                csharp_projects.append(filepath)
                
        if not csharp_projects:
            for filepath in self._walk_files(repo_path, spec):
                if filepath.suffix == '.csproj':
                    csharp_projects.append(filepath)
            
        parser_dll = Path(__file__).parent.parent / "RoslynParser" / "bin" / "Release" / "net8.0" / "RoslynParser.dll"
        for proj in csharp_projects:
            if "RoslynParser" in str(proj): continue
            
            logger.info(f"Running Roslyn semantic analysis on {proj}...")
            cmd = ["dotnet", str(parser_dll), str(proj)]
            if not parser_dll.exists():
                cmd = ["dotnet", "run", "-c", "Release", "--project", str(Path(__file__).parent.parent / "RoslynParser" / "RoslynParser.csproj"), "--", str(proj)]
                
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                try:
                    data = json.loads(res.stdout)
                    
                    # Кэшируем resolve во избежание миллионов обращений к диску по Docker Volume
                    path_resolved_cache = {}
                    def get_resolved(path_str):
                        if path_str not in path_resolved_cache:
                            path_resolved_cache[path_str] = Path(path_str).resolve()
                        return path_resolved_cache[path_str]
                    
                    for s in data.get('symbols', []):
                        fp = get_resolved(s['file_path'])
                        if fp not in self.csharp_cache:
                            self.csharp_cache[fp] = {'symbols': [], 'edges': []}
                        self.csharp_cache[fp]['symbols'].append(s)
                        
                    id_to_file = {}
                    for s in data.get('symbols', []):
                        id_to_file[s['ast_node_id']] = get_resolved(s['file_path'])
                        
                    for e in data.get('edges', []):
                        if e['source_ast_id'] in id_to_file:
                            fp = id_to_file[e['source_ast_id']]
                            self.csharp_cache[fp]['edges'].append(e)
                            
                except Exception as ex:
                    logger.error(f"Failed to parse Roslyn output for {proj}: {ex}")
            else:
                logger.error(f"Roslyn analysis failed for {proj}: {res.stderr}")

        # Fix #3: Конвертируем sqlite3.Row в чистые dict для thread-safety
        known_files_raw = self.db.get_known_files(repo_id)
        known_files = {path: dict(row) for path, row in known_files_raw.items()}
        
        current_files = set()
        updated_count = 0
        added_count = 0
        unchanged_count = 0
        deleted_count = 0
        
        logger.info("Walking directory tree to collect files...")
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
                total_files = len(files_to_check)
                logger.info(f"Found {total_files} files to check. Starting content hashing and AST parsing...")
                results = executor.map(check_file, files_to_check)
                
                processed = 0
                log_step = max(1, total_files // 10) if total_files > 0 else 1
                
                for res in results:
                    processed += 1
                    if processed % log_step == 0 or processed == total_files:
                        logger.info(f"Scan progress: {processed}/{total_files} files ({int(processed/total_files*100)}%)")
                        
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

        # Восстанавливаем эмбеддинги для чанков, которые есть в БД, но выпали из-за OOM (мягкое восстановление)
        if self.use_embeddings:
            missing = self.db.get_chunks_without_embeddings(repo_id)
            if missing:
                logger.info(f"Recovery: Found {len(missing)} text chunks in DB missing vector embeddings. Queuing them now.")
                chunks_to_embed.extend(missing)

        # Fix #1: Батч-векторизация порциями (sub-batches) для экономии RAM
        if self.use_embeddings and self.embedder and chunks_to_embed:
            total_chunks = len(chunks_to_embed)
            logger.info(f"Batch computing local embeddings for {total_chunks} chunks (sub-batches of {EMBED_SUB_BATCH_SIZE})...")
            
            processed_chunks = 0
            for batch_start in range(0, total_chunks, EMBED_SUB_BATCH_SIZE):
                batch_slice = chunks_to_embed[batch_start:batch_start + EMBED_SUB_BATCH_SIZE]
                texts = [item[0] for item in batch_slice]
                rowids = [item[1] for item in batch_slice]
                rp_list = [item[2] for item in batch_slice]
                
                vectors = self.embedder.embed_batch(texts, batch_size=8)
                
                processed_chunks += len(batch_slice)
                logger.info(f"Embedding progress: {processed_chunks}/{total_chunks} chunks ({int(processed_chunks/total_chunks*100)}%)")
                
                records = []
                completed_rp = set()
                for i, vector in enumerate(vectors):
                    if vector:
                        records.append((rowids[i], vector))
                        completed_rp.add(rp_list[i])
                        
                if records:
                    self.db.add_embeddings_batch(records)
                
                import gc
                gc.collect()
                    
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
            
            if ext == '.cs':
                # Используем кэш от RoslynParser, чтобы избежать синтаксического парсинга Tree-sitter
                cache_hit = getattr(self, 'csharp_cache', {}).get(filepath.resolve())
                if cache_hit:
                    local_symbols_map = {}
                    
                    for s in cache_hit['symbols']:
                        chunk_id = str(uuid.uuid4())
                        chunk_rowid = self.db.add_chunk(
                            chunk_id=chunk_id, file_id=file_id, content=s['body'], 
                            source_kind='code', trust='verified',
                            line_start=s['line_start'], line_end=s['line_end']
                        )
                        symbol_id = self.db.add_symbol(
                            file_id, s['name'], s['qualified_name'],
                            s['kind'], s['language'], 
                            s['line_start'], s['line_end'], 
                            s['signature'], chunk_id
                        )
                        local_symbols_map[s['ast_node_id']] = symbol_id
                        
                        if self.use_embeddings and self.embedder:
                            chunks_to_embed.append((s['body'], chunk_rowid, rel_path))
                            added_embedding = True
                            
                    for e in cache_hit['edges']:
                        source_id = local_symbols_map.get(e['source_ast_id'])
                        if not source_id: continue
                        
                        target_name = e['target_qualified_name']
                        targets = self.db.find_symbols(target_name, limit=1)
                        if targets:
                            self.db.add_symbol_edge(source_id, targets[0]['id'], e['kind'])
                        else:
                            self.db.add_unresolved_ref(source_id, target_name, e['kind'])
                return added_embedding

            elif ext in LANGUAGE_MAP and ext != '.cs':
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
