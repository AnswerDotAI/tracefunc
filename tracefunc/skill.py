"""Trace a Python function's execution at AST-line level: per-line hit counts and live variable values, via `sys.monitoring`. Use when debugging *why* code takes a branch, loops, recurses, or computes a wrong value, without editing the code under investigation or using an interactive debugger.

## When to reach for this

Any "why does this function do X?" question that source reading alone doesn't settle: which branch fired and with what values, what a loop saw per iteration, what arguments each (possibly recursive) call received. One call replaces print-debugging (no edits to the target code) and pdb (no interactive stepping): the whole story comes back as one readable data structure.

## Usage

    from tracefunc import tracefunc

    def wrapper(n): return sum(target(i) for i in range(n))
    traces = tracefunc(wrapper, 3, target_func=target)
    for stack, trace in traces:
        print(stack)                       # who called it, filtered to relevant frames
        for snippet, (hits, vars_) in trace.items(): print(snippet, hits, vars_)

- `tracefunc(fn, *args, target_func=None, incl_unhit=False, **kwargs)` runs `fn(*args, **kwargs)` and records every call of `target_func` (default: `fn` itself).
- Returns `TraceResults`: a list of up to 10 `(stack_str, trace_dict)` pairs, one per call (recursion included). `trace_dict` maps each executed AST-level line (separate `;`-statements and comprehensions included) to `(hit_count, {var: [(type_name, truncated_repr), ...]})` with up to 10 samples per variable, recorded after the line runs.
- If `fn` raises, the exception is caught and stored in `TraceResults.exc`, and the traces gathered up to the raise are still returned: crash investigation is the main use case, so a raising `fn` is normal, not an error.
- Lines that never executed are omitted; pass `incl_unhit=True` to see them with hit count 0 (useful for "why is this branch never taken?").
- For runaway recursion, lower `sys.setrecursionlimit` first so the run finishes quickly; the 10-call cap keeps output bounded either way.
- Requires Python 3.12+.
"""

from .core import tracefunc, TraceResults

__all__ = ['tracefunc', 'TraceResults']
