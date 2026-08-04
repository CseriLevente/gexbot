"""Operator commands.

One module per thing an operator does deliberately and rarely. Nothing here is
imported by the engine, the adapters or the configuration layer: these are entry
points, and an entry point that library code depends on stops being one.

There is exactly one command, and it captures raw vendor responses. There is no
command that places an order, sizes a position or starts a strategy, and
``tests/unit/test_architecture.py`` fails the build if one appears.
"""
