"""
Модуль для извлечения символов и связей из исходного кода.
Использует Tree-sitter для полиглотного AST парсинга.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import tree_sitter
import tree_sitter_c_sharp
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_sql

logger = logging.getLogger(__name__)

# Маппинг языков Tree-sitter
TS_LANGUAGES = {
    'c_sharp': tree_sitter.Language(tree_sitter_c_sharp.language()),
    'typescript': tree_sitter.Language(tree_sitter_typescript.language_typescript()),
    'javascript': tree_sitter.Language(tree_sitter_javascript.language()),
    'sql': tree_sitter.Language(tree_sitter_sql.language()),
}

# Маппинг расширений файлов на языки
LANGUAGE_MAP = {
    '.cs': 'c_sharp',
    '.ts': 'typescript', 
    '.tsx': 'typescript',
    '.js': 'javascript', 
    '.jsx': 'javascript',
    '.sql': 'sql',
}

@dataclass
class Symbol:
    name: str              # Имя символа: "UserService"
    qualified_name: str    # Полное имя: "Yurta.Core.Services.UserService"
    kind: str              # Тип: class, method, function, interface, property, enum...
    language: str          # c_sharp, typescript...
    line_start: int        # 1-indexed
    line_end: int          # 1-indexed
    signature: str         # Сигнатура: "public async Task<User> GetById(int id)"
    body: str              # Полный текст узла для чанка

@dataclass
class SymbolEdge:
    source_name: str       # Имя/qualified_name вызывающего (кто)
    target_name: str       # Имя/qualified_name вызываемого (кого)
    kind: str              # CALLS, INHERITS, IMPLEMENTS, IMPORTS, USES


class CodeParser:
    def __init__(self):
        self.parsers = {}
        for lang_name, lang_obj in TS_LANGUAGES.items():
            parser = tree_sitter.Parser(lang_obj)
            self.parsers[lang_name] = parser

    def parse_file(self, filepath: str | Path, language: str) -> list[Symbol]:
        """Извлекает все символы из файла."""
        filepath = Path(filepath)
        if language not in self.parsers:
            logger.warning(f"Language {language} not supported by CodeParser.")
            return []

        try:
            content = filepath.read_bytes()
            tree = self.parsers[language].parse(content)
            
            if language == 'c_sharp':
                return self._parse_csharp_symbols(tree, content)
            elif language == 'typescript' or language == 'javascript':
                return self._parse_ts_js_symbols(tree, content, language)
            elif language == 'sql':
                return self._parse_sql_symbols(tree, content)
            else:
                return []
        except Exception as e:
            logger.error(f"Failed to parse symbols in {filepath}: {e}")
            return []

    def extract_edges(self, filepath: str | Path, language: str) -> list[SymbolEdge]:
        """Извлекает связи (calls, imports, etc.) из файла."""
        filepath = Path(filepath)
        if language not in self.parsers:
            return []

        try:
            content = filepath.read_bytes()
            tree = self.parsers[language].parse(content)
            
            if language == 'c_sharp':
                return self._extract_csharp_edges(tree, content)
            elif language == 'typescript' or language == 'javascript':
                return self._extract_ts_js_edges(tree, content, language)
            elif language == 'sql':
                return self._extract_sql_edges(tree, content)
            else:
                return []
        except Exception as e:
            logger.error(f"Failed to extract edges in {filepath}: {e}")
            return []

    def _get_node_text(self, node: tree_sitter.Node, content: bytes) -> str:
        return content[node.start_byte:node.end_byte].decode('utf-8', errors='replace')


    # -------------------------------------------------------------------------
    # C# Parsing
    # -------------------------------------------------------------------------
    def _parse_csharp_symbols(self, tree: tree_sitter.Tree, content: bytes) -> list[Symbol]:
        symbols = []
        lang = TS_LANGUAGES['c_sharp']
        
        # Рекурсивная функция для прохода по дереву и сбора иерархии
        def traverse(node: tree_sitter.Node, parent_qualified_name: str):
            current_qualified_name = parent_qualified_name
            
            if node.type == 'namespace_declaration' or node.type == 'file_scoped_namespace_declaration':
                name_node = node.child_by_field_name('name')
                if name_node:
                    ns_name = self._get_node_text(name_node, content)
                    current_qualified_name = ns_name if not parent_qualified_name else f"{parent_qualified_name}.{ns_name}"
            
            elif node.type in ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration', 'enum_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    kind = node.type.split('_')[0] # class, struct, interface, record, enum
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    # Получаем сигнатуру (первая строка грубо, или объединение модификаторов и имени)
                    # Для простоты забираем всё до открывающейся {
                    body_node = node.child_by_field_name('body')
                    if body_node:
                        sig_bytes = content[node.start_byte:body_node.start_byte]
                    else:
                        sig_bytes = content[node.start_byte:node.end_byte]
                    signature = sig_bytes.decode('utf-8', errors='replace').strip()
                    
                    body = self._get_node_text(node, content)
                    
                    symbols.append(Symbol(
                        name=name,
                        qualified_name=current_qualified_name,
                        kind=kind,
                        language='c_sharp',
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=signature,
                        body=body
                    ))

            elif node.type in ('method_declaration', 'constructor_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    kind = 'method' if node.type == 'method_declaration' else 'constructor'
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    body_node = node.child_by_field_name('body')
                    if body_node:
                        sig = content[node.start_byte:body_node.start_byte].decode('utf-8', errors='replace').strip()
                    else:
                        sig = content[node.start_byte:node.end_byte].decode('utf-8', errors='replace').strip()

                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind=kind, language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))

            elif node.type == 'property_declaration':
                name_node = node.child_by_field_name('name')
                type_node = node.child_by_field_name('type')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    accessors = node.child_by_field_name('accessors')
                    if accessors:
                        sig = content[node.start_byte:accessors.start_byte].decode('utf-8', errors='replace').strip()
                    else:
                        sig = content[node.start_byte:node.end_byte].decode('utf-8', errors='replace').strip()

                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='property', language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))

            elif node.type == 'field_declaration':
                # fields can have multiple declarators: int a, b;
                decl_list = node.child_by_field_name('declarator')
                # Actually, in tree-sitter-c-sharp, variable_declaration has type and declarators
                # We'll just take the first declarator name
                first_name = None
                for child in node.children:
                    if child.type == 'variable_declaration':
                        for vchild in child.children:
                            if vchild.type == 'variable_declarator':
                                name_node = vchild.child_by_field_name('name')
                                if name_node:
                                    first_name = self._get_node_text(name_node, content)
                                    break
                        if first_name: break
                
                if first_name:
                    current_qualified_name = f"{parent_qualified_name}.{first_name}" if parent_qualified_name else first_name
                    sig = content[node.start_byte:node.end_byte].decode('utf-8', errors='replace').strip()
                    symbols.append(Symbol(
                        name=first_name, qualified_name=current_qualified_name,
                        kind='field', language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))
            
            elif node.type == 'enum_member_declaration':
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='enum_member', language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=name, body=self._get_node_text(node, content)
                    ))
            
            elif node.type in ('event_declaration', 'event_field_declaration'):
                name_node = node.child_by_field_name('name') # for event_decl
                if not name_node:
                    # for event_field_decl, gotta dig into variable_declaration
                    for child in node.children:
                        if child.type == 'variable_declaration':
                            for vchild in child.children:
                                if vchild.type == 'variable_declarator':
                                    name_node = vchild.child_by_field_name('name')
                                    break
                
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    sig_node = node.child_by_field_name('accessor_list')
                    end_byte = sig_node.start_byte if sig_node else node.end_byte
                    sig = content[node.start_byte:end_byte].decode('utf-8', errors='replace').strip()
                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='event', language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))

            elif node.type == 'delegate_declaration':
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    sig = self._get_node_text(node, content).strip()
                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='delegate', language='c_sharp',
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=sig
                    ))

            # traverse children
            for child in node.children:
                traverse(child, current_qualified_name)

        traverse(tree.root_node, "")
        return symbols

    def _extract_csharp_edges(self, tree: tree_sitter.Tree, content: bytes) -> list[SymbolEdge]:
        edges = []
        
        def get_identifier_name(node: tree_sitter.Node) -> str:
            """Extract the rightmost identifier from a member access or just the identifier."""
            if node.type == 'identifier':
                return self._get_node_text(node, content)
            elif node.type == 'member_access_expression':
                name_node = node.child_by_field_name('name')
                if name_node:
                    return self._get_node_text(name_node, content)
            elif node.type == 'generic_name':
                name_node = node.child[-1] # Usually identifier is first or before type_argument_list. Wait, better to just take the first child.
                for child in node.children:
                    if child.type == 'identifier':
                        return self._get_node_text(child, content)
            return self._get_node_text(node, content)

        def traverse(node: tree_sitter.Node, parent_qualified_name: str):
            current_qualified_name = parent_qualified_name
            
            # Scope tracking
            if node.type in ('namespace_declaration', 'file_scoped_namespace_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    ns_name = self._get_node_text(name_node, content)
                    current_qualified_name = ns_name if not parent_qualified_name else f"{parent_qualified_name}.{ns_name}"
            
            elif node.type in ('class_declaration', 'struct_declaration', 'interface_declaration', 'record_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                
                # Check for base classes/interfaces
                for child in node.children:
                    if child.type == 'base_list':
                        for type_node in child.children:
                            if type_node.type in ('identifier', 'generic_name', 'qualified_name'):
                                target_name = get_identifier_name(type_node)
                                # Эвристика: если начинается с 'I' и вторая буква большая, то IMPLEMENTS
                                kind = 'IMPLEMENTS' if target_name.startswith('I') and len(target_name) > 1 and target_name[1].isupper() else 'INHERITS'
                                edges.append(SymbolEdge(
                                    source_name=current_qualified_name,
                                    target_name=target_name,
                                    kind=kind
                                ))
            
            elif node.type in ('method_declaration', 'constructor_declaration', 'property_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
            
            # Edge extraction logic
            if node.type == 'using_directive':
                for child in node.children:
                    if child.type in ('identifier', 'qualified_name'):
                        target_name = self._get_node_text(child, content)
                        edges.append(SymbolEdge(
                            source_name=current_qualified_name or "file_level", 
                            target_name=target_name,
                            kind='IMPORTS'
                        ))
                        
            elif node.type == 'invocation_expression':
                function_node = node.child_by_field_name('function')
                if function_node:
                    target_name = get_identifier_name(function_node)
                    edges.append(SymbolEdge(
                        source_name=current_qualified_name,
                        target_name=target_name,
                        kind='CALLS'
                    ))
            
            elif node.type == 'object_creation_expression':
                type_node = node.child_by_field_name('type')
                if type_node:
                    target_name = get_identifier_name(type_node)
                    edges.append(SymbolEdge(
                        source_name=current_qualified_name,
                        target_name=target_name,
                        kind='CALLS' # Calling constructor
                    ))

            for child in node.children:
                traverse(child, current_qualified_name)

        traverse(tree.root_node, "")
        return edges

    # -------------------------------------------------------------------------
    # TypeScript / JavaScript Parsing
    # -------------------------------------------------------------------------
    def _parse_ts_js_symbols(self, tree: tree_sitter.Tree, content: bytes, language: str) -> list[Symbol]:
        symbols = []
        
        def traverse(node: tree_sitter.Node, parent_qualified_name: str):
            current_qualified_name = parent_qualified_name
            
            if node.type in ('class_declaration', 'interface_declaration', 'enum_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    kind = node.type.split('_')[0]
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    body_node = node.child_by_field_name('body')
                    if body_node:
                        sig = content[node.start_byte:body_node.start_byte].decode('utf-8', errors='replace').strip()
                    else:
                        sig = content[node.start_byte:node.end_byte].decode('utf-8', errors='replace').strip()

                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind=kind, language=language,
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))
            
            elif node.type in ('function_declaration', 'method_definition'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    kind = 'function' if node.type == 'function_declaration' else 'method'
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    body_node = node.child_by_field_name('body')
                    if body_node:
                        sig = content[node.start_byte:body_node.start_byte].decode('utf-8', errors='replace').strip()
                    else:
                        sig = content[node.start_byte:node.end_byte].decode('utf-8', errors='replace').strip()

                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind=kind, language=language,
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))

            elif node.type == 'type_alias_declaration':
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    
                    # Sig up to the "="
                    eq_index = self._get_node_text(node, content).find('=')
                    if eq_index > 0:
                        sig = self._get_node_text(node, content)[:eq_index].strip()
                    else:
                        sig = name

                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='type', language=language,
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))
            
            elif node.type in ('public_field_definition', 'property_signature'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                    sig = self._get_node_text(node, content).strip()
                    symbols.append(Symbol(
                        name=name, qualified_name=current_qualified_name,
                        kind='property' if node.type == 'property_signature' else 'field', language=language,
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=sig, body=self._get_node_text(node, content)
                    ))
            
            elif node.type == 'variable_declarator':
                # Only care about arrow functions assigned to const/let/var at top level or inside classes/modules
                # and maybe exported variables. For simplicity keep functions here.
                value_node = node.child_by_field_name('value')
                if value_node and value_node.type == 'arrow_function':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = self._get_node_text(name_node, content)
                        current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                        body_node = value_node.child_by_field_name('body')
                        if body_node:
                            sig = content[node.start_byte:body_node.start_byte].decode('utf-8', errors='replace').strip()
                        else:
                            sig = name

                        symbols.append(Symbol(
                            name=name, qualified_name=current_qualified_name,
                            kind='function', language=language,
                            line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                            signature=sig, body=self._get_node_text(node, content)
                        ))

            # Traverse children
            for child in node.children:
                traverse(child, current_qualified_name)

        traverse(tree.root_node, "")
        return symbols

    def _extract_ts_js_edges(self, tree: tree_sitter.Tree, content: bytes, language: str) -> list[SymbolEdge]:
        edges = []
        
        def get_identifier_name(node: tree_sitter.Node) -> str:
            if node.type == 'identifier':
                return self._get_node_text(node, content)
            elif node.type == 'member_expression':
                prop_node = node.child_by_field_name('property')
                if prop_node:
                    return self._get_node_text(prop_node, content)
            return self._get_node_text(node, content)

        def traverse(node: tree_sitter.Node, parent_qualified_name: str):
            current_qualified_name = parent_qualified_name
            
            if node.type in ('class_declaration', 'interface_declaration', 'enum_declaration'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name
                
                # Check extends/implements
                for child in node.children:
                    if child.type == 'class_heritage':
                        for heritage_child in child.children:
                            if heritage_child.type == 'extends_clause':
                                for type_node in heritage_child.children:
                                    if type_node.type in ('identifier', 'type_identifier'):
                                        edges.append(SymbolEdge(
                                            source_name=current_qualified_name,
                                            target_name=self._get_node_text(type_node, content),
                                            kind='INHERITS'
                                        ))
                            elif heritage_child.type == 'implements_clause':
                                for type_node in heritage_child.children:
                                    if type_node.type in ('identifier', 'type_identifier'):
                                        edges.append(SymbolEdge(
                                            source_name=current_qualified_name,
                                            target_name=self._get_node_text(type_node, content),
                                            kind='IMPLEMENTS'
                                        ))
                    elif child.type == 'extends_clause': # For interface inheritance
                        for type_node in child.children:
                            if type_node.type in ('identifier', 'type_identifier'):
                                edges.append(SymbolEdge(
                                    source_name=current_qualified_name,
                                    target_name=self._get_node_text(type_node, content),
                                    kind='INHERITS'
                                ))

            elif node.type in ('function_declaration', 'method_definition'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    name = self._get_node_text(name_node, content)
                    current_qualified_name = f"{parent_qualified_name}.{name}" if parent_qualified_name else name

            # Edge Extraction
            if node.type == 'import_statement':
                # find import_clause -> named_imports -> import_specifier etc.
                # Simplification: just extract all identifiers after "import"
                import_clause = node.child_by_field_name('import')
                # Wait, TS tree-sitter uses 'import' keyword, but actual exported names are in import_clause or named_imports
                for child in node.children:
                    if child.type == 'import_clause':
                        for c in child.children:
                            if c.type == 'identifier':
                                edges.append(SymbolEdge(
                                    source_name=current_qualified_name or "file_level",
                                    target_name=self._get_node_text(c, content),
                                    kind='IMPORTS'
                                ))
                            elif c.type == 'named_imports':
                                for spec in c.children:
                                    if spec.type == 'import_specifier':
                                        name_node = spec.child_by_field_name('name')
                                        if name_node:
                                            edges.append(SymbolEdge(
                                                source_name=current_qualified_name or "file_level",
                                                target_name=self._get_node_text(name_node, content),
                                                kind='IMPORTS'
                                            ))
            elif node.type == 'call_expression':
                function_node = node.child_by_field_name('function')
                if function_node:
                    target_name = get_identifier_name(function_node)
                    edges.append(SymbolEdge(
                        source_name=current_qualified_name,
                        target_name=target_name,
                        kind='CALLS'
                    ))
            elif node.type == 'new_expression':
                constructor_node = node.child_by_field_name('constructor')
                if constructor_node:
                    target_name = get_identifier_name(constructor_node)
                    edges.append(SymbolEdge(
                        source_name=current_qualified_name,
                        target_name=target_name,
                        kind='CALLS'
                    ))

            for child in node.children:
                traverse(child, current_qualified_name)

        traverse(tree.root_node, "")
        return edges

    # -------------------------------------------------------------------------
    # SQL Parsing
    # -------------------------------------------------------------------------
    def _parse_sql_symbols(self, tree: tree_sitter.Tree, content: bytes) -> list[Symbol]:
        symbols = []
        
        def traverse(node: tree_sitter.Node):
            if node.type in ('create_table', 'create_view', 'create_index', 'create_function'):
                name = None
                for child in node.children:
                    if child.type in ('identifier', 'object_reference', 'table_reference'):
                        name = self._get_node_text(child, content)
                        break
                
                if name:
                    kind = node.type.split('_')[1] # table, view, index, function
                    # Handle multi-node wrappers like statement
                    body_node = node
                    parent = node.parent
                    if parent and parent.type == 'statement':
                        body_node = parent
                        
                    symbols.append(Symbol(
                        name=name, qualified_name=name,
                        kind=kind, language='sql',
                        line_start=body_node.start_point[0] + 1, line_end=body_node.end_point[0] + 1,
                        signature=name, body=self._get_node_text(body_node, content)
                    ))

            for child in node.children:
                traverse(child)

        traverse(tree.root_node)
        return symbols

    def _extract_sql_edges(self, tree: tree_sitter.Tree, content: bytes) -> list[SymbolEdge]:
        # TODO: Implement SQL edges
        return []

