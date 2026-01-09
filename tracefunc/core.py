import ast
import builtins
import dis
import inspect
import sys
import textwrap
import types
from dataclasses import dataclass
from typing import Any, Callable


def tracefunc(
    fn: Callable, /, *args, **kwargs
) -> dict:
    """
    Trace `fn(*args, **kwargs)` via sys.settrace (opcode-level), returning a dict:

        {
          "<source snippet for AST-line>": (
              hit_count,
              {
                "var": [ (type_name, truncated_repr), ... up to 10 ],
                ...
              }
          ),
          ...
        }

    Notes:
    - "Line" means an AST-level line: separate statements (even if on one physical line via `;`).
    - Compound statements (if/for/while/try/def/class/except/case) are keyed by their header only.
    - Comprehensions are treated as one line: variables *inside* comprehensions are not recorded,
      and comprehension frames (<listcomp>/<genexpr>/...) are not traced.
    - Nested functions/classes are separate lines and are traced when executed.
    - Snapshots are recorded *after* each line finishes (so assignments show updated values).
    - Requires Python 3.11+ (PEP 657 fine-grained positions).
    """
    if sys.version_info < (3, 11):
        raise RuntimeError("tracefunc requires Python 3.11+ (PEP 657 fine-grained positions).")

    try:
        src_lines, block_first_lineno = inspect.getsourcelines(fn)
    except (OSError, TypeError) as e:
        raise ValueError("tracefunc requires a Python function with retrievable source code.") from e

    src = textwrap.dedent("".join(src_lines))

    def _leading_ws_len(s: str) -> int:
        return len(s) - len(s.lstrip(" \t"))

    nonblank = [ln for ln in src_lines if ln.strip()]
    base_indent = min((_leading_ws_len(ln) for ln in nonblank), default=0)

    mod = ast.parse(src)

    # Find the function node corresponding to `fn` within the retrieved source block.
    want_def_line = fn.__code__.co_firstlineno
    want_rel_line = want_def_line - block_first_lineno + 1
    root: ast.AST | None = None
    for n in mod.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn.__name__:
            if getattr(n, "lineno", None) == want_rel_line:
                root = n
                break
            root = root or n
    if root is None:
        raise ValueError("Could not locate function definition in retrieved source.")

    # Source slicing helpers (dedented source coordinates).
    src_lines_ded = src.splitlines(keepends=True)
    line_offsets = [0]
    for ln in src_lines_ded:
        line_offsets.append(line_offsets[-1] + len(ln))

    def _to_index(lineno: int, col: int) -> int:
        if lineno < 1:
            return 0
        if lineno > len(src_lines_ded):
            return len(src)
        return min(line_offsets[lineno - 1] + max(col, 0), len(src))

    match_case = getattr(ast, "match_case", None)
    line_node_types: tuple[type, ...] = (ast.stmt, ast.ExceptHandler) + ((match_case,) if match_case else ())

    def _header_end_pos(node: ast.AST) -> tuple[int, int]:
        """
        For compound nodes, treat only their header as the "line": cut at the start of the first
        statement/case in their suite. For simple statements, use end positions.
        """
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            return getattr(first, "lineno", node.lineno), getattr(first, "col_offset", node.col_offset)

        cases = getattr(node, "cases", None)  # match statement
        if isinstance(cases, list) and cases:
            first = cases[0]
            return getattr(first, "lineno", node.lineno), getattr(first, "col_offset", node.col_offset)

        return getattr(node, "end_lineno", node.lineno), getattr(node, "end_col_offset", node.col_offset)

    @dataclass(frozen=True)
    class _LineInfo:
        key: str
        vars: tuple[str, ...]
        span: tuple[int, int, int, int]  # file (start_line, start_col, end_line, end_col)

    # Collect all AST "lines" under the function (statements + except/case headers), excluding root def.
    line_nodes: list[ast.AST] = []

    def _collect(n: ast.AST) -> None:
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, line_node_types):
                if ch is not root:
                    line_nodes.append(ch)
                _collect(ch)
            else:
                _collect(ch)

    _collect(root)

    class _NameCollector(ast.NodeVisitor):
        """
        Collect identifier names mentioned in *this* line, but:
        - do not descend into nested "line nodes" (they are separate output lines),
        - do not descend into comprehensions,
        - do not descend into lambdas (own scope).
        """
        def __init__(self, root_node: ast.AST):
            self.root_node = root_node
            self.names: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            self.names.add(node.id)

        def visit_Global(self, node: ast.Global) -> None:
            self.names.update(node.names)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
            self.names.update(node.names)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node is not self.root_node:
                return
            if isinstance(node.name, str):
                self.names.add(node.name)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is not self.root_node:
                return
            self.names.add(node.name)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is not self.root_node:
                return
            self.names.add(node.name)
            self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node is not self.root_node:
                return
            self.names.add(node.name)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if node is not self.root_node:
                return
            for a in node.names:
                self.names.add(a.asname or a.name.split(".")[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node is not self.root_node:
                return
            for a in node.names:
                self.names.add(a.asname or a.name)

        def generic_visit(self, node: ast.AST) -> None:
            if node is not self.root_node:
                if isinstance(node, line_node_types):
                    return
                if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                    return
                if isinstance(node, ast.Lambda):
                    return
            super().generic_visit(node)

    def _segment_for(node: ast.AST) -> str:
        start = _to_index(getattr(node, "lineno", 1), getattr(node, "col_offset", 0))
        end_line, end_col = _header_end_pos(node)
        end = _to_index(end_line, end_col)
        seg = src[start:end].rstrip()
        if not seg.strip():
            end_line2 = getattr(node, "end_lineno", getattr(node, "lineno", 1))
            end_col2 = getattr(node, "end_col_offset", getattr(node, "col_offset", 0))
            seg = src[start:_to_index(end_line2, end_col2)].rstrip()
        seg = seg.strip()
        if not seg and hasattr(ast, "unparse"):
            # best-effort fallback (should be rare)
            try:
                seg = ast.unparse(node).strip()
            except Exception:
                pass
        return seg

    # Build line infos + fast lookup structures.
    line_infos: list[_LineInfo] = []
    used_keys: set[str] = set()
    line_to_ids: dict[int, list[int]] = {}

    for node in line_nodes:
        seg = _segment_for(node)
        if not seg:
            continue

        key = seg
        while key in used_keys:
            key += " "  # keep "source-like" key while ensuring uniqueness
        used_keys.add(key)

        collector = _NameCollector(node)
        collector.visit(node)
        var_names = tuple(sorted(collector.names))

        sline = block_first_lineno + getattr(node, "lineno", 1) - 1
        scol = base_indent + getattr(node, "col_offset", 0)
        end_line, end_col = _header_end_pos(node)
        eline = block_first_lineno + end_line - 1
        ecol = base_indent + end_col

        idx = len(line_infos)
        line_infos.append(_LineInfo(key=key, vars=var_names, span=(sline, scol, eline, ecol)))
        for ln in range(sline, eline + 1):
            line_to_ids.setdefault(ln, []).append(idx)

    def _span_size(span: tuple[int, int, int, int]) -> tuple[int, int]:
        sl, sc, el, ec = span
        return (el - sl, ec - sc)

    for ln, ids in line_to_ids.items():
        ids.sort(key=lambda i: _span_size(line_infos[i].span))  # smallest span first

    MAX_SAMPLES = 10
    data: dict[str, dict[str, Any]] = {
        info.key: {"count": 0, "vars": {v: [] for v in info.vars}} for info in line_infos
    }

    # Bytecode offset -> (line, col) cache (PEP 657)
    pos_cache: dict[types.CodeType, dict[int, tuple[int, int]]] = {}

    def _positions_for(code: types.CodeType) -> dict[int, tuple[int, int]]:
        m = pos_cache.get(code)
        if m is not None:
            return m
        d: dict[int, tuple[int, int]] = {}
        for ins in dis.get_instructions(code):
            pos = getattr(ins, "positions", None)
            if not pos or pos.lineno is None or pos.col_offset is None:
                continue
            d[ins.offset] = (pos.lineno, pos.col_offset)
        pos_cache[code] = d
        return d

    def _contains(span: tuple[int, int, int, int], ln: int, col: int) -> bool:
        sl, sc, el, ec = span
        if ln < sl or ln > el:
            return False
        if ln == sl and col < sc:
            return False
        if ln == el and col >= ec:
            return False
        return True

    def _lookup_line_id(ln: int, col: int) -> int | None:
        ids = line_to_ids.get(ln)
        if not ids:
            return None
        for i in ids:  # smallest span first
            if _contains(line_infos[i].span, ln, col):
                return i
        return None

    def _truncate(s: str, n: int = 50) -> str:
        return s if len(s) <= n else (s[: max(0, n - 3)] + "...")

    def _describe(value: Any) -> tuple[str, str]:
        try:
            r = repr(value)
        except Exception as e:
            r = f"<repr-error {type(e).__name__}: {e}>"
        return (type(value).__name__, _truncate(r, 50))

    def _lookup_name(name: str, frame: types.FrameType) -> Any:
        if name in frame.f_locals:
            return frame.f_locals[name]
        if name in frame.f_globals:
            return frame.f_globals[name]
        b = frame.f_builtins
        if isinstance(b, dict) and name in b:
            return b[name]
        if hasattr(builtins, name):
            return getattr(builtins, name)
        raise NameError(name)

    root_filename = fn.__code__.co_filename
    root_sline = block_first_lineno + getattr(root, "lineno", 1) - 1
    root_eline = block_first_lineno + getattr(root, "end_lineno", getattr(root, "lineno", 1)) - 1
    excluded_code_names = {"<listcomp>", "<setcomp>", "<dictcomp>", "<genexpr>", "<lambda>"}

    def _should_trace_code(code: types.CodeType) -> bool:
        return (
            code.co_filename == root_filename
            and root_sline <= code.co_firstlineno <= root_eline
            and code.co_name not in excluded_code_names
        )

    def _snapshot(line_id: int, frame: types.FrameType) -> None:
        info = line_infos[line_id]
        entry = data[info.key]
        entry["count"] += 1
        if entry["count"] > MAX_SAMPLES:
            return
        vd: dict[str, list[tuple[str, str]]] = entry["vars"]
        for name in info.vars:
            try:
                vd[name].append(_describe(_lookup_name(name, frame)))
            except Exception as e:
                vd[name].append((type(e).__name__, "<unavailable>"))

    # Per-frame current line id (tracked by id(frame); removed on return/exception).
    current_stmt_by_fid: dict[int, int | None] = {}

    def tracer(frame: types.FrameType, event: str, arg: Any):
        code = frame.f_code

        if event == "call":
            if _should_trace_code(code):
                frame.f_trace_opcodes = True
                current_stmt_by_fid[id(frame)] = None
                return tracer
            return None

        if not _should_trace_code(code):
            return None

        fid = id(frame)

        if event == "opcode":
            pos = _positions_for(code).get(frame.f_lasti)
            if not pos:
                return tracer

            ln, col = pos
            cur = _lookup_line_id(ln, col)
            if cur is None:
                return tracer

            prev = current_stmt_by_fid.get(fid)
            if prev is None:
                current_stmt_by_fid[fid] = cur
            elif prev != cur:
                _snapshot(prev, frame)  # record after prev finished
                current_stmt_by_fid[fid] = cur
            return tracer

        if event in ("return", "exception"):
            prev = current_stmt_by_fid.pop(fid, None)
            if prev is not None:
                _snapshot(prev, frame)
            return tracer

        return tracer

    old_trace = sys.gettrace()
    sys.settrace(tracer)
    try:
        fn(*args, **kwargs)
    finally:
        sys.settrace(old_trace)

    return {k: (v["count"], v["vars"]) for k, v in data.items()}

