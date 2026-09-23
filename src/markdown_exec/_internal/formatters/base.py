# Generic formatter for executing code.

from __future__ import annotations

import os
import signal
from contextlib import contextmanager
from pathlib import Path
from textwrap import indent
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from markupsafe import Markup

from markdown_exec._internal.logger import get_logger
from markdown_exec._internal.manifest import manifest_recorder
from markdown_exec._internal.rendering import MarkdownConverter, add_source, code_block

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from markdown.core import Markdown

_logger = get_logger(__name__)

default_tabs = ("Source", "Result")
"""Default tab titles."""


@contextmanager
def working_directory(path: str | None = None) -> Iterator[None]:
    """Change the working directory for the duration of the context.

    Parameters:
        path: The path to change the working directory to.
    """
    if path:
        old_cwd = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old_cwd)
    else:
        yield


@contextmanager
def console_width(width: int | None = None) -> Iterator[None]:
    """Set the console width for the duration of the context.

    The console width is set using the `COLUMNS` environment variable.

    Parameters:
        width: The width to set the console to.
    """
    if width:
        old_width = os.environ.get("COLUMNS", None)
        os.environ["COLUMNS"] = str(width)
        try:
            yield
        finally:
            if old_width is None:
                del os.environ["COLUMNS"]
            else:
                os.environ["COLUMNS"] = old_width
    else:
        yield


class ExecutionError(Exception):
    """Exception raised for errors during execution of a code block.

    Attributes:
        message: The exception message.
        returncode: The code returned by the execution of the code block.
    """

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
        """The code returned by the execution of the code block."""


class ExecutionTimeout(ExecutionError):  # noqa: N818
    """Exception raised when the execution of a code block times out."""


def _raise_timeout(signum: int, frame: Any) -> None:  # noqa: ARG001
    raise ExecutionTimeout("Execution timed out")


@contextmanager
def _time_limit(seconds: float | None = None) -> Iterator[None]:
    """Raise an ExecutionTimeout if the wrapped code runs for too long.

    Relies on SIGALRM, therefore only works on POSIX systems
    and in the main thread. Otherwise it is a no-op.

    Parameters:
        seconds: The maximum number of seconds to wait.
    """
    if not seconds or not hasattr(signal, "SIGALRM"):
        yield
        return
    try:
        previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    except ValueError:
        # Not in the main thread: cannot enforce the timeout.
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _format_log_details(details: str, *, strip_fences: bool = False) -> str:
    if strip_fences:
        lines = details.split("\n")
        if lines[0].startswith("```") and lines[-1].startswith("```"):
            details = "\n".join(lines[1:-1])
    return indent(details, " " * 2)


def base_format(
    *,
    language: str,
    run: Callable,
    code: str,
    md: Markdown,
    html: bool = False,
    source: str = "",
    result: str = "",
    tabs: tuple[str, str] = default_tabs,
    id: str = "",  # noqa: A002
    id_prefix: str | None = None,
    returncode: int = 0,
    transform_source: Callable[[str], tuple[str, str]] | None = None,
    session: str | None = None,
    update_toc: bool = True,
    workdir: str | None = None,
    width: int | None = None,
    timeout: float | None = None,
    **options: Any,
) -> Markup:
    """Execute code and return HTML.

    Parameters:
        language: The code language.
        run: Function that runs code and returns output.
        code: The code to execute.
        md: The Markdown instance.
        html: Whether to inject output as HTML directly, without rendering.
        source: Whether to show source as well, and where.
        result: If provided, use as language to format result in a code block.
        tabs: Titles of tabs (if used).
        id: An optional ID for the code block (useful when warning about errors).
        id_prefix: A string used to prefix HTML ids in the generated HTML.
        returncode: The expected exit code.
        transform_source: An optional callable that returns transformed versions of the source.
            The input source is the one that is ran, the output source is the one that is
            rendered (when the source option is enabled).
        session: A session name, to persist state between executed code blocks.
        update_toc: Whether to include generated headings
            into the Markdown table of contents (toc extension).
        workdir: The working directory to use for the execution.
        timeout: Maximum execution time in seconds (0 or None means no limit).
        **options: Additional options passed from the formatter.

    Returns:
        HTML contents.
    """
    markdown = MarkdownConverter(md, update_toc=update_toc)
    extra = options.get("extra", {})
    use_cache = str(extra.pop("cache", "no")).lower() not in {"", "no", "off", "false", "0"}

    if transform_source:
        source_input, source_output = transform_source(code)
    else:
        source_input = code
        source_output = code

    manifest_options = {
        "html": html,
        "source": source,
        "result": result,
        "returncode": returncode,
        "session": session or None,
        "update_toc": update_toc,
        "tabs": list(tabs),
        "workdir": workdir,
        "width": width,
        "timeout": timeout,
        "cache": use_cache,
        "extra": dict(extra),
    }
    cache_key = None
    if use_cache and not session:
        cache_key = (
            language,
            source_input,
            workdir,
            width,
            returncode,
            html,
            source,
            result,
            tuple(sorted(extra.items())),
        )

    with manifest_recorder.block(
        language=language,
        code=source_input,
        options=manifest_options,
        explicit_id=id,
        session=session,
        workdir=workdir,
    ) as block:
        output = manifest_recorder.cache_lookup(cache_key) if cache_key is not None else None
        if output is not None:
            block.set_status("cached", returncode=returncode)
        else:
            try:
                with working_directory(workdir), console_width(width):
                    output = run(source_input, returncode=returncode, session=session, id=id, timeout=timeout, **extra)
            except ExecutionError as error:
                status = "timeout" if isinstance(error, ExecutionTimeout) else "error"
                block.set_status(status, returncode=error.returncode)
                block.set_output(str(error))
                identifier = id or extra.get("title", "")
                identifier = identifier and f"'{identifier}' "
                exit_message = "errors" if error.returncode is None else f"unexpected code {error.returncode}"
                log_message = (
                    f"Execution of {language} code block {identifier}exited with {exit_message}\n\n"
                    f"Code block is:\n\n{_format_log_details(source_input)}\n\n"
                    f"Output is:\n\n{_format_log_details(str(error), strip_fences=True)}\n"
                )
                _logger.warning(log_message)
                return markdown.convert(str(error))
            if cache_key is not None:
                manifest_recorder.cache_store(cache_key, output)
            block.set_status("ok", returncode=returncode)
        block.set_output(output)

        if not output and not source:
            return Markup()

        try:
            if html:
                if source:
                    placeholder = f'<div class="{uuid4()}"></div>'
                    wrapped_output = add_source(
                        source=source_output,
                        location=source,
                        output=placeholder,
                        language=language,
                        tabs=tabs,
                        **extra,
                    )
                    block.set_markdown(wrapped_output)
                    return markdown.convert(wrapped_output, stash={placeholder: output})
                block.set_markdown(output)
                return Markup(output)  # noqa: S704

            wrapped_output = output
            if result and source != "console":
                wrapped_output = code_block(result, output)
            if source:
                wrapped_output = add_source(
                    source=source_output,
                    location=source,
                    output=wrapped_output,
                    language=language,
                    tabs=tabs,
                    result=result,
                    **extra,
                )
            prefix = id_prefix if id_prefix is not None else (f"{id}-" if id else None)
            block.set_markdown(wrapped_output)
            return markdown.convert(wrapped_output, id_prefix=prefix)
        except Exception:
            block.set_status("render_error")
            raise
