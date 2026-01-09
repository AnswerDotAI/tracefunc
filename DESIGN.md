# Tracefunc: AST‑level execution tracing via `sys.settrace`

## Summary

This project implements a single entry point:

```py
tracefunc(fn, *args, **kwargs) -> dict[str, (hit_count, vars_map)]
```

It runs the target function under a tracing hook (`sys.settrace`) and returns a structured dictionary keyed by **AST-level “lines”** (statements), not physical lines in the source file. Each key is the **source snippet** for that AST statement. Each value is:

* `hit_count`: how many times that statement was executed
* `vars_map`: `{var_name: [(type_name, truncated_repr), ...]}`
  capturing observed values *on each hit*, truncated to 50 characters, with at most 10 samples per line.

The implementation is designed to handle:

* multiple statements on one physical line (semicolon-separated),
* nested `def`/`class` bodies as independent traced lines,
* comprehensions treated as a **single line** (no variable capture from inside them),
* restoration of any pre-existing tracing function even on exceptions.

It intentionally targets **Python 3.11+** to rely on PEP 657 fine-grained source positions.

---

## Requirements captured from the specification

### Output shape

* Return type: `dict`
* Number of keys: **equal to the number of AST “lines”** (statements) in the function, including nested function/class bodies as separate lines.
* Keys: **source code snippets** corresponding to those AST “lines”.
* Values: `(hit_count, var_values_dict)`

### “Line” definition (AST-level)

* A single physical line can contain multiple AST statements:

  * Example: `for i in range(10): print(i); print(i+1)`
    is treated as three distinct “lines”:

    * `for i in range(10):`
    * `print(i)`
    * `print(i + 1)`

### Variable value capture

* For each traced line hit:

  * Determine the variables “mentioned” on that line.
  * Record their values (type + repr truncated to 50 chars).
  * Keep up to 10 samples per line (per variable).

### Comprehension rule

* A comprehension is treated as **one line**.
* Do **not** output values of variables *inside* a comprehension.

### Nested defs/classes

* Nested `def` and `class` statements are separate lines.
* Their bodies are traced if executed (e.g., method bodies when called, inner function body when called).

---

## Implementation overview

The implementation is split conceptually into two phases:

1. **Static analysis (AST + source slicing)**

   * parse source
   * enumerate statement-level nodes
   * compute a stable key snippet for each node (“AST line”)
   * collect variable names mentioned per node
   * compute per-node source spans in file coordinates

2. **Dynamic tracing (sys.settrace + opcode positions)**

   * enable opcode-level tracing (`frame.f_trace_opcodes = True`)
   * map current `(lineno, col_offset)` to the correct AST statement span
   * count hits and snapshot variables

---

## Why opcode-level tracing is needed

`sys.settrace` “line” events are too coarse for this spec because they report only the **physical line number**. If multiple statements exist on one physical line (semicolon-separated), line events cannot distinguish them reliably.

Python 3.11 introduces fine-grained instruction positions (PEP 657), allowing mapping of each opcode to a `(lineno, col_offset)` pair. By switching to opcode tracing:

* you can tell **which statement** is currently executing, even on the same physical line,
* you can detect statement transitions by observing changes in `(lineno, col)` and matching to AST statement spans.

This is the core reason the implementation requires Python 3.11+.

---

## Static analysis details

### Source acquisition and normalization

* Uses `inspect.getsourcelines(fn)` to fetch the defining source block.
* Dedents the source with `textwrap.dedent` to simplify AST parsing.
* Tracks the original file line numbers (`block_first_lineno`) and base indentation so AST `(lineno, col_offset)` can be converted back into file coordinates.

### Locating the correct function AST node

Because `getsourcelines` can return a block containing multiple definitions, the implementation tries to match the parsed AST `FunctionDef/AsyncFunctionDef` node by:

* function name, and
* line number relative to the retrieved block.

This reduces the chance of selecting the wrong `def` when multiple same-named functions exist in the block.

### Enumerating “AST lines”

The code collects nodes that represent “lines”:

* `ast.stmt` nodes (statements)
* `ast.ExceptHandler` (so `except ...:` is treated as a header line)
* `match_case` if available (Python structural pattern matching cases)

It recursively visits children and includes nested statement nodes, which naturally includes nested defs/classes.

### Header-only slicing for compound statements

For compound statements like:

* `if ...:`
* `for ...:`
* `while ...:`
* `try:`
* `def ...:`
* `class ...:`
* `except ...:`

…the spec is best met by treating **only the header** as the “line”, not the entire block. The implementation approximates this by slicing source from the node start to the start of its first body element.

