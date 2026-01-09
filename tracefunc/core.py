import ast, builtins, dis, inspect, os, sys, sysconfig, textwrap
from fastcore.utils import *

comp_node_types = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
comp_code_names = {"<listcomp>", "<setcomp>", "<dictcomp>", "<genexpr>"}
line_node_types = (ast.stmt, ast.ExceptHandler, ast.match_case) + comp_node_types
iter_opnames = {"LIST_APPEND", "SET_ADD", "MAP_ADD", "YIELD_VALUE"}


class NameCollector(ast.NodeVisitor):
    """Collect identifier names mentioned in *this* line, but:
    - do not descend into nested "line nodes" (they are separate output lines),
    - do descend into comprehensions ONLY when the root_node is that comprehension,
    - do not descend into lambdas (own scope)."""
    def __init__(self, root_node): self.root_node, self.names = root_node, set()

    def visit_Name(self, node): self.names.add(node.id)
    def visit_Global(self, node): self.names.update(node.names)
    visit_Nonlocal = visit_Global

    def _root_only(fn): return lambda self, node, *a, **k: None if node is not self.root_node else fn(self, node, *a, **k)

    @_root_only
    def _visit_named_def(self, node): self.names.add(node.name); self.generic_visit(node)

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_named_def

    @_root_only
    def visit_ExceptHandler(self, node):
        if isinstance(node.name, str): self.names.add(node.name)
        self.generic_visit(node)

    @_root_only
    def visit_Import(self, node):
        for a in node.names: self.names.add(a.asname or a.name.split(".")[0])

    @_root_only
    def visit_ImportFrom(self, node):
        for a in node.names: self.names.add(a.asname or a.name)

    def generic_visit(self, node):
        if node is not self.root_node:
            # Nested line nodes and Lambas are separate.
            if isinstance(node, (line_node_types, ast.Lambda)): return
            # For non-comprehension line nodes, don't descend into comprehensions (they'll be recorded as their own nodes)
            if isinstance(node, comp_node_types) and not isinstance(self.root_node, comp_node_types): return
        super().generic_visit(node)


