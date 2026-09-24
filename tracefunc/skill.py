"""See what a Python function did, line by line: how many times each line ran and what the variables held. Use when working out why code takes a branch, loops, recurses, or computes a wrong value, without changing that code or stepping through a debugger.

## When to reach for this

Any "why does this function do X?" that source reading alone doesn't settle: which branch fired and with what values, what a loop saw each iteration, what arguments each (possibly recursive) call received. One call replaces print-debugging (no edits to the target code) and pdb (no interactive stepping); the whole story comes back as one readable data structure.

## Usage

    from tracefunc import tracefunc

    def wrapper(n): return sum(target(i) for i in range(n))
    traces = tracefunc(wrapper, 3, target_func=target)
    for stack, trace in traces:
        print(stack)                       # who called it, filtered to relevant frames
        for snippet, (hits, vars_) in trace.items(): print(snippet, hits, vars_)

`tracefunc` runs `fn(*args, **kwargs)` and records each call of `target_func` (default `fn`). `doc(tracefunc)` describes the result: at most 10 calls, each a call stack plus a map from every executed line to its hit count and up to 10 samples per variable. Needs Python 3.12+.

Crash investigation is the main use: if `fn` raises, the exception is stored in `traces.exc` and the traces gathered up to the raise are still returned, so a raising `fn` is normal, not an error. `incl_unhit=True` includes lines that never ran, with hit count 0, to see why a branch is never taken. For runaway recursion, lower `sys.setrecursionlimit` first so the run finishes quickly; the 10-call cap bounds the output either way.
"""

from .core import tracefunc, TraceResults

__all__ = ['tracefunc', 'TraceResults']
