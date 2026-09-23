# Execution manifest recording.
#
# This module implements an opt-in recorder that collects, for every code block
# Markdown Exec executes (or skips), the information needed to audit a build:
# which document the block comes from, a stable block identifier, a digest of
# the source code, the parsed options, the runner version, the whitelisted
# environment variables, the exit status, digests of the captured output and
# of the generated Markdown.
#
# The manifest is serialized to deterministic JSON (sorted keys, no timestamps)
# and written atomically: the payload is first written to a temporary file and
# then moved over the target, so a failed build never publishes half a manifest.

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager, suppress
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_PREVIEW_LIMIT = 200
"""Maximum number of characters kept in recorded previews."""

MANIFEST_VERSION = 1
"""Version of the manifest schema."""


class ManifestError(Exception):
    """Error raised when the execution manifest cannot be built consistently."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf8")).hexdigest()


def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_LIMIT:
        return text
    return f"{text[:_PREVIEW_LIMIT]}... [truncated, {len(text)} characters total]"


def _stream_info(text: str | None) -> dict[str, Any]:
    if text is None:
        return {"sha256": None, "bytes": None, "preview": None}
    return {
        "sha256": _digest(text),
        "bytes": len(text.encode("utf8")),
        "preview": _preview(text),
    }


def _runner_info() -> dict[str, str]:
    try:
        version = metadata.version("markdown-exec")
    except metadata.PackageNotFoundError:
        version = "0.0.0"
    return {
        "name": "markdown-exec",
        "version": version,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
    }


class _BlockHandle:
    """Handle given to formatters to report the outcome of a single block."""

    def __init__(self, record: dict[str, Any] | None) -> None:
        self._record = record

    @property
    def block_id(self) -> str | None:
        """The stable identifier assigned to this block."""
        return self._record["block_id"] if self._record is not None else None

    def set_status(self, status: str, returncode: int | None = None) -> None:
        """Set the final status of the block."""
        if self._record is not None:
            self._record["status"] = status
            self._record["returncode"] = returncode

    def set_output(self, stdout: str | None, stderr: str | None = None) -> None:
        """Record digests of the captured output streams."""
        if self._record is not None:
            self._record["stdout"] = _stream_info(stdout)
            self._record["stderr"] = _stream_info(stderr)

    def set_markdown(self, markdown: str) -> None:
        """Record the digest of the generated Markdown fed back to the renderer."""
        if self._record is not None:
            self._record["markdown_sha256"] = _digest(markdown)


class ExecutionManifest:
    """Recorder for code block executions.

    A single instance is shared through the module-level `manifest_recorder`.
    The recorder is disabled by default and enabled through `configure`.
    """

    def __init__(self) -> None:
        self._path: str | None = None
        self._env_whitelist: tuple[str, ...] = ()
        self._reset_state()

    def _reset_state(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._document = ""
        self._explicit_ids: set[tuple[str, str]] = set()
        self._auto_ids: dict[tuple[str, str, str], int] = {}
        self._sessions: dict[str, list[str]] = {}
        self._cache: dict[tuple, str] = {}
        self._errors: list[str] = []
        self._paused = 0

    @property
    def enabled(self) -> bool:
        """Whether the recorder is enabled."""
        return self._path is not None

    @property
    def records(self) -> list[dict[str, Any]]:
        """The recorded blocks, in execution order."""
        return self._records

    def configure(self, path: str, env_whitelist: tuple[str, ...] | list[str] = ()) -> None:
        """Enable recording.

        Parameters:
            path: Where the manifest will be written.
            env_whitelist: Names of environment variables that are safe to record.
                Nothing else is ever recorded, so secrets stay out of the manifest.
        """
        self._path = path
        self._env_whitelist = tuple(env_whitelist)
        self._reset_state()

    def reset(self) -> None:
        """Disable recording and drop all recorded state."""
        self._path = None
        self._env_whitelist = ()
        self._reset_state()

    def set_document(self, document: str) -> None:
        """Set the path of the document currently being rendered."""
        self._document = document

    @property
    def errors(self) -> list[str]:
        """Errors collected while recording (e.g. duplicate explicit ids)."""
        return self._errors

    @contextmanager
    def pause(self) -> Iterator[None]:
        """Temporarily pause recording (used while re-rendering generated Markdown)."""
        self._paused += 1
        try:
            yield
        finally:
            self._paused -= 1

    def cache_lookup(self, key: tuple) -> str | None:
        """Return the cached output for a block, if any."""
        return self._cache.get(key)

    def cache_store(self, key: tuple, output: str) -> None:
        """Store the output of a block in the cache."""
        self._cache[key] = output

    def _block_id(self, explicit_id: str, language: str, code_sha256: str | None) -> str:
        if explicit_id:
            key = (self._document, explicit_id)
            if key in self._explicit_ids:
                self._errors.append(
                    f"duplicate explicit id {explicit_id!r} for code blocks in document {self._document!r}",
                )
            else:
                self._explicit_ids.add(key)
            return explicit_id
        # Content-based identifier: stable when unrelated paragraphs are reordered.
        base = f"{language}-{(code_sha256 or 'nocode')[:12]}"
        key = (self._document, language, code_sha256 or "")
        occurrence = self._auto_ids.get(key, 0) + 1
        self._auto_ids[key] = occurrence
        return base if occurrence == 1 else f"{base}-{occurrence}"

    def _environment(self) -> dict[str, str]:
        return {name: os.environ[name] for name in self._env_whitelist if name in os.environ}

    def _session_info(self, session: str | None, block_id: str) -> tuple[int | None, str | None]:
        if not session:
            return None, None
        history = self._sessions.setdefault(session, [])
        index = len(history) + 1
        parent = history[-1] if history else None
        history.append(block_id)
        return index, parent

    @contextmanager
    def block(
        self,
        *,
        language: str,
        code: str,
        options: dict[str, Any],
        explicit_id: str = "",
        session: str | None = None,
        workdir: str | None = None,
    ) -> Iterator[_BlockHandle]:
        """Record the execution of a single code block.

        Parameters:
            language: The code language.
            code: The source code being executed.
            options: The parsed options of the block.
            explicit_id: An optional explicit identifier.
            session: An optional session name.
            workdir: The working directory used for the execution.

        Yields:
            A handle to report the block outcome.
        """
        if not self.enabled or self._paused:
            yield _BlockHandle(None)
            return
        code_sha256 = _digest(code)
        block_id = self._block_id(explicit_id, language, code_sha256)
        session_index, session_parent = self._session_info(session, block_id)
        record: dict[str, Any] = {
            "document": self._document,
            "block_id": block_id,
            "language": language,
            "status": "ok",
            "runner": _runner_info(),
            "code_sha256": code_sha256,
            "code_preview": _preview(code),
            "options": options,
            "env": self._environment(),
            "workdir": workdir,
            "session": session or None,
            "session_index": session_index,
            "session_parent": session_parent,
            "returncode": None,
            "stdout": _stream_info(None),
            "stderr": _stream_info(None),
            "markdown_sha256": None,
        }
        self._records.append(record)
        yield _BlockHandle(record)

    def record_skipped(self, *, language: str, options: dict[str, Any]) -> None:
        """Record a code block that was not executed.

        Parameters:
            language: The code language.
            options: The raw options of the block.
        """
        if not self.enabled or self._paused:
            return
        explicit_id = str(options.get("id", "") or "")
        block_id = self._block_id(explicit_id, language, None)
        self._records.append(
            {
                "document": self._document,
                "block_id": block_id,
                "language": language,
                "status": "skipped",
                "runner": _runner_info(),
                "code_sha256": None,
                "code_preview": None,
                "options": dict(options),
                "env": self._environment(),
                "workdir": None,
                "session": options.get("session") or None,
                "session_index": None,
                "session_parent": None,
                "returncode": None,
                "stdout": _stream_info(None),
                "stderr": _stream_info(None),
                "markdown_sha256": None,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the manifest as a dictionary."""
        return {
            "version": MANIFEST_VERSION,
            "runner": _runner_info(),
            "env_whitelist": list(self._env_whitelist),
            "blocks": self._records,
        }

    def dumps(self) -> str:
        """Return the manifest as deterministic JSON."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, path: str | os.PathLike | None = None) -> Path:
        """Write the manifest to disk, atomically.

        The payload is first written to a temporary file in the same directory,
        then moved over the target, so a failed or interrupted build never
        publishes a partial manifest.

        Parameters:
            path: Where to write the manifest. Defaults to the configured path.

        Returns:
            The path the manifest was written to.

        Raises:
            ManifestError: If errors were collected while recording,
                or if no path was configured.
        """
        if self._errors:
            raise ManifestError("; ".join(self._errors))
        target = Path(path or self._path or "")
        if not str(target):
            raise ManifestError("no path configured for the execution manifest")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.dumps().encode("utf8")
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp_file:
                tmp_file.write(payload)
            Path(tmp_name).replace(target)
        except BaseException:
            with suppress(OSError):
                Path(tmp_name).unlink()
            raise
        return target


manifest_recorder = ExecutionManifest()
"""Shared execution manifest recorder."""
