from dataclasses import dataclass
from pathlib import Path
import re

@dataclass
class MarkdownSection:
    title: str          # "## Installation"
    content: str        # Текст секции (включая заголовок)
    level: int          # 1, 2, 3...
    line_start: int
    line_end: int

class MarkdownParser:
    def __init__(self):
        # Регулярка для поиска заголовков (учитываем код блоки, чтобы не парсить комментарии внутри кода)
        self.header_regex = re.compile(r'^(#{1,6})\s+(.*)$')
    
    def parse_file(self, filepath: str | Path) -> list[MarkdownSection]:
        filepath = Path(filepath)
        try:
            content = filepath.read_text(encoding='utf-8')
            lines = content.splitlines()
            
            sections = []
            current_title = ""
            current_level = 0
            current_start = 1
            current_text = []
            
            in_code_block = False
            
            for i, line in enumerate(lines):
                line_idx = i + 1
                
                # Игнорируем заголовки внутри код-блоков
                if line.strip().startswith('```'):
                    in_code_block = not in_code_block
                
                if not in_code_block:
                    match = self.header_regex.match(line)
                    if match:
                        # Сохраняем предыдущую секцию
                        if current_text:
                            # Убираем пустые линии в конце
                            while current_text and not current_text[-1].strip():
                                current_text.pop()
                            if current_text:
                                sections.append(MarkdownSection(
                                    title=current_title,
                                    content='\n'.join(current_text),
                                    level=current_level,
                                    line_start=current_start,
                                    line_end=current_start + len(current_text) - 1
                                ))
                        
                        current_title = line.strip()
                        current_level = len(match.group(1))
                        current_start = line_idx
                        current_text = [line]
                        continue
                
                # Добавляем строку в текущую секцию
                current_text.append(line)
            
            # Сохраняем последнюю секцию
            if current_text:
                while current_text and not current_text[-1].strip():
                    current_text.pop()
                if current_text:
                    sections.append(MarkdownSection(
                        title=current_title,
                        content='\n'.join(current_text),
                        level=current_level,
                        line_start=current_start,
                        line_end=current_start + len(current_text) - 1
                    ))
            
            # Если файл не содержал заголовков, добавим его как единый чанк
            if not sections and lines:
                return [MarkdownSection(
                    title="Document",
                    content=content,
                    level=1,
                    line_start=1,
                    line_end=len(lines)
                )]
            
            return sections
        except Exception as e:
            # На случай проблем с кодировкой
            return []
