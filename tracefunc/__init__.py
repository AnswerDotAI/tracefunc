"""AST-level execution tracing via sys.monitoring

Modules:

- `tracefunc.skill`: See what a Python function did, line by line: how many times each line ran and what the variables held. Use when working out why code takes a branch, loops, recurses, or computes a wrong value, without changing that code or stepping through a debugger."""

__version__ = "0.0.8"
from .core import tracefunc, TraceResults

