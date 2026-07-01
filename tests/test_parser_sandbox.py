"""Tests for src.core.parser_sandbox — parser isolation via subprocess."""

from __future__ import annotations

import pytest


# ── Module-level test functions (must be picklable for sandbox) ─

def _add(a: int, b: int) -> int:
    return a + b


def _greet(name: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {name}!"


def _quick() -> str:
    return "done"


def _slow() -> str:
    import time
    time.sleep(10)
    return "never"


def _fail_func() -> None:
    raise ValueError("test error")


def _big_result() -> str:
    return "x" * 100_000


# ── Import smoke test ──────────────────────────────────────────

def test_import_parser_sandbox() -> None:
    from src.core.parser_sandbox import ParserSandbox, RlimitSandbox

    assert ParserSandbox is not None
    assert RlimitSandbox is not None


# ── RlimitSandbox basic construction ───────────────────────────

def test_rlimit_sandbox_defaults() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox()
    assert sb.cpu_limit == 30
    assert sb.memory_limit == 512 * 1024 * 1024
    assert sb.timeout == 30.0


def test_rlimit_sandbox_custom() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(cpu_limit=10, memory_limit=64 * 1024 * 1024, timeout=5.0)
    assert sb.cpu_limit == 10
    assert sb.memory_limit == 64 * 1024 * 1024
    assert sb.timeout == 5.0


# ── RlimitSandbox.run — simple function ────────────────────────

def test_run_simple_function() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(cpu_limit=10, memory_limit=128 * 1024 * 1024, timeout=10.0)
    result = sb.run(_add, 3, 4)
    assert result == 7


def test_run_function_with_kwargs() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(cpu_limit=10, memory_limit=128 * 1024 * 1024, timeout=10.0)
    result = sb.run(_greet, "World", greeting="Hi")
    assert result == "Hi, World!"


# ── RlimitSandbox.run_with_timeout ─────────────────────────────

def test_run_with_timeout_completes() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(timeout=10.0)
    result = sb.run_with_timeout(_quick, timeout=5.0)
    assert result == "done"


def test_run_with_timeout_exceeded() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(timeout=2.0)

    with pytest.raises(TimeoutError):
        sb.run_with_timeout(_slow, timeout=1.0)


# ── RlimitSandbox.run — error propagation ──────────────────────

def test_run_propagates_exceptions() -> None:
    from src.core.parser_sandbox import RlimitSandbox, SandboxError

    sb = RlimitSandbox(cpu_limit=10, memory_limit=128 * 1024 * 1024, timeout=10.0)

    with pytest.raises(SandboxError, match="test error"):
        sb.run(_fail_func)


# ── Abstract interface ─────────────────────────────────────────

def test_cannot_instantiate_abstract() -> None:
    from src.core.parser_sandbox import ParserSandbox

    with pytest.raises(TypeError):
        ParserSandbox()  # type: ignore[abstract]


def test_rlimit_sandbox_is_subclass() -> None:
    from src.core.parser_sandbox import ParserSandbox, RlimitSandbox

    assert issubclass(RlimitSandbox, ParserSandbox)


# ── Large output handling ──────────────────────────────────────

def test_run_with_large_output() -> None:
    from src.core.parser_sandbox import RlimitSandbox

    sb = RlimitSandbox(cpu_limit=10, memory_limit=256 * 1024 * 1024, timeout=10.0)
    result = sb.run(_big_result)
    assert len(result) == 100_000
    assert result == "x" * 100_000