This yields keys like `for i in range(3):` rather than capturing the whole suite.

### Variable name extraction per statement

A custom AST visitor collects identifiers (`ast.Name`) mentioned in the statement, with important exclusions:

* **Do not descend into nested statement nodes**: their variables belong to separate output lines.
* **Do not descend into comprehensions** (`ListComp`, `DictComp`, etc.).
* **Do not descend into lambdas** (own scope).
* For `def`/`class`, include the defined name (`inner`, `C`) because it is a variable that becomes bound.

Imports also add the introduced names.

This keeps variable capture aligned to the spec’s statement-level granularity.

### Key uniqueness

Two different statement nodes can slice to identical source snippets (e.g., repeated `x = 1` lines). The implementation ensures keys remain unique by appending spaces until uniqueness is achieved.

This keeps the output a valid dict without losing any statement entries.

---

## Dynamic tracing details

### Scope filtering

The tracer should not attempt to process every frame in the program. It filters frames by:

* same source filename as `fn.__code__.co_filename`
* code objects whose `co_firstlineno` falls inside the source block range for `fn`
* excluding common comprehension frame names: `<listcomp>`, `<genexpr>`, etc.

This aligns with: “comprehensions are one line” and avoids collecting values inside comprehension frames.

### Mapping execution position → AST “line”

For each executing frame, the tracer:

1. uses `dis.get_instructions(code)` to build a map:

   * `offset -> (lineno, col_offset)` for opcodes with valid positions
2. on each `opcode` event, looks up the current position
3. finds the **smallest AST statement span** (precomputed) that contains `(lineno, col)`

This approach allows multiple statements on the same physical line to be differentiated.

### Recording values “after the statement”

A key behavioral choice: snapshots are taken *after* a statement finishes, not when it begins.

Mechanism:

* maintain `current_stmt_id` per frame
* on opcode events, when the statement id changes:

  * snapshot values for the previous statement
* on `return` / `exception`, snapshot the current statement and clean up

This makes assignment lines much more intuitive:

* on `x = 1`, you record `x == 1`, not “unbound”.

### Capturing variable values

For each statement hit (up to 10 samples per statement):

* For each variable name collected statically:

  * resolve from `frame.f_locals`, else `frame.f_globals`, else `frame.f_builtins`
  * capture `(type(value).__name__, truncated_repr(repr(value)))`
  * on lookup errors (e.g., `del x`), record `(ExceptionType, "<unavailable>")`

Truncation is performed on the `repr` string to 50 characters.

### Trace restoration guarantees

The implementation saves `old_trace = sys.gettrace()` and restores it in a `finally` block. This matters because:

* pytest, debuggers, and coverage tools may already have a tracing function installed
* a leaked trace function can break unrelated code and tests

The tests explicitly verify restoration in both normal and exception paths.

---

## Behavioral notes and practical caveats

These are not “bugs”, but important realities of implementing this spec on CPython:

* **Compound statement header hit counts are not always obvious**
  For example, the `for ...:` header may correspond to opcodes executed once or multiple times depending on compiler details. Tests generally verify presence rather than exact header counts for loops/conditionals.

* **Key snippets are best-effort slices**
  The implementation slices from source text using AST positions; for unusual formatting, very long multiline expressions, or edge cases, the fallback may use `ast.unparse`.

* **Values are taken from locals/globals/builtins**
  This matches the idea of “variables mentioned on the line”, but also means you might see entries like `len` (builtin) or module globals.

* **Performance considerations**
  Opcode-level tracing is significantly slower than line-level tracing. This is a tool for debugging/analysis, not production profiling.

---

## Test suite overview (pytest)

The test suite is designed around:

* correctness of structure,
* statement granularity (AST “lines”),
* variable capture rules (including comprehensions),
* sample cap and hit count behavior,
* nested defs/classes,
* restoration of tracing state.

All tests are skipped automatically on Python < 3.11.

### What the tests cover

#### 1) Return shape and invariants

* `test_tracefunc_returns_expected_shape_and_restores_trace_on_success`

  * verifies dict-of-(int, dict)
  * verifies each sample tuple is `(type_name, repr)` and repr length ≤ 50
  * verifies `sys.gettrace()` is restored

#### 2) Basic statement splitting and value capture

* `test_basic_counts_and_variable_values`

  * asserts a simple function produces exactly 3 keys (`x=1`, `y=x+2`, `return y`)
  * asserts hit counts and recorded values

#### 3) Semicolon-separated statements on one physical line

