"""
Parser Sandbox — resource-isolated subprocess execution for document parsers.

Provides an abstract `ParserSandbox` interface and a concrete `RlimitSandbox`
that enforces CPU time, address-space (memory), and wall-clock limits via
`resource.setrlimit` + `signal.alarm` inside a `subprocess`.

Security note
-------------
This is **resource isolation only**, not a full security sandbox.  The
subprocess still shares the same UID, filesystem namespace, and network
namespace as the parent.  For stronger isolation (seccomp, user namespaces,
etc.) a future subclass (e.g. `SeccompSandbox`) can be added without changing
the interface.

Protocol
--------
The parent pickles (callable, args, kwargs) and sends them to the child via
stdin.  The child unpickles, executes, and writes the pickled result (or
exception) to stdout.  Stderr is reserved for unexpected child errors.
"""

from __future__ import annotations

import pickle
import resource
import signal
import subprocess
import sys
import traceback
from abc import ABC, abstractmethod
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SandboxError(Exception):
    """Base exception for sandbox failures."""


class SandboxTimeoutError(SandboxError, TimeoutError):
    """Raised when the sandboxed function exceeds its wall-clock timeout."""


class SandboxMemoryError(SandboxError, MemoryError):
    """Raised when the sandboxed function exceeds its memory limit."""


