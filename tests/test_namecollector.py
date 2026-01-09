import ast

from tracefunc.core import NameCollector


def _collect_names(src, node):
    collector = NameCollector(node)
    collector.visit(node)
    return collector.names


def test_names_in_statement_do_not_descend_into_comprehension():
    mod = ast.parse(
        "def f(n):\n"
        "    xs = [i * i for i in range(n)]\n"
        "    return xs\n"
    )
    fn = mod.body[0]
    assign = fn.body[0]
    assert _collect_names(mod, assign) == {"xs"}


def test_names_in_comprehension_include_iteration_vars():
    mod = ast.parse("def f(n):\n    xs = [i * i for i in range(n)]\n")
    fn = mod.body[0]
    comp = fn.body[0].value
    assert _collect_names(mod, comp) == {"i", "n", "range"}


def test_names_for_import_and_except_handler_are_root_only():
    mod = ast.parse("""\
def f():
    import os.path as p
    try:
        1 / 0
    except Exception as err:
        return err
""")
    fn = mod.body[0]
    imp = fn.body[0]
    try_stmt = fn.body[1]
    handler = try_stmt.handlers[0]
    assert _collect_names(mod, imp) == {"p"}
    assert _collect_names(mod, handler) == {"Exception", "err"}
