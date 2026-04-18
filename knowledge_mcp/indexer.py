import os
import gc
import hashlib
import json
import logging
import subprocess
import uuid
from enum import Enum
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


class ReindexStatus(Enum):
    """Результат попытки реиндексации одного файла."""
    QUEUED_FOR_EMBEDDING = "queued"       # Чанки добавлены в очередь на векторизацию
    PROCESSED_NO_EMBEDDING = "processed"  # Файл обработан успешно, эмбеддинги не нужны
    PARSE_ERROR = "error"                 # Файл пропущен из-за ошибки (бинарник, кодировка)


class Indexer:
    def __init__(self, db: KnowledgeDB, use_embeddings: bool = True):
        self.db = db
        self.use_embeddings = use_embeddings
        self.embedder = LocalEmbedder() if use_embeddings else None
        # Кэш результатов Roslyn: resolved Path -> {'symbols': [...], 'edges': [...]}
        self.csharp_cache: dict = {}

    # ──────────────────────────────────────────────────────────────────────
    # Вспомогательные методы: обход файлов
    # ──────────────────────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────────────────────
    # Roslyn: семантический анализ .NET проектов
    # ──────────────────────────────────────────────────────────────────────

    def _run_roslyn_analysis(self, repo_path: Path, spec: pathspec.PathSpec):
        """
        Запускает RoslynParser для .NET проектов и заполняет self.csharp_cache.
        Кэш содержит символы и рёбра для каждого .cs файла.
        """
        self.csharp_cache = {}

        # Оптимизированный поиск .sln файлов с уважением к .gitignore
        csharp_projects = [f for f in self._walk_files(repo_path, spec) if f.suffix == '.sln']
        if not csharp_projects:
            csharp_projects = [f for f in self._walk_files(repo_path, spec) if f.suffix == '.csproj']

        if not csharp_projects:
            return

        # Кэш path.resolve() для избежания миллионов обращений к диску по Docker Volume.
        # ВАЖНО: объявляем ВНЕ цикла по проектам, чтобы кэш не сбрасывался на каждой итерации.
        path_resolved_cache: dict[str, Path] = {}

        def get_resolved(path_str: str) -> Path:
            if path_str not in path_resolved_cache:
                path_resolved_cache[path_str] = Path(path_str).resolve()
            return path_resolved_cache[path_str]

        parser_dll = Path(__file__).parent.parent / "RoslynParser" / "bin" / "Release" / "net8.0" / "RoslynParser.dll"

        for proj in csharp_projects:
            if "RoslynParser" in str(proj):
                continue

            logger.info(f"Running Roslyn semantic analysis on {proj}...")

            if parser_dll.exists():
                cmd = ["dotnet", str(parser_dll), str(proj)]
            else:
                cmd = [
                    "dotnet", "run", "-c", "Release",
                    "--project", str(Path(__file__).parent.parent / "RoslynParser" / "RoslynParser.csproj"),
                    "--", str(proj)
                ]

            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                logger.error(f"Roslyn analysis failed for {proj}: {res.stderr}")
                continue

            try:
                data = json.loads(res.stdout)

                for s in data.get('symbols', []):
                    fp = get_resolved(s['file_path'])
                    if fp not in self.csharp_cache:
                        self.csharp_cache[fp] = {'symbols': [], 'edges': []}
                    self.csharp_cache[fp]['symbols'].append(s)

                id_to_file = {
                    s['ast_node_id']: get_resolved(s['file_path'])
                    for s in data.get('symbols', [])
                }

                for e in data.get('edges', []):
                    if e['source_ast_id'] in id_to_file:
                        fp = id_to_file[e['source_ast_id']]
                        self.csharp_cache[fp]['edges'].append(e)

            except Exception as ex:
                logger.error(f"Failed to parse Roslyn output for {proj}: {ex}")

    # ──────────────────────────────────────────────────────────────────────
    # Общий helper: создание чанка + постановка в очередь эмбеддингов
    # ──────────────────────────────────────────────────────────────────────

    def _add_chunk_with_embedding(
        self,
        file_id: int,
        rel_path: str,
        content: str,
        source_kind: str,
        trust: str,
        line_start: int,
        line_end: int,
        chunks_to_embed: list,
    ) -> tuple[str, int]:
        """
        Создаёт чанк в БД и при необходимости добавляет его в очередь на векторизацию.
        Возвращает (chunk_id, chunk_rowid).
        Централизует дублирующийся паттерн uuid+add_chunk+append из 4 ветвей _reindex_file.
        """
        chunk_id = str(uuid.uuid4())
        chunk_rowid = self.db.add_chunk(
            chunk_id=chunk_id, file_id=file_id, content=content,
            source_kind=source_kind, trust=trust,
            line_start=line_start, line_end=line_end,
        )
        if self.use_embeddings and self.embedder:
            chunks_to_embed.append((content, chunk_rowid, rel_path))
        return chunk_id, chunk_rowid

    # ──────────────────────────────────────────────────────────────────────
    # Стратегии реиндексации по типу файла
    # ──────────────────────────────────────────────────────────────────────

    def _reindex_csharp(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """Реиндексация .cs файла через кэш RoslynParser (Semantic Precision)."""
        cache_hit = self.csharp_cache.get(filepath.resolve())
        if not cache_hit:
            return ReindexStatus.PROCESSED_NO_EMBEDDING

        local_symbols_map = {}
        embed_count_before = len(chunks_to_embed)

        for s in cache_hit['symbols']:
            chunk_id, _ = self._add_chunk_with_embedding(
                file_id, rel_path, s['body'],
                'code', 'verified',
                s['line_start'], s['line_end'],
                chunks_to_embed,
            )
            symbol_id = self.db.add_symbol(
                file_id, s['name'], s['qualified_name'],
                s['kind'], s['language'],
                s['line_start'], s['line_end'],
                s['signature'], chunk_id,
            )
            local_symbols_map[s['ast_node_id']] = symbol_id

        for e in cache_hit['edges']:
            source_id = local_symbols_map.get(e['source_ast_id'])
            if not source_id:
                continue

            target_name = e['target_qualified_name']
            targets = self.db.find_symbols(target_name, limit=1)
            if targets:
                self.db.add_symbol_edge(source_id, targets[0]['id'], e['kind'])
            else:
                self.db.add_unresolved_ref(source_id, target_name, e['kind'])

        added_any = len(chunks_to_embed) > embed_count_before
        return ReindexStatus.QUEUED_FOR_EMBEDDING if added_any else ReindexStatus.PROCESSED_NO_EMBEDDING

    def _reindex_code(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """Реиндексация файла кода через Tree-sitter (symbol-based chunking)."""
        parser = CodeParser()
        lang = LANGUAGE_MAP[filepath.suffix.lower()]
        symbols = parser.parse_file(filepath, lang)
        local_symbols_map = {}
        embed_count_before = len(chunks_to_embed)

        for symbol in symbols:
            if not symbol.body.strip():
                continue

            chunk_id, _ = self._add_chunk_with_embedding(
                file_id, rel_path, symbol.body,
                'code', 'verified',
                symbol.line_start, symbol.line_end,
                chunks_to_embed,
            )
            symbol_id = self.db.add_symbol(
                file_id, symbol.name, symbol.qualified_name,
                symbol.kind, symbol.language,
                symbol.line_start, symbol.line_end,
                symbol.signature, chunk_id,
            )
            local_symbols_map[symbol.qualified_name] = symbol_id
            if not local_symbols_map.get("file_level"):
                local_symbols_map["file_level"] = symbol_id

        # Связи: сначала ищем локально, затем в БД, иначе — unresolved ref
        edges = parser.extract_edges(filepath, lang)
        for edge in edges:
            source_id = local_symbols_map.get(edge.source_name)
            if not source_id:
                continue

            target_id = local_symbols_map.get(edge.target_name)
            if not target_id:
                targets = self.db.find_symbols(edge.target_name, limit=1)
                if targets:
                    target_id = targets[0]['id']

            if target_id:
                self.db.add_symbol_edge(source_id, target_id, edge.kind)
            else:
                self.db.add_unresolved_ref(source_id, edge.target_name, edge.kind)

        added_any = len(chunks_to_embed) > embed_count_before
        return ReindexStatus.QUEUED_FOR_EMBEDDING if added_any else ReindexStatus.PROCESSED_NO_EMBEDDING

    def _reindex_markdown(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """Реиндексация Markdown/MDX документации (section-based chunking)."""
        md_parser = MarkdownParser()
        sections = md_parser.parse_file(filepath)
        embed_count_before = len(chunks_to_embed)

        for section in sections:
            if not section.content.strip():
                continue
            self._add_chunk_with_embedding(
                file_id, rel_path, section.content,
                'docs', 'hint',
                section.line_start, section.line_end,
                chunks_to_embed,
            )

        added_any = len(chunks_to_embed) > embed_count_before
        return ReindexStatus.QUEUED_FOR_EMBEDDING if added_any else ReindexStatus.PROCESSED_NO_EMBEDDING

    def _reindex_fallback(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """Fallback: реиндексация любого текстового файла целиком (txt, json, yaml и т.п.)."""
        content = filepath.read_text(encoding='utf-8')
        if not content.strip():
            return ReindexStatus.PROCESSED_NO_EMBEDDING

        lines_count = len(content.splitlines())
        embed_count_before = len(chunks_to_embed)
        self._add_chunk_with_embedding(
            file_id, rel_path, content,
            'code', 'verified',
            1, lines_count,
            chunks_to_embed,
        )

        added_any = len(chunks_to_embed) > embed_count_before
        return ReindexStatus.QUEUED_FOR_EMBEDDING if added_any else ReindexStatus.PROCESSED_NO_EMBEDDING

    def _reindex_file(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """
        Очищает старые чанки и символы файла, затем делегирует реиндексацию
        методу-стратегии в зависимости от типа файла.

        Returns:
            ReindexStatus.QUEUED_FOR_EMBEDDING  — чанки добавлены в очередь на векторизацию
            ReindexStatus.PROCESSED_NO_EMBEDDING — успешно обработан, эмбеддинги не нужны
            ReindexStatus.PARSE_ERROR           — файл пропущен (бинарник, проблема кодировки)
        """
        self.db.clear_file_chunks(file_id)
        self.db.clear_file_symbols(file_id)

        ext = filepath.suffix.lower()
        try:
            if ext == '.cs':
                return self._reindex_csharp(file_id, filepath, rel_path, chunks_to_embed)
            elif ext in LANGUAGE_MAP:
                return self._reindex_code(file_id, filepath, rel_path, chunks_to_embed)
            elif ext in ('.md', '.mdx') or rel_path.startswith(('docs/', 'knowledge/')):
                return self._reindex_markdown(file_id, filepath, rel_path, chunks_to_embed)
            else:
                return self._reindex_fallback(file_id, filepath, rel_path, chunks_to_embed)
        except Exception as e:
            # Скипаем в случае проблем кодировок (бинарники)
            logger.warning(f"Failed parsing file {rel_path}: {e}")
            return ReindexStatus.PARSE_ERROR

    # ──────────────────────────────────────────────────────────────────────
    # Эмбеддинги и отчётность
    # ──────────────────────────────────────────────────────────────────────

    def _embed_pending_chunks(self, repo_id: str, chunks_to_embed: list, sync_buffer: dict):
        """
        Векторизует накопленные чанки порциями (sub-batch) для экономии RAM.
        Дополнительно подхватывает чанки без эмбеддингов из предыдущих запусков (recovery после OOM).
        Обновляет sync_buffer по завершении каждой порции.
        """
        # Recovery: чанки, которые есть в БД, но потеряли вектор из-за OOM
        missing = self.db.get_chunks_without_embeddings(repo_id)
        if missing:
            logger.info(f"Recovery: Found {len(missing)} text chunks in DB missing vector embeddings. Queuing them now.")
            chunks_to_embed.extend(missing)

        if not chunks_to_embed:
            return

        # Удаляем дубликаты по rowid (могут возникнуть при наложении новых чанков и recovery)
        unique_list = []
        seen_rowids = set()
        for item in chunks_to_embed:
            rowid = item[1]
            if rowid not in seen_rowids:
                unique_list.append(item)
                seen_rowids.add(rowid)
        
        chunks_to_embed = unique_list
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

            gc.collect()

            # Обновляем sync_buffer для файлов текущей порции
            for rp in rp_list:
                if rp in sync_buffer:
                    sync_buffer[rp] = 'completed' if rp in completed_rp else 'error_embedding'

            # Освобождаем ссылки на тексты текущей порции
            del texts, vectors, records

        # Освобождаем весь буфер после завершения
        chunks_to_embed.clear()

    def _write_sync_report(self, repo_id: str, sync_buffer: dict):
        """Записывает JSON-отчёт о проблемных файлах или удаляет старый при 100% успехе."""
        # Санитизируем repo_id для безопасного имени файла
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
                    "issues": pending_or_error,
                }, f, indent=2)
            logger.error(f"Verification failed: {len(pending_or_error)} items failed. Report dumped to {report_path}")

    # ──────────────────────────────────────────────────────────────────────
    # Публичный API
    # ──────────────────────────────────────────────────────────────────────

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

        # Семантический анализ .NET проектов через Roslyn
        self._run_roslyn_analysis(repo_path, spec)

        # Конвертируем sqlite3.Row в чистые dict для thread-safety
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
        chunks_to_embed = []

        # Оборачиваем весь цикл в одну транзакцию вместо тысяч отдельных commit()
        self.db.begin_transaction()
        try:
            # Ограничиваем количество потоков для предотвращения 'Too many open files'
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_HASH_WORKERS) as executor:
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

                        reindex_result = self._reindex_file(file_id, filepath, rel_path, chunks_to_embed)
                        if reindex_result == ReindexStatus.QUEUED_FOR_EMBEDDING:
                            sync_buffer[rel_path] = 'waiting_for_embedding'
                        elif reindex_result == ReindexStatus.PARSE_ERROR:
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

        # Батч-векторизация + recovery потерянных эмбеддингов
        if self.use_embeddings and self.embedder:
            self._embed_pending_chunks(repo_id, chunks_to_embed, sync_buffer)

        # Финальная верификация: отчёт о проблемных файлах
        self._write_sync_report(repo_id, sync_buffer)

        # Разрешение повисших cross-repo ссылок
        try:
            resolved_count = self.db.resolve_pending_references()
            if resolved_count > 0:
                logger.info(f"Resolved {resolved_count} pending cross-repo references.")
        except Exception as e:
            logger.error(f"Error resolving pending references: {e}")

        logger.info(f"Sync complete for '{repo_id}': +{added_count} ~{updated_count} -{deleted_count} (={unchanged_count} unchanged)")