class SandboxResourceError(SandboxError):
    """Raised when a resource limit (CPU, etc.) is hit."""


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class ParserSandbox(ABC):
    """Abstract interface for executing functions in a sandboxed subprocess."""

    @abstractmethod
    def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute ``func(*args, **kwargs)`` in a sandboxed subprocess.

        Returns the return value of *func* (must be picklable).

        Raises
        ------
        SandboxError
            Any sandbox-specific failure (timeout, memory, etc.).
        """
        ...

    @abstractmethod
    def run_with_timeout(
        self, func: Callable[..., Any], timeout: float, *args: Any, **kwargs: Any
    ) -> Any:
        """Execute with a wall-clock *timeout* (seconds).

        Equivalent to ``run(func, *args, **kwargs)`` but allows callers to
        override the sandbox's default timeout on a per-call basis.
        """
        ...


# ---------------------------------------------------------------------------
# Worker that executes inside the subprocess
# ---------------------------------------------------------------------------

def _sandbox_worker() -> None:
    """Entry-point for the sandbox subprocess.

    Protocol (stdin → stdout):
    1. Read pickled ``(func, args, kwargs)`` from **stdin**.
    2. Unpickle.
    3. Call ``func(*args, **kwargs)``.
    4. Pickle ``(True, result)`` to **stdout** on success.
    5. Pickle ``(False, exception_info)`` to **stdout** on failure.

    This function is called via ``subprocess.Popen`` after the parent has
    already configured rlimits and signal handlers (the parent passes them as
    command-line arguments so the child can re-apply them after ``exec``).
    """
    try:
        # Read the pickled payload from stdin
        raw = sys.stdin.buffer.read()
        func, args, kwargs = pickle.loads(raw)

        result = func(*args, **kwargs)

        # Success — pickle (True, result) to stdout
        sys.stdout.buffer.write(pickle.dumps((True, result)))
        sys.stdout.buffer.flush()
    except Exception:
        # Capture exception info and send it back
        exc_info = traceback.format_exc()
        sys.stdout.buffer.write(pickle.dumps((False, exc_info)))
        sys.stdout.buffer.flush()
        sys.exit(1)


# ---------------------------------------------------------------------------
# RlimitSandbox
# ---------------------------------------------------------------------------

class RlimitSandbox(ParserSandbox):
    """Sandbox that enforces limits via ``resource.setrlimit`` and ``signal.alarm``.

    Parameters
    ----------
    cpu_limit:
        Soft & hard CPU-time limit in **seconds** (``RLIMIT_CPU``).
        Default: 30.
    memory_limit:
        Soft & hard address-space limit in **bytes** (``RLIMIT_AS``).
        Default: 512 MiB (``512 * 1024 * 1024``).
    timeout:
        Wall-clock timeout in **seconds**, enforced via ``signal.alarm``
        (``SIGALRM``).  Default: 30.0.
    """

    def __init__(
        self,
        cpu_limit: int = 30,
        memory_limit: int = 512 * 1024 * 1024,
        timeout: float = 30.0,
    ) -> None:
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *func* in a subprocess with the configured resource limits."""
        return self._execute(func, *args, _timeout=self.timeout, **kwargs)

    def run_with_timeout(
        self, func: Callable[..., Any], timeout: float, *args: Any, **kwargs: Any
    ) -> Any:
        """Execute *func* with a custom wall-clock *timeout*."""
        return self._execute(func, *args, _timeout=timeout, **kwargs)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        _timeout: float,
        **kwargs: Any,
    ) -> Any:
        """Spawn a subprocess, send the payload, and collect the result."""

        # Serialize the payload
        payload = pickle.dumps((func, args, kwargs))

        # Build a small helper script that the child will run.
        # The child re-applies rlimits (because after fork/exec they are
        # inherited but we want to be explicit) and sets the alarm.
        child_code = (
            "import os, pickle, resource, signal, sys, traceback\n"
            "\n"
            "# --- Re-apply resource limits ---\n"
            f"resource.setrlimit(resource.RLIMIT_CPU, ({self.cpu_limit}, {self.cpu_limit}))\n"
            f"resource.setrlimit(resource.RLIMIT_AS, ({self.memory_limit}, {self.memory_limit}))\n"
            "\n"
            "# --- Wall-clock timeout ---\n"
            f"signal.alarm({int(_timeout)})\n"
            "\n"
            "# --- Execute ---\n"
            "try:\n"
            "    raw = sys.stdin.buffer.read()\n"
            "    func, args, kwargs = pickle.loads(raw)\n"
            "    result = func(*args, **kwargs)\n"
            "    sys.stdout.buffer.write(pickle.dumps((True, result)))\n"
            "    sys.stdout.buffer.flush()\n"
            "except Exception:\n"
            "    exc_info = traceback.format_exc()\n"
            "    sys.stdout.buffer.write(pickle.dumps((False, exc_info)))\n"
            "    sys.stdout.buffer.flush()\n"
            "    sys.exit(1)\n"
        )

        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxError(f"Failed to spawn sandbox subprocess: {exc}") from exc

        # Send payload and wait for result
        try:
            stdout, stderr = proc.communicate(input=payload, timeout=_timeout + 5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise SandboxTimeoutError(
                f"Sandbox subprocess did not finish within {_timeout + 5:.1f}s "
                f"(wall-clock timeout was {_timeout}s)"
            )

        # Decode stdout first — the child always sends pickled result
        # even on failure (success flag tells us what happened).
        try:
            success, data = pickle.loads(stdout)
        except (pickle.UnpicklingError, EOFError, ValueError) as exc:
            # Could not decode stdout — fall back to returncode analysis
            if proc.returncode != 0:
                self._handle_child_error(proc.returncode, stderr, _timeout)
            raise SandboxError(
                f"Failed to unpickle subprocess result.  "
                f"stderr: {stderr.decode(errors='replace')[:500]}"
            ) from exc

        if not success:
            # data is a traceback string from the child
            raise SandboxError(
                f"Sandboxed function raised an exception:\n{data}"
            )

        return data

    def _handle_child_error(
        self, returncode: int, stderr: bytes, _timeout: float
    ) -> None:
        """Map a non-zero child return code to the appropriate exception."""
        stderr_text = stderr.decode(errors="replace")[:1000]

        # SIGALRM → returncode = -signal.SIGALRM (negative on POSIX)
        if returncode == -signal.SIGALRM:
            raise SandboxTimeoutError(
                f"Sandboxed function exceeded wall-clock timeout of {_timeout}s (SIGALRM)"
            )

        # SIGXCPU → RLIMIT_CPU exceeded
        if hasattr(signal, "SIGXCPU") and returncode == -signal.SIGXCPU:
            raise SandboxResourceError(
                f"Sandboxed function exceeded CPU time limit of {self.cpu_limit}s (SIGXCPU).  "
                f"stderr: {stderr_text}"
            )

        # SIGSEGV or SIGBUS → likely memory limit
        if returncode in (-signal.SIGSEGV, -signal.SIGBUS):
            raise SandboxMemoryError(
                f"Sandboxed function likely exceeded memory limit of "
                f"{self.memory_limit / (1024 * 1024):.0f} MiB "
                f"(signal={returncode}).  stderr: {stderr_text}"
            )

        # Generic non-zero exit
        raise SandboxError(
            f"Sandbox subprocess exited with code {returncode}.  "
            f"stderr: {stderr_text}"
        )
