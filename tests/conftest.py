"""Pytest configuration shared by every test package.

The only thing worth putting here for now is the sys.path tweak that makes the
``helpers`` module importable; it is declared through the ``pythonpath`` option
in ``pyproject.toml``.
"""
