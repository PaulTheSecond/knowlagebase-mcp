import os
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Generator

import pathspec

from .db import KnowledgeDB
from .embeddings import LocalEmbedder

logger = logging.getLogger(__name__)

def hash_file(filepath: Path, chunk_size: int = 8192) -> str:
    """Вычисляет SHA-256 хэш содержимого файла."""
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()

class Indexer:
    def __init__(self, db: KnowledgeDB, use_embeddings: bool = True):
        self.db = db
        self.use_embeddings = use_embeddings
        self.embedder = LocalEmbedder() if use_embeddings else None

    def _get_ignore_spec(self, root_path: Path) -> pathspec.PathSpec:
        """Считывает .gitignore и добавляет системные исключения."""
        patterns = ['.git/', 'node_modules/', '.idea/', '.vs/', 'venv/', '__pycache__/', '*.log', '*.db']
        
        gitignore_path = root_path / '.gitignore'
        if gitignore_path.exists():
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                patterns.extend(f.readlines())
                
        return pathspec.PathSpec.from_lines(pathspec.patterns.GitWildMatchPattern, patterns)

    def _walk_files(self, root_path: Path, spec: pathspec.PathSpec) -> Generator[Path, None, None]:
        """Генератор, рекурсивно обходящий файлы с исключением путей по gitignore."""
        for dirpath, dirnames, filenames in os.walk(root_path):
            dir_rel_path = os.path.relpath(dirpath, root_path)
            if dir_rel_path == '.':
                dir_rel_path = ''
                
            # Исключаем директории, соответствующие паттернам, чтобы os.walk внутрь не заходил
            dirnames[:] = [d for d in dirnames if not spec.match_file((os.path.join(dir_rel_path, d) + '/').replace('\\', '/'))]
            
            for f in filenames:
                file_rel_path = os.path.join(dir_rel_path, f).replace('\\', '/')
                if not spec.match_file(file_rel_path):
                    yield Path(dirpath) / f

    def sync_repo(self, repo_id: str, repo_path: str | Path):
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
        known_files = self.db.get_known_files(repo_id)
        
        current_files = set()
        updated_count = 0
        added_count = 0
        unchanged_count = 0
        deleted_count = 0
        
        for filepath in self._walk_files(repo_path, spec):
            if not filepath.is_file():
                continue
                
            rel_path = filepath.relative_to(repo_path).as_posix()
            current_files.add(rel_path)
            
            try:
                mtime = filepath.stat().st_mtime
                
                if rel_path in known_files:
                    known = known_files[rel_path]
                    
                    if known['mtime'] == mtime:
                        unchanged_count += 1
                        continue  # Файл не менялся
                        
                    file_hash = hash_file(filepath)
                    if known['hash'] == file_hash:
                        # Хэш не изменился (например, touch), просто обновим mtime в БД
                        self.db.upsert_file(repo_id, rel_path, mtime, file_hash)
                        unchanged_count += 1
                        continue
                        
                    # Контент файла изменился
                    updated_count += 1
                    file_id = self.db.upsert_file(repo_id, rel_path, mtime, file_hash)
                    self._reindex_file(file_id, filepath, rel_path)
                else:
                    # Новый файл
                    added_count += 1
                    file_hash = hash_file(filepath)
                    file_id = self.db.upsert_file(repo_id, rel_path, mtime, file_hash)
                    self._reindex_file(file_id, filepath, rel_path)
                    
            except Exception as e:
                logger.error(f"Error processing {rel_path}: {e}")

        # Удаляем из БД ушедшие с диска файлы
        for rel_path, known in known_files.items():
            if rel_path not in current_files:
                self.db.delete_file(known['id'])
                deleted_count += 1

        logger.info(f"Sync complete for '{repo_id}': +{added_count} ~{updated_count} -{deleted_count} (={unchanged_count} unchanged)")

    def _reindex_file(self, file_id: int, filepath: Path, rel_path: str):
        """Очищает старые чанки файла и нарезает новые."""
        self.db.clear_file_chunks(file_id)
        
        # Базовый парсер (пока просто файл целиком — заглушка для расширенной логики)
        try:
            content = filepath.read_text(encoding='utf-8')
            if not content.strip():
                return
                
            chunk_id = str(uuid.uuid4())
            lines_count = len(content.splitlines())
            
            source_kind = 'docs' if rel_path.startswith(('docs/', 'knowledge/')) or rel_path.endswith('.md') else 'code'
            trust = 'hint' if source_kind == 'docs' else 'verified'
            
            chunk_rowid = self.db.add_chunk(
                chunk_id=chunk_id,
                file_id=file_id,
                content=content,
                source_kind=source_kind,
                trust=trust,
                line_start=1,
                line_end=lines_count
            )
            
            if self.use_embeddings and self.embedder:
                vector = self.embedder.embed_text(content)
                if vector:
                    self.db.add_embedding(chunk_rowid, vector)
        except Exception as e:
            # Скипаем в случае проблем кодировок (бинарники)
            logger.warning(f"Failed parsing file {rel_path}: {e}")