def tracefunc(fn, /, *args, target_func=None, **kwargs):
    """
    Trace execution using sys.monitoring (Python 3.12+), returning a list of per-call traces.

    API:
      - `fn(*args, **kwargs)` is executed.
      - `target_func` (optional) selects which function's calls are recorded.
        Defaults to `fn` for backwards compatibility.

    Return:
      - list of length <= 10
      - one element per call to `target_func` (including recursion)
      - each element is: (stack_str, trace_dict)

        stack_str: call stack string (filtered so `fn` is the shallowest frame shown)
        trace_dict: {
          "<source snippet for AST-line>": (
              hit_count,
              {
                "var": [ (type_name, truncated_repr), ... up to 10 ],
                ...
              }
          ),
          ...
        }

    Semantics:
      - "Line" means an AST-level line: separate statements (even if on one physical line via `;`).
      - Compound statements are keyed by their header only.
      - Comprehensions are ALSO treated as a line node and are monitored, including inside
        the comprehension frame, with per-iteration snapshots.
      - Snapshots are recorded after each line finishes, so assignments show updated values.
      - Requires Python 3.12+ (sys.monitoring instruction events).
    """
    if sys.version_info < (3, 12):
        raise RuntimeError("tracefunc requires Python 3.12+ (sys.monitoring instruction events).")

    stack_anchor = fn.__code__ if target_func is not None else None
    stack_base = os.path.dirname(stack_anchor.co_filename) if stack_anchor else None
    repls = []
    venv = os.environ.get("VIRTUAL_ENV")
    if venv: repls.append((os.path.realpath(venv), "$VIRTUAL_ENV"))
    home = os.path.expanduser("~")
    if home: repls.append((os.path.realpath(home), "~"))
    for p, label in (
        (sys.prefix, "$PYTHON_PREFIX"),
        (getattr(sys, "base_prefix", None), "$PYTHON_BASE"),
        (getattr(sys, "exec_prefix", None), "$PYTHON_EXEC_PREFIX"),
        (getattr(sys, "base_exec_prefix", None), "$PYTHON_BASE_EXEC_PREFIX"),
    ):
        if p: repls.append((os.path.realpath(p), label))
    paths = sysconfig.get_paths()
    for key, label in (
        ("purelib", "$SITE_PACKAGES"),
        ("platlib", "$PLAT_SITE_PACKAGES"),
        ("stdlib", "$PYTHON_STDLIB"),
        ("platstdlib", "$PYTHON_PLAT_STDLIB"),
    ):
        if key in paths: repls.append((os.path.realpath(paths[key]), label))
    repls.sort(key=lambda x: len(x[0]), reverse=True)
    if target_func is None: target_func = fn

    try: src_lines, block_first_lineno = inspect.getsourcelines(target_func)
    except (OSError, TypeError) as e: raise ValueError("requires a Python function with retrievable source code.") from e

    src = textwrap.dedent("".join(src_lines))

    def _leading_ws_len(s): return len(s) - len(s.lstrip(" \t"))

    nonblank = [ln for ln in src_lines if ln.strip()]
    base_indent = min((_leading_ws_len(ln) for ln in nonblank), default=0)
    mod = ast.parse(src)

    # Find the function node corresponding to `target_func` within the retrieved source block.
    want_def_line = target_func.__code__.co_firstlineno
    want_rel_line = want_def_line - block_first_lineno + 1
    root = None
    for n in mod.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == target_func.__name__:
            if getattr(n, "lineno", None) == want_rel_line:
                root = n
                break
            root = root or n
    if root is None: raise ValueError("Could not locate function definition in retrieved source.")

    # Source slicing helpers (dedented source coordinates).
    src_lines_ded = src.splitlines(keepends=True)
    line_offsets = [0]
    for ln in src_lines_ded: line_offsets.append(line_offsets[-1] + len(ln))

    def _to_index(lineno, col):
        return 0 if lineno < 1 else len(src) if lineno > len(src_lines_ded) else min(line_offsets[lineno - 1] + max(col, 0), len(src))

    def _header_end_pos(node):
        """For compound nodes, treat only header as the "line": cut at start of first statement/case in their suite. For simple statements/comprehensions, use end positions."""
        # Comprehensions: treat entire node span.
        if isinstance(node, comp_node_types):
            return getattr(node, "end_lineno", node.lineno), getattr(node, "end_col_offset", node.col_offset)

        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            return getattr(first, "lineno", node.lineno), getattr(first, "col_offset", node.col_offset)

        cases = getattr(node, "cases", None)  # match statement
        if isinstance(cases, list) and cases:
            first = cases[0]
            return getattr(first, "lineno", node.lineno), getattr(first, "col_offset", node.col_offset)

        return getattr(node, "end_lineno", node.lineno), getattr(node, "end_col_offset", node.col_offset)

    def _wrap_comp_key(seg, node):
        seg = seg.strip()
        # Generator expressions print as (...) in source; wrap to make it obvious in keys.
        if isinstance(node, ast.GeneratorExp) and not (seg.startswith("(") and seg.endswith(")")): return f"({seg})"
        # List/Set/Dict comps are already bracketed/braced in source; keep as-is.
        return seg

    class _LineInfo:
        def __init__(self, key, vars, span): self.key, self.vars, self.span = key, vars, span

    # Collect all AST "lines" under the function (statements + except/case headers + comprehensions), excluding root def.
    line_nodes = []

    def _collect(n):
        for ch in ast.iter_child_nodes(n):
            if isinstance(ch, line_node_types) and ch is not root: line_nodes.append(ch)
            _collect(ch)

    _collect(root)

    def _segment_for(node):
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
            try: seg = ast.unparse(node).strip()
            except Exception: pass
        if isinstance(node, comp_node_types): seg = _wrap_comp_key(seg, node)
        return seg

    # Build line infos + fast lookup structures.
    line_infos, used_keys, line_to_stmt_ids, line_to_comp_ids = [], set(), {}, {}

    for node in line_nodes:
        seg = _segment_for(node)
        if not seg: continue

        key = seg
        while key in used_keys: key += " "  # keep "source-like" key while ensuring uniqueness
        used_keys.add(key)

        collector = NameCollector(node)
        collector.visit(node)
        var_names = tuple(sorted(collector.names))

        sline = block_first_lineno + getattr(node, "lineno", 1) - 1
        scol = base_indent + getattr(node, "col_offset", 0)
        end_line, end_col = _header_end_pos(node)
        eline = block_first_lineno + end_line - 1
        ecol = base_indent + end_col

        idx = len(line_infos)
        is_comp = isinstance(node, comp_node_types)
        line_infos.append(_LineInfo(key=key, vars=var_names, span=(sline, scol, eline, ecol)))
        for ln in range(sline, eline + 1):
            if is_comp: line_to_comp_ids.setdefault(ln, []).append(idx)
            else: line_to_stmt_ids.setdefault(ln, []).append(idx)

    def _span_size(span): return (span[2] - span[0], span[3] - span[1])

    for ids in (*line_to_stmt_ids.values(), *line_to_comp_ids.values()):
        ids.sort(key=lambda i: _span_size(line_infos[i].span))  # smallest span first

    MAX_SAMPLES = 10

    # Bytecode offset -> (line, col) cache (PEP 657)
    pos_cache = {}

    def _positions_for(code):
        return pos_cache.setdefault(code, {ins.offset: (pos.lineno, pos.col_offset) for ins in dis.get_instructions(code) if (pos := getattr(ins, "positions", None)) and pos.lineno is not None and pos.col_offset is not None})

    # Cache offsets for iteration-result opcodes per code object.
    iter_op_cache = {}

    def _iter_op_offsets(code):
        return iter_op_cache.setdefault(code, {ins.offset for ins in dis.get_instructions(code) if ins.opname in iter_opnames})

    def _contains(span, ln, col):
        sl, sc, el, ec = span
        return not (ln < sl or ln > el or (ln == sl and col < sc) or (ln == el and col >= ec))

    def _lookup_id(map, ln, col):
        ids = map.get(ln)
        if not ids: return None
        for i in ids:  # smallest span first
            if _contains(line_infos[i].span, ln, col): return i
        return None

    _lookup_line_id = lambda ln, col: _lookup_id(line_to_stmt_ids, ln, col)
    _lookup_comp_line_id = lambda ln, col: _lookup_id(line_to_comp_ids, ln, col)

    def _truncate(s, n=50): return s if len(s) <= n else (s[: max(0, n - 3)] + "...")

    def _describe(value):
        try: r = repr(value)
        except Exception as e: r = f"<repr-error {type(e).__name__}: {e}>"
        return (type(value).__name__, _truncate(r, 50))

    def _lookup_name(name, frame):
        if name in frame.f_locals: return frame.f_locals[name]
        if name in frame.f_globals: return frame.f_globals[name]
        b = frame.f_builtins
        if isinstance(b, dict) and name in b: return b[name]
        if hasattr(builtins, name): return getattr(builtins, name)
        raise NameError(name)

    target_code = target_func.__code__
    root_filename = target_code.co_filename

    # Span for "what we consider part of the target_func block" (includes nested defs in its source region).
    root_sline = block_first_lineno + getattr(root, "lineno", 1) - 1
    root_eline = block_first_lineno + getattr(root, "end_lineno", getattr(root, "lineno", 1)) - 1

    def _should_trace_code(code): return code.co_filename == root_filename and root_sline <= code.co_firstlineno <= root_eline

    # Per recorded call: data dict, per-frame "current stmt", and other maps.
    class _CallState:
        def __init__(self, data, current_stmt_by_fid, stack): store_attr()

    def _new_call_state(stack):
        data = {info.key: {"count": 0, "vars": {v: [] for v in info.vars}} for info in line_infos}
        return _CallState(data=data, current_stmt_by_fid={}, stack=stack)

    calls = []
    # Frame id -> call index (propagates from target frame to children)
    call_by_fid = {}
    # For comprehension frames: frame id -> line_id of the comprehension node they correspond to.
    comp_lineid_by_fid = {}

    def _stack_str(frame):
        if stack_anchor is None: return ""
        frames = []
        while frame:
            frames.append(frame)
            frame = frame.f_back
        frames.reverse()
        for i, fr in enumerate(frames):
            if fr.f_code is stack_anchor:
                frames = frames[i:]
                break
        else: return ""
        def _shorten(path):
            if stack_base and path.startswith(stack_base + os.sep):
                return os.path.relpath(path, stack_base)
            if not os.path.isabs(path): return path
            for base, label in repls:
                if path == base or path.startswith(base + os.sep):
                    return label + path[len(base):]
            return path
        out = []
        for fr in frames:
            path = _shorten(fr.f_code.co_filename)
            name = getattr(fr.f_code, "co_qualname", fr.f_code.co_name)
            out.append(f"{name} ({path}:{fr.f_lineno})")
        return "\n".join(out)

    def _snapshot(call_idx, line_id, frame, *, force=False):
        st = calls[call_idx]
        info = line_infos[line_id]
        entry = st.data[info.key]
        entry["count"] += 1
        if entry["count"] > MAX_SAMPLES and not force: return
        vd = entry["vars"]
        for name in info.vars:
            try: vd[name].append(_describe(_lookup_name(name, frame)))
            except Exception: vd[name].append(("NameError", "<unavailable>"))

    def _run_with_monitoring():
        monitoring = sys.monitoring
        tool_id = None
        for tid in range(0, 16):
            try:
                monitoring.use_tool_id(tid, "tracefunc")
                tool_id = tid
                break
            except Exception: continue
        if tool_id is None: raise RuntimeError("tracefunc could not acquire a sys.monitoring tool id.")

        events = monitoring.events
        enabled = events.INSTRUCTION | events.PY_START | events.PY_RETURN | events.PY_UNWIND

        def _mon_start(code, offset):
            if not _should_trace_code(code): return
            frame = sys._getframe(1)
            fid = id(frame)

            parent = frame.f_back
            if parent is not None and (parent_call := call_by_fid.get(id(parent))) is not None:
                call_by_fid[fid] = parent_call
                calls[parent_call].current_stmt_by_fid[fid] = None

            if code is target_code:
                # Still propagate mapping so inner frames can be ignored consistently.
                if len(calls) >= 10: return
                calls.append(_new_call_state(_stack_str(frame)))
                call_idx = len(calls) - 1
                call_by_fid[fid] = call_idx
                calls[call_idx].current_stmt_by_fid[fid] = None
                return

            # If this is a comprehension frame, try to map it to a comprehension line_id.
            # (Only meaningful if it belongs to a call.)
            if call_by_fid.get(fid) is not None and code.co_name in comp_code_names:
                for off, (ln, col) in sorted(_positions_for(code).items()):
                    line_id = _lookup_comp_line_id(ln, col)
                    if line_id is not None:
                        comp_lineid_by_fid[fid] = line_id
                        break

        def _mon_end(code, offset, _arg):
            if not _should_trace_code(code): return
            frame = sys._getframe(1)
            fid = id(frame)

            call_idx = call_by_fid.pop(fid, None)
            if call_idx is None: return

            st = calls[call_idx]
            prev = st.current_stmt_by_fid.pop(fid, None)
            # Flush the last non-comprehension stmt if present.
            if prev is not None: _snapshot(call_idx, prev, frame)
            # Clean comp mapping too.
            comp_lineid_by_fid.pop(fid, None)

        def _mon_instruction(code, offset):
            if not _should_trace_code(code): return
            frame = sys._getframe(1)
            fid = id(frame)

            call_idx = call_by_fid.get(fid)
            if call_idx is None: return

            pos = _positions_for(code).get(offset)
            # Special handling for comprehensions: snapshot per iteration using element/produce opcodes.
            if code.co_name in comp_code_names:
                line_id = comp_lineid_by_fid.get(fid)
                if line_id is None: return
                if offset in _iter_op_offsets(code): _snapshot(call_idx, line_id, frame)
                return

            if not pos: return
            ln, col = pos
            if offset in _iter_op_offsets(code):
                comp_id = _lookup_comp_line_id(ln, col)
                if comp_id is not None: _snapshot(call_idx, comp_id, frame)
            cur = _lookup_line_id(ln, col)
            if cur is None: return

            st = calls[call_idx]
            prev = st.current_stmt_by_fid.get(fid)
            if prev is None: st.current_stmt_by_fid[fid] = cur
            elif prev != cur:
                _snapshot(call_idx, prev, frame)
                st.current_stmt_by_fid[fid] = cur

        monitoring.set_events(tool_id, enabled)
        monitoring.register_callback(tool_id, events.PY_START, _mon_start)
        monitoring.register_callback(tool_id, events.PY_RETURN, _mon_end)
        monitoring.register_callback(tool_id, events.PY_UNWIND, _mon_end)
        monitoring.register_callback(tool_id, events.INSTRUCTION, _mon_instruction)

        try: fn(*args, **kwargs)
        finally:
            monitoring.set_events(tool_id, events.NO_EVENTS)
            monitoring.free_tool_id(tool_id)

    _run_with_monitoring()

    # Convert per-call states into the requested output format.
    return [(st.stack, {k: (v["count"], v["vars"]) for k, v in st.data.items()}) for st in calls]
