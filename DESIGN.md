# Tracefunc: AST-level execution tracing via `sys.monitoring`

## Summary

This project provides a single entry point:

```py
tracefunc(fn, *args, target_func=None, **kwargs) -> list[tuple[str, dict[str, tuple[int, dict[str, list[tuple[str, str]]]]]]]
```

It runs the target function under instruction-level monitoring (`sys.monitoring`) and returns a `list` of per-call traces. Each element is a `(stack_str, trace_dict)` tuple where `trace_dict` is keyed by AST-level "lines" (statements and comprehensions), not physical lines in the source file. Each value is:

- `hit_count`: how many times that AST line was executed
- `vars_map`: `{var_name: [(type_name, truncated_repr), ...]}` capturing observed values on each hit, truncated to 50 characters, with up to 10 samples per variable per line.

Key behaviors:

- Multiple statements on one physical line (semicolon-separated) are treated as separate AST lines.
- Compound statements are keyed by their header only (e.g., `for ...:`).
- Comprehensions are treated as their own AST lines and are monitored per iteration.
- Snapshots are recorded after a line finishes, so assignments show updated values.
- Python 3.12+ only (`sys.monitoring` + `PEP 657` instruction positions).

---

## Output shape

- Return type: `list`, length <= 10
- Each element: `(stack_str, trace_dict)`
  - `stack_str`: call stack string with `fn` as the shallowest frame shown (empty when `target_func` is `None`)
  - `trace_dict`: `dict` mapping `AST line` source snippet -> (`hit_count`, `vars_map`)
- One element per call to `target_func` (including recursion). `target_func` defaults to `fn`.

Example value shape:

```py
(
    "main (example.py:10)\nfn (example.py:42)",
    {
        "x = 1": (1, {"x": [("int", "1")]})
    },
)
```

---

## "Line" definition (AST-level)

Traceable "lines" include:

- `ast.stmt` nodes (statements)
- `ast.ExceptHandler` (so `except ...:` is treated as a header line)
- `ast.match_case` (match/case headers)
- `ast.ListComp`, `ast.SetComp`, `ast.DictComp`, `ast.GeneratorExp` (comprehensions)

A single physical line can contain multiple AST statements, which become separate keys.  For compound statements, only the header is used as the key. For comprehensions, the full expression span is used and wrapped as needed (generator expressions are parenthesized in keys).

---

## Static analysis

### Source acquisition

- Uses `inspect.getsourcelines(target_func)` and `textwrap.dedent` to parse a clean AST.
- Tracks `block_first_lineno` and base indentation so AST coordinates map back to file coordinates.

### Locate the correct function node

Because `getsourcelines` can return a block with multiple defs, the parser selects the `ast.FunctionDef` or `ast.AsyncFunctionDef` by name and line number relative to the retrieved block.

### Enumerate AST line nodes

The implementation recursively walks the AST under the function node and collects all nodes of the "line" types listed above, excluding the root function definition itself.

### Span slicing and key creation

- Simple statements and comprehensions use their full node span.
- Compound statements use a header-only span that stops at the first body element.
- Source snippets are sliced from the dedented source; if slicing fails, `ast.unparse` is used as a fallback.
- Duplicate snippets are disambiguated by appending spaces until unique.

### `NameCollector` (variable discovery)

`NameCollector` is a module-level AST visitor used to collect identifier names for a given line node. Rules:

- Do not descend into nested line nodes (they are separate output lines).
- Do not descend into lambdas (separate scope).
- Do not descend into comprehensions unless the root node is the comprehension.
- For `def`/`class` nodes, include the defined name.
- For `import` nodes, include introduced names.
- For `except` handlers, include the exception name and any names referenced in the handler type.

This ensures statement lines record only the names relevant to that statement, while comprehension lines include the internal iteration variables.

---

## Dynamic tracing

### Monitoring and scope filtering

The tracer uses `sys.monitoring` with `INSTRUCTION`, `PY_START`, `PY_RETURN`, and `PY_UNWIND` events. Frames are considered in scope if:

- `code.co_filename` matches the target file, and
- `code.co_firstlineno` is within the source block range for `target_func`.

This keeps tracing focused to the target function and any nested defs within its source block. Comprehension frames are included and mapped to their corresponding comprehension line.

### Call stack capture

Each recorded call includes a `stack_str` captured on call entry. The stack is a newline-joined
list of `func_name (file:line)` frames, filtered so `fn` is the shallowest frame shown (frames
above `fn` are omitted). For frames under `fn`'s directory, `file` is shown relative to that
directory; other frames keep full paths. If `target_func` is `None`, `stack_str` is empty.

### Mapping execution position to an AST line

- `dis.get_instructions` is used to build a mapping from instruction offset -> (`lineno`, `col`).
- The current (`lineno`, `col`) is matched against the smallest AST span that contains it.

This is what enables statement-level tracing when multiple statements share a single physical line.

### Statement transitions and snapshots

Per frame, the tracer tracks the current AST line id. When it changes, the previous line is snapshotted. On `PY_RETURN` / `PY_UNWIND`, the current line is also snapshotted.

This "snapshot after line completes" approach makes assignment lines reflect updated values.

### Comprehension handling

Comprehensions are their own AST line nodes and are traced per iteration.

- Each comprehension frame is mapped back to its comprehension line by scanning instruction positions until a span match is found.
- Per-iteration snapshots are taken when iteration-result opcodes execute: `LIST_APPEND`, `SET_ADD`, `MAP_ADD`, and `YIELD_VALUE`.

This yields per-iteration variable values for comprehension internals.

### Variable value capture

For each line hit (up to 10 samples per variable per line):

1. Resolve names from `frame.f_locals`
2. Else `frame.f_globals`
3. Else `frame.f_builtins`

Values are recorded as (`type_name`, `truncated_repr`). Unavailable values (e.g., `del x`) produce (`NameError`, `"<unavailable>"`).

---

## Behavioral notes and caveats

- Header hit counts for compound statements can be non-obvious because they depend on compiler details; tests generally check presence rather than exact header counts.
- Key snippets are best-effort slices; unusual formatting or multiline expressions may fall back to `ast.unparse`.
- Builtins/globals are included when referenced by name (e.g., `len`, `range`).
- Opcode-level tracing is slower than line-level tracing and intended for debugging, not production.
- `sys.gettrace` is not disturbed; `sys.monitoring` is enabled and cleaned up inside a finally block.

---

## Test suite overview (`pytest`)

The tests emphasize:

- output shape (`list` of `dict`s) and sample formatting
- statement granularity (semicolon splits, one-line loop bodies)
- comprehension tracing (separate line, per-iteration values)
- generator expression key formatting
- nested defs/classes and nested execution
- globals and builtins resolution
- deletion/unavailable values
- duplicate key disambiguation
- sample cap and count growth
- recursion and `target_func` selection
- `NameCollector` behavior in isolation
- stack string capture and call-site differentiation

---

## Opportunities to simplify by changing the specification

If you want to reduce complexity while keeping core utility, the biggest levers are:

1) Treat "line" as physical line
- Use `sys.settrace` `"line"` events, drop instruction mapping.
- Tradeoff: cannot split semicolon-separated statements.

2) Drop per-iteration comprehension snapshots
- Keep comprehension as a single line hit per call.
- Tradeoff: lose iteration values.

3) Record only locals
- Avoid globals/builtins lookup.
- Tradeoff: less context for statements that reference globals or builtins.

4) Store only the last value per variable per line
- Drop sample lists and caps.
- Tradeoff: no history across iterations.

These are optional; the current implementation keeps the full, more precise behavior.