* `test_semicolons_create_multiple_ast_lines_on_one_physical_line`

  * ensures `x=1; y=2; z=x+y; return z` yields 4 distinct “lines”

#### 4) One-line `for` loop with multiple body statements

* `test_for_one_liner_has_separate_header_and_body_lines`

  * ensures distinct keys exist for:

    * loop header
    * each body statement
  * verifies body statements hit 3 times and variable samples track `i = 0,1,2`

#### 5) Comprehension rule enforcement

* `test_comprehension_is_one_line_and_does_not_capture_internal_names`

  * ensures only the assignment target (`xs`) is tracked, not `i`, `range`, `n`
* `test_comprehension_expression_statement_has_no_vars`

  * ensures a bare listcomp expression records no variables (internal names ignored)

#### 6) Nested `def` traced as separate lines, body traced only if executed

* `test_nested_function_body_present_but_not_hit_when_not_called`

  * inner body keys exist but hit count is 0
* `test_nested_function_body_is_traced_when_called`

  * inner body keys hit once, values of `a` and `b` correct

#### 7) Globals and builtins resolved as mentioned variables

* `test_records_builtins_and_globals_as_variables`

  * verifies `GLOB` is resolved and recorded
  * verifies builtin `len` is included and has a builtin type name

#### 8) Deleted/unavailable variables

* `test_deleting_a_variable_records_unavailable_value`

  * verifies `del x` results in `("NameError", "<unavailable>")`

#### 9) Duplicate statement keys handled safely

* `test_duplicate_source_lines_are_disambiguated_with_unique_keys`

  * ensures two `x = 1` statements still produce 2 distinct dict keys

#### 10) Sample cap (10) and count continues beyond cap

* `test_max_10_samples_per_line_but_count_keeps_growing`

  * verifies `hit_count` grows to 20 but only 10 samples are stored
  * sanity checks sample sequences for `i` and `x`

#### 11) Nested `class` and method tracing

* `test_traces_class_body_and_method_body_when_method_is_called`

  * class header, class body assignment, method def are hit
  * method body is traced when invoked

#### 12) Trace restoration on exception

* `test_restores_previous_trace_even_when_traced_function_raises`

  * verifies `sys.gettrace()` restored even when traced function raises

---

## Opportunities to simplify by changing the specification

If you want the tool to remain useful but dramatically simplify the implementation, these spec changes offer the highest leverage.

### 1) Redefine “line” as physical line (or CPython line events)

**Change:** “Line” means physical source line number.

**Simplification:**

* Can use `sys.settrace` `"line"` events.
* No opcode mapping, no column offsets, no PEP 657 dependency.
* Works on Python 3.8+ (and earlier).

**Tradeoff:** loses the ability to distinguish semicolon-separated statements.

---

### 2) Remove the semicolon / same-physical-line requirement

**Change:** “If multiple statements occur on one physical line, treat them as one.”

**Simplification:**

* Still can use `"line"` events.
* Or keep AST but don’t need opcode-level positions.

**Tradeoff:** less precision; still useful for typical code style.

---

### 3) Record only “hits”, not per-variable values

**Change:** return `{line: hit_count}` only.

**Simplification:**

* Removes name collection + value resolution.
* Avoids `repr` hazards and value sampling logic.
* Faster and less memory-heavy.

**Tradeoff:** loses introspection, but still useful for coverage-like tracing.

---

### 4) Record only locals, not globals/builtins

**Change:** “Variables mentioned” means “names that resolve in `frame.f_locals`”.

**Simplification:**

* Avoids resolution order and builtin/global noise (`len`, `range`, etc.).
* Improves determinism across environments.

**Tradeoff:** sometimes you want globals/builtins for context; but often locals are enough.

---

### 5) Only store the last value per variable per line

**Change:** replace list-of-samples with a single `(type, repr)` for most recent hit.

**Simplification:**

* Removes sample limit logic and per-hit storage.
* Output is smaller and easier to consume.

**Tradeoff:** no history across iterations.

---

### 6) Allow tracing only the top-level function body (exclude nested defs/classes)

**Change:** nested `def`/`class` are listed as lines, but their bodies are not traced.

**Simplification:**

* Avoids filtering of frames/code objects and nested span mapping.
* Output becomes more predictable.

**Tradeoff:** you lose deep tracing, but still see definition events.

---

### 7) Treat comprehension bodies as traceable, but don’t capture values

**Change:** allow tracing comprehension frames but capture no variables inside.

**Simplification:**

* Removes special-case frame filtering for `<listcomp>` etc.
* Keeps semantic “comprehension is one line” while simplifying execution filtering.

