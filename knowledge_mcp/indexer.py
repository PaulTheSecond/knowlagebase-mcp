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


# Размер суб-батча для обработки эмбеддингов порциями (оптимизировано для баланса RAM/скорость)
EMBED_SUB_BATCH_SIZE = 128
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
        # Переиспользуемые инстансы парсеров (Tree-sitter инициализируется один раз)
        self.code_parser = CodeParser()
        self.md_parser = MarkdownParser()

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
    # Слой 1: Чистые функции парсинга (без доступа к БД)
    # Безопасны для запуска в ThreadPoolExecutor.
    # Возвращают tuple: (chunks_raw, symbols_raw, edges_raw)
    #   chunks_raw:  [(body, source_kind, trust, line_start, line_end), ...]
    #   symbols_raw: [(name, qname, kind, lang, ls, le, sig, body_idx), ...]
    #                body_idx — индекс в chunks_raw, которому принадлежит символ
    #   edges_raw:   [(source_qname_or_ast_id, target_name, kind, is_ast_id), ...]
    # ──────────────────────────────────────────────────────────────────────

    def _parse_csharp(self, filepath: Path) -> tuple[list, list, list]:
        """Парсинг .cs файла из кэша Roslyn (чистая функция, нет обращений к БД)."""
        cache_hit = self.csharp_cache.get(filepath.resolve())
        if not cache_hit:
            return [], [], []

        chunks_raw = []
        symbols_raw = []
        edges_raw = []

        ast_id_to_chunk_idx: dict = {}
        for s in cache_hit['symbols']:
            idx = len(chunks_raw)
            chunks_raw.append((s['body'], 'code', 'verified', s['line_start'], s['line_end']))
            symbols_raw.append((
                s['name'], s['qualified_name'], s['kind'], s['language'],
                s['line_start'], s['line_end'], s['signature'], idx
            ))
            ast_id_to_chunk_idx[s['ast_node_id']] = idx

        for e in cache_hit['edges']:
            if e['source_ast_id'] in ast_id_to_chunk_idx:
                edges_raw.append((e['source_ast_id'], e['target_qualified_name'], e['kind'], True))

        return chunks_raw, symbols_raw, edges_raw

    def _parse_code(self, filepath: Path) -> tuple[list, list, list]:
        """Парсинг кодового файла через Tree-sitter (чистая функция, нет обращений к БД)."""
        lang = LANGUAGE_MAP[filepath.suffix.lower()]
        symbols = self.code_parser.parse_file(filepath, lang)
        edges_ts = self.code_parser.extract_edges(filepath, lang)

        chunks_raw = []
        symbols_raw = []
        edges_raw = []
        qname_to_idx: dict = {}

        for symbol in symbols:
            if not symbol.body.strip():
                continue
            idx = len(chunks_raw)
            chunks_raw.append((symbol.body, 'code', 'verified', symbol.line_start, symbol.line_end))
            symbols_raw.append((
                symbol.name, symbol.qualified_name, symbol.kind, symbol.language,
                symbol.line_start, symbol.line_end, symbol.signature, idx
            ))
            qname_to_idx[symbol.qualified_name] = idx
            if 'file_level' not in qname_to_idx:
                qname_to_idx['file_level'] = idx

        for edge in edges_ts:
            edges_raw.append((edge.source_name, edge.target_name, edge.kind, False))

        return chunks_raw, symbols_raw, edges_raw

    def _parse_markdown(self, filepath: Path) -> tuple[list, list, list]:
        """Парсинг Markdown (чистая функция, нет обращений к БД)."""
        sections = self.md_parser.parse_file(filepath)
        chunks_raw = [
            (sec.content, 'docs', 'hint', sec.line_start, sec.line_end)
            for sec in sections if sec.content.strip()
        ]
        return chunks_raw, [], []

    def _parse_fallback(self, filepath: Path) -> tuple[list, list, list]:
        """Fallback: читает файл целиком (чистая функция, нет обращений к БД)."""
        content = filepath.read_text(encoding='utf-8')
        if not content.strip():
            return [], [], []
        lines_count = len(content.splitlines())
        return [(content, 'code', 'verified', 1, lines_count)], [], []

    def _parse_file_pure(self, filepath: Path, rel_path: str) -> tuple[list, list, list] | None:
        """
        Диспетчер чистого парсинга файла.
        Возвращает (chunks_raw, symbols_raw, edges_raw) или None при ошибке.
        Безопасен для вызова из ThreadPoolExecutor.
        """
        ext = filepath.suffix.lower()
        try:
            if ext == '.cs':
                return self._parse_csharp(filepath)
            elif ext in LANGUAGE_MAP:
                return self._parse_code(filepath)
            elif ext in ('.md', '.mdx') or rel_path.startswith(('docs/', 'knowledge/')):
                return self._parse_markdown(filepath)
            else:
                return self._parse_fallback(filepath)
        except Exception as e:
            logger.warning(f"Failed parsing file {rel_path}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # Слой 2: Батчевая запись результатов парсинга в БД
    # ──────────────────────────────────────────────────────────────────────

    def _flush_parsed_file(
        self,
        file_id: int,
        rel_path: str,
        parsed: tuple[list, list, list],
        chunks_to_embed: list,
    ) -> ReindexStatus:
        """
        Записывает результаты парсинга в БД батчевыми операциями.
        Возвращает ReindexStatus.
        """
        chunks_raw, symbols_raw, edges_raw = parsed

        if not chunks_raw and not symbols_raw:
            return ReindexStatus.PROCESSED_NO_EMBEDDING

        # --- Батчевая вставка чанков ---
        chunk_records = [
            (str(uuid.uuid4()), file_id, body, ls, le, sk, tr, None)
            for body, sk, tr, ls, le in chunks_raw
        ]
        rowids = self.db.add_chunks_batch(chunk_records)
        # chunk_id (uuid string) по индексу
        chunk_ids = [r[0] for r in chunk_records]

        # Добавляем в очередь эмбеддингов
        embed_added = False
        if self.use_embeddings and self.embedder:
            for i, (body, sk, tr, ls, le) in enumerate(chunks_raw):
                chunks_to_embed.append((body, rowids[i], rel_path))
                embed_added = True

        # --- Батчевая вставка символов ---
        if symbols_raw:
            sym_records = [
                (file_id, name, qname, kind, lang, ls, le, sig, chunk_ids[body_idx])
                for name, qname, kind, lang, ls, le, sig, body_idx in symbols_raw
            ]
            sym_rowids = self.db.add_symbols_batch(sym_records)
            # Строим map: qualified_name / ast_node_id → symbol rowid
            qname_to_sym_id: dict = {}
            ast_to_sym_id: dict = {}
            for i, (name, qname, kind, lang, ls, le, sig, body_idx) in enumerate(symbols_raw):
                qname_to_sym_id[qname] = sym_rowids[i]
                if not qname_to_sym_id.get('file_level'):
                    qname_to_sym_id['file_level'] = sym_rowids[i]
            # Для .cs — ast_node_id хранится в edges_raw (is_ast_id=True)
            # Перестраиваем map через порядок symbols_raw (соответствует csharp_cache['symbols'])
            cache_hit = None
            if edges_raw and edges_raw[0][3]:  # is_ast_id
                cache_hit = self.csharp_cache.get(Path(rel_path))  # для поиска ast_node_id
                # ast_id_to_chunk_idx был построен в _parse_csharp по порядку,
                # т.е. i-й символ соответствует i-й записи csharp_cache['symbols']
                # Восстанавливаем через кэш напрямую
                resolved_key = None
                for rk, cv in self.csharp_cache.items():
                    # Ищем по relative path приближённо через суффикс
                    if str(rk).replace('\\', '/').endswith(rel_path):
                        resolved_key = rk
                        break
                if resolved_key:
                    cache_syms = self.csharp_cache[resolved_key]['symbols']
                    for i, s in enumerate(cache_syms):
                        if i < len(sym_rowids):
                            ast_to_sym_id[s['ast_node_id']] = sym_rowids[i]

            # --- Батчевая вставка рёбер ---
            edge_records = []
            unresolved_records = []
            for src_key, target_name, kind, is_ast_id in edges_raw:
                if is_ast_id:
                    source_id = ast_to_sym_id.get(src_key)
                else:
                    source_id = qname_to_sym_id.get(src_key)
                if not source_id:
                    continue

                # Ищем target: сначала локально, затем в БД
                target_id = qname_to_sym_id.get(target_name)
                if not target_id:
                    targets = self.db.find_symbols(target_name, limit=1)
                    target_id = targets[0]['id'] if targets else None

                if target_id:
                    edge_records.append((source_id, target_id, kind))
                else:
                    unresolved_records.append((source_id, target_name, kind))

            self.db.add_symbol_edges_batch(edge_records)
            self.db.add_unresolved_refs_batch(unresolved_records)

        return ReindexStatus.QUEUED_FOR_EMBEDDING if embed_added else ReindexStatus.PROCESSED_NO_EMBEDDING

    def _reindex_file(self, file_id: int, filepath: Path, rel_path: str, chunks_to_embed: list) -> ReindexStatus:
        """
        Совместимый метод (используется при последовательной обработке).
        Очищает старые данные файла, парсит и флашит результаты в БД.
        """
        self.db.clear_file_chunks(file_id)
        self.db.clear_file_symbols(file_id)

        parsed = self._parse_file_pure(filepath, rel_path)
        if parsed is None:
            return ReindexStatus.PARSE_ERROR
        return self._flush_parsed_file(file_id, rel_path, parsed, chunks_to_embed)


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
        Парсинг AST выполняется параллельно в ThreadPoolExecutor,
        запись в БД — только в главном потоке батчевыми операциями.
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

        def check_and_parse(item):
            """
            Параллельная задача: хэширует файл и, если он изменился,
            сразу парсит его AST. Не делает никаких обращений к БД.
            Возвращает: (rel_path, filepath, mtime, file_hash, status, old_id, parsed)
              parsed = (chunks_raw, symbols_raw, edges_raw) | None
            """
            filepath, rel_path = item
            try:
                mtime = filepath.stat().st_mtime
                if rel_path in known_files:
                    known = known_files[rel_path]
                    if known['mtime'] == mtime:
                        return (rel_path, filepath, mtime, None, 'unchanged', None, None)
                    file_hash = hash_file(filepath)
                    if known['hash'] == file_hash:
                        return (rel_path, filepath, mtime, file_hash, 'touch', None, None)
                    # Файл изменился — парсим сразу
                    parsed = self._parse_file_pure(filepath, rel_path)
                    return (rel_path, filepath, mtime, file_hash, 'updated', known['id'], parsed)
                else:
                    file_hash = hash_file(filepath)
                    # Новый файл — парсим сразу
                    parsed = self._parse_file_pure(filepath, rel_path)
                    return (rel_path, filepath, mtime, file_hash, 'added', None, parsed)
            except Exception as e:
                return (rel_path, filepath, None, None, 'error', str(e), None)

        sync_buffer = {rel_path: 'pending' for _, rel_path in files_to_check}
        chunks_to_embed = []

        # Оборачиваем весь цикл в одну транзакцию вместо тысяч отдельных commit()
        self.db.begin_transaction()
        try:
            # Хэширование + AST парсинг параллельно
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_HASH_WORKERS) as executor:
                total_files = len(files_to_check)
                logger.info(f"Found {total_files} files to check. Starting content hashing and AST parsing...")
                results = executor.map(check_and_parse, files_to_check)

                processed = 0
                log_step = max(1, total_files // 10) if total_files > 0 else 1

                for res in results:
                    processed += 1
                    if processed % log_step == 0 or processed == total_files:
                        logger.info(f"Scan progress: {processed}/{total_files} files ({int(processed/total_files*100)}%)")

                    rel_path, filepath, mtime, file_hash, status, old_id, parsed = res
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
                        self.db.clear_file_chunks(file_id)
                        self.db.clear_file_symbols(file_id)

                        if parsed is None:
                            # Ошибка парсинга
                            sync_buffer[rel_path] = 'skipped_parse_error'
                        else:
                            reindex_result = self._flush_parsed_file(file_id, rel_path, parsed, chunks_to_embed)
                            if reindex_result == ReindexStatus.QUEUED_FOR_EMBEDDING:
                                sync_buffer[rel_path] = 'waiting_for_embedding'
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

        # Очищаем тяжелый кэш семантики перед самой ресурсоемкой фазой векторизации
        self.csharp_cache = {}
        gc.collect()

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

