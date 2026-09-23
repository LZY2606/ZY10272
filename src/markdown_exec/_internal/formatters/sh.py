# Formatter for executing shell code.

from __future__ import annotations

import subprocess
from typing import Any

from markdown_exec._internal.formatters.base import ExecutionError, ExecutionTimeout, base_format
from markdown_exec._internal.rendering import code_block


def _run_sh(
    code: str,
    returncode: int | None = None,
    session: str | None = None,  # noqa: ARG001
    id: str | None = None,  # noqa: A002,ARG001
    timeout: float | None = None,
    **extra: str,
) -> str:
    try:
        process = subprocess.run(  # noqa: S603
            ["sh", "-c", code],  # noqa: S607
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            encoding="utf8",
            timeout=timeout or None,
        )
    except subprocess.TimeoutExpired as error:
        partial_output = error.output or ""
        if isinstance(partial_output, bytes):
            partial_output = partial_output.decode("utf8", "replace")
        raise ExecutionTimeout(code_block("sh", partial_output, **extra)) from error
    if process.returncode != returncode:
        raise ExecutionError(code_block("sh", process.stdout, **extra), process.returncode)
    return process.stdout


def _format_sh(**kwargs: Any) -> str:
    return base_format(language="sh", run=_run_sh, **kwargs)