**Tradeoff:** more overhead from tracing more frames; still less logic.

---

## Possible refactorings

These are refactors that would keep the spec intact but improve maintainability, testability, and extensibility.

### 1) Split into “analysis” and “runtime” modules

Right now the implementation is one function containing multiple responsibilities. Extract:

* `analyze_function_source(fn) -> AnalysisResult`
* `run_trace(analysis, fn, args, kwargs) -> dict`

This makes it easier to:

* test statement extraction independently from tracing,
* cache analysis results across multiple runs.

### 2) Make configuration explicit

Introduce a small config dataclass, e.g.:

* `max_samples: int = 10`
* `repr_limit: int = 50`
* `include_globals: bool = True`
* `include_builtins: bool = True`

This reduces magic constants and lets users tune behavior.

### 3) Replace “append spaces” with stable keying + stable display

Instead of mutating the key string for uniqueness, consider returning:

* a list of entries, or
* dict keyed by a stable id: `(filename, start_line, start_col, end_line, end_col)`
  plus `source_snippet` as a field.

If you must keep dict keys as source snippets, a cleaner approach is suffixing:

* `"x = 1 #2"` rather than appending spaces.

### 4) Improve span lookup performance

The `(lineno -> [stmt_ids])` mapping plus linear scan is fine for small functions but can be optimized:

* Precompute a structure per line:

  * intervals in column space
  * binary search by `col_offset`

Or use an interval tree keyed by `(line, col)`.

### 5) More robust source slicing

Consider always building snippet keys via:

* `ast.get_source_segment(source, node)` (when available and reliable),
* fallback to slicing by offsets.

This can reduce weird formatting edge cases.

### 6) Make value capture safer / more predictable

`repr(value)` can be:

* slow,
* huge,
* side-effectful in rare cases (custom `__repr__`).

Safer strategies:

* time-limited repr (hard without threads/signals),
* size-limited repr of containers,
* catch exceptions (already done),
* optionally show `<repr omitted>` for large/unsafe types.

### 7) Add explicit semantics for “hit” timing

Right now snapshots occur after statement completion (by design). Document and enforce this as part of the API contract, or allow configuration:

* `"before"` vs `"after"` snapshot timing.

---

## Extractable modules that could be independently useful

Several pieces of this project are valuable utilities on their own.

### 1) AST statement enumerator + header slicer

**Module idea:** `ast_lines.py`

Exports:

* `iter_statement_nodes(fn_source_ast_root) -> list[nodes]`
* `header_span(node) -> span`
* `statement_snippet(source, node) -> str`

Useful for:

* static code analysis tools,
* AST-aware linters,
* educational tooling (“show me each statement”).

---

### 2) “Names mentioned” collector with scope/comprehension control

**Module idea:** `name_collection.py`

Exports:

* `names_mentioned(node, *, exclude_comprehensions=True, exclude_nested_statements=True, ...)`

Useful for:

* dependency analysis (“what identifiers does this statement reference?”),
* lightweight def-use chains,
* code review automation.

---

### 3) Bytecode position mapping (PEP 657)

**Module idea:** `pep657_positions.py`

Exports:

* `instruction_positions(code) -> dict[offset, (lineno, col)]`

Useful for:

* profilers/debuggers that need better than line precision,
* coverage tools that want statement-level metrics,
* advanced instrumentation.

---

### 4) Statement-span matcher

**Module idea:** `span_match.py`

Exports:

* `build_span_index(spans) -> index`
* `lookup(index, lineno, col) -> stmt_id`

Useful for:

* mapping runtime program counters to static source regions.

---

### 5) Safe truncating repr utility

**Module idea:** `safe_repr.py`

Exports:

* `describe(value, limit=50) -> (type_name, truncated_repr)`

Useful in:

* logging,
* tracing,
* debugging utilities.

---

## Conclusion

The implementation meets a fairly demanding spec: statement-level keys derived from AST, opcode-level mapping for correct statement discrimination on a single physical line, exclusion of comprehension internals, and full restoration of prior tracing state.

The tests are intentionally targeted at:

* statement granularity,
* value capture correctness,
* rule compliance (comprehensions, nested bodies),
* robustness (duplicate keys, deletions, exceptions),
* and invariants (sample limits, repr truncation, trace restoration).

If you decide this tool is primarily for debugging and education (rather than a strict statement-coverage engine), relaxing the “AST line” requirement to physical lines or reducing value-capture scope are the two biggest simplification opportunities while keeping it broadly useful.

