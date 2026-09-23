"""Tests for the execution manifest."""

from __future__ import annotations

import hashlib
import json
import re
from importlib import metadata
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
from mkdocs.commands.build import build
from mkdocs.config import load_config

from markdown_exec import ExecutionManifest, ManifestError, manifest_recorder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from markdown import Markdown


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf8")).hexdigest()


@pytest.fixture(name="recorder")
def _fixture_recorder(tmp_path: Path) -> Iterator[ExecutionManifest]:
    """Enable the manifest recorder for the duration of a test."""
    manifest_recorder.configure(
        str(tmp_path / "execution-manifest.json"),
        env_whitelist=["MARKDOWN_EXEC_TEST_ENV"],
    )
    yield manifest_recorder
    manifest_recorder.reset()


def test_records_standalone_block(md: Markdown, recorder: ExecutionManifest) -> None:
    """A standalone block is recorded with digests locating it back to the source."""
    code = 'print("hello")'
    md.convert(f'```python exec="yes"\n{code}\n```')
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record["status"] == "ok"
    assert record["language"] == "python"
    assert record["block_id"]
    assert record["code_sha256"] == _sha256(code)
    assert record["stdout"]["sha256"] == _sha256("hello\n")
    assert record["returncode"] == 0
    assert record["markdown_sha256"]
    assert record["options"]["workdir"] is None
    assert record["runner"]["version"] == metadata.version("markdown-exec")
    manifest = json.loads(recorder.dumps())
    assert manifest["runner"]["version"] == metadata.version("markdown-exec")
    assert manifest["version"] == 1


def test_block_ids_stable_across_paragraph_reorder(md: Markdown, recorder: ExecutionManifest, tmp_path: Path) -> None:
    """Reordering unrelated paragraphs does not change generated block ids."""

    def doc(paragraph_a: str, paragraph_b: str) -> str:
        return dedent(
            f"""
            {paragraph_a}

            ```python exec="yes"
            print("first")
            ```

            {paragraph_b}

            ```python exec="yes"
            print("second")
            ```
            """,
        )

    md.convert(doc("Alpha paragraph.", "Beta paragraph."))
    ids_before = [record["block_id"] for record in recorder.records]
    recorder.configure(str(tmp_path / "second.json"), env_whitelist=[])
    md.reset()
    md.convert(doc("Beta paragraph.", "Alpha paragraph."))
    ids_after = [record["block_id"] for record in recorder.records]
    assert ids_before == ids_after


def test_explicit_id_conflict_raises(md: Markdown, recorder: ExecutionManifest) -> None:
    """Two blocks sharing an explicit id in the same document are an error."""
    md.convert(
        dedent(
            """
            ```python exec="yes" id="dup"
            print(1)
            ```

            ```python exec="yes" id="dup"
            print(2)
            ```
            """,
        ),
    )
    assert any("duplicate explicit id" in error for error in recorder.errors)
    with pytest.raises(ManifestError, match="duplicate explicit id"):
        recorder.write()


def test_shared_session_records_parent_and_index(md: Markdown, recorder: ExecutionManifest) -> None:
    """Session blocks record their sequence number and parent state."""
    md.convert(
        dedent(
            """
            ```python exec="yes" session="manifest-session"
            value = 40
            ```

            ```python exec="yes" session="manifest-session"
            print(value + 2)
            ```
            """,
        ),
    )
    first, second = recorder.records
    assert first["session"] == "manifest-session"
    assert first["session_index"] == 1
    assert first["session_parent"] is None
    assert second["session_index"] == 2
    assert second["session_parent"] == first["block_id"]


def test_skipped_block_recorded(md: Markdown, recorder: ExecutionManifest) -> None:
    """Blocks without exec are recorded as skipped."""
    md.convert('```python\nprint("not executed")\n```')
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record["status"] == "skipped"
    assert record["language"] == "python"


def test_cached_block(md: Markdown, recorder: ExecutionManifest) -> None:
    """Identical blocks with cache reuse the first output and are marked cached."""
    html = md.convert(
        dedent(
            """
            ```python exec="yes" cache="yes"
            print("cached-output")
            ```

            ```python exec="yes" cache="yes"
            print("cached-output")
            ```
            """,
        ),
    )
    assert [record["status"] for record in recorder.records] == ["ok", "cached"]
    first, second = recorder.records
    assert first["stdout"] == second["stdout"]
    assert html.count("cached-output") == 2


def test_timeout_shell(md: Markdown, recorder: ExecutionManifest) -> None:
    """Shell blocks exceeding their timeout are recorded as timeout."""
    md.convert('```sh exec="yes" timeout="0.2"\nsleep 5\n```')
    assert recorder.records[0]["status"] == "timeout"
    assert recorder.records[0]["returncode"] is None


def test_timeout_python(md: Markdown, recorder: ExecutionManifest) -> None:
    """Python blocks exceeding their timeout are recorded as timeout."""
    md.convert('```python exec="yes" timeout="0.2"\nwhile True: pass\n```')
    assert recorder.records[0]["status"] == "timeout"


def test_error_status(md: Markdown, recorder: ExecutionManifest) -> None:
    """Failed executions record the error status and exit code."""
    md.convert('```python exec="yes"\nraise ValueError("boom")\n```')
    md.convert('```sh exec="yes"\nexit 3\n```')
    python_record, sh_record = recorder.records
    assert python_record["status"] == "error"
    assert python_record["returncode"] is None
    assert sh_record["status"] == "error"
    assert sh_record["returncode"] == 3


def test_render_error_status(md: Markdown, recorder: ExecutionManifest) -> None:
    """Failures while rendering the output are a distinct status."""
    md.convert('```python exec="yes" source="nope"\nprint(1)\n```')
    assert recorder.records[0]["status"] == "render_error"


def test_env_whitelist_excludes_secrets(md: Markdown, recorder: ExecutionManifest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only whitelisted environment variables are recorded."""
    monkeypatch.setenv("MARKDOWN_EXEC_TEST_ENV", "visible")
    monkeypatch.setenv("MARKDOWN_EXEC_SECRET", "topsecret")
    md.convert('```python exec="yes"\nprint("env")\n```')
    record = recorder.records[0]
    assert record["env"] == {"MARKDOWN_EXEC_TEST_ENV": "visible"}
    payload = recorder.dumps()
    assert "topsecret" not in payload
    assert "MARKDOWN_EXEC_SECRET" not in payload


def test_truncated_output(md: Markdown, recorder: ExecutionManifest) -> None:
    """Long outputs are digested and truncated, never recorded in full."""
    md.convert('```python exec="yes"\nprint("x" * 5000)\n```')
    record = recorder.records[0]
    assert record["stdout"]["bytes"] == 5001
    assert record["stdout"]["sha256"] == _sha256("x" * 5000 + "\n")
    assert len(record["stdout"]["preview"]) < 300
    assert "x" * 5000 not in recorder.dumps()


def test_atomic_write_leaves_no_temp_files(md: Markdown, recorder: ExecutionManifest, tmp_path: Path) -> None:
    """The manifest is written atomically and parses as JSON."""
    md.convert('```python exec="yes"\nprint("atomic")\n```')
    target = recorder.write()
    assert json.loads(target.read_text(encoding="utf8"))["blocks"]
    assert not list(tmp_path.glob("*.tmp"))


def test_manifest_is_deterministic(md: Markdown, recorder: ExecutionManifest, tmp_path: Path) -> None:
    """Two identical conversions produce byte-identical manifests."""
    doc = '```python exec="yes" session="det"\nvalue = 1\n```\n\n```python exec="yes" session="det"\nprint(value)\n```\n'
    md.convert(doc)
    first = recorder.dumps()
    recorder.configure(str(tmp_path / "second.json"), env_whitelist=["MARKDOWN_EXEC_TEST_ENV"])
    md.reset()
    md.convert(doc)
    second = recorder.dumps()
    assert first == second


# MkDocs integration tests.

_INDEX_MD = dedent(
    """
    # Home

    A paragraph.

    ```python exec="yes" id="standalone"
    print("standalone-output")
    ```

    ```python exec="yes" session="shared"
    value = 21
    ```

    ```python exec="yes" session="shared"
    print(value * 2)
    ```

    ```python
    print("not executed")
    ```

    ```python exec="yes"
    print("x" * 5000)
    ```

    ```python exec="yes"
    raise RuntimeError("boom")
    ```

    === "Tab"

        ```python exec="yes"
        print("tabbed-output")
        ```

    !!! note

        ```python exec="yes"
        print("admonition-output")
        ```

    --8<-- "snippet.md"
    """,
)

_SNIPPET_MD = dedent(
    """
    ```python exec="yes"
    print("included-output")
    ```
    """,
)


def _write_project(tmp_path: Path, *, manifest: bool = True, index: str = _INDEX_MD) -> Path:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "index.md").write_text(index, encoding="utf8")
    includes_dir = tmp_path / "includes"
    includes_dir.mkdir()
    (includes_dir / "snippet.md").write_text(_SNIPPET_MD, encoding="utf8")
    if manifest:
        plugin_config = (
            "  - markdown-exec:\n"
            "      manifest: execution-manifest.json\n"
            "      manifest_env:\n"
            "        - MARKDOWN_EXEC_TEST_ENV\n"
        )
    else:
        plugin_config = "  - markdown-exec\n"
    config_path = tmp_path / "mkdocs.yml"
    config_path.write_text(
        "site_name: Test\n"
        f"site_dir: {tmp_path / 'site'}\n"
        "markdown_extensions:\n"
        "  - admonition\n"
        "  - pymdownx.tabbed\n"
        "  - pymdownx.superfences\n"
        "  - pymdownx.snippets:\n"
        f"      base_path: {includes_dir}\n"
        "plugins:\n"
        f"{plugin_config}",
        encoding="utf8",
    )
    return config_path


def _build(config_path: Path) -> None:
    build(load_config(str(config_path)))


def _fenced_codes(markdown_text: str) -> list[str]:
    return [dedent(code) for code in re.findall(r"```(?:python|sh|bash)[^\n]*\n(.*?)```", markdown_text, flags=re.DOTALL)]


def _page_content(site_dir: Path) -> bytes:
    """Read the built page, normalizing MkDocs' own build-date comment."""
    html = (site_dir / "index.html").read_text(encoding="utf8")
    return re.sub(r"Build Date UTC : [^\n]*", "Build Date UTC :", html).encode("utf8")


@pytest.fixture(name="built_project")
def _fixture_built_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a temporary MkDocs project with the manifest enabled."""
    monkeypatch.setenv("MARKDOWN_EXEC_TEST_ENV", "visible")
    monkeypatch.setenv("MARKDOWN_EXEC_SECRET", "topsecret")
    config_path = _write_project(tmp_path)
    _build(config_path)
    return tmp_path


def test_mkdocs_manifest_contents(built_project: Path) -> None:
    """The manifest records every block of the built site."""
    manifest = json.loads((built_project / "site" / "execution-manifest.json").read_text(encoding="utf8"))
    blocks = manifest["blocks"]
    by_id = {block["block_id"]: block for block in blocks}

    # Standalone block with explicit id.
    standalone = by_id["standalone"]
    assert standalone["document"] == "index.md"
    assert standalone["status"] == "ok"
    assert standalone["stdout"]["sha256"] == _sha256("standalone-output\n")
    assert standalone["env"] == {"MARKDOWN_EXEC_TEST_ENV": "visible"}

    # Shared session: sequence numbers and parent state.
    session_blocks = [block for block in blocks if block["session"] == "shared"]
    assert [block["session_index"] for block in session_blocks] == [1, 2]
    assert session_blocks[1]["session_parent"] == session_blocks[0]["block_id"]

    # Skipped, failed and truncated blocks are distinct statuses.
    statuses = [block["status"] for block in blocks]
    assert "skipped" in statuses
    assert "error" in statuses
    truncated = next(block for block in blocks if block["stdout"]["bytes"] == 5001)
    assert len(truncated["stdout"]["preview"]) < 300

    # Nested (tab, admonition) and included blocks are recorded under the page.
    outputs = {block["stdout"]["sha256"] for block in blocks if block["stdout"]["sha256"]}
    for expected in ("tabbed-output\n", "admonition-output\n", "included-output\n"):
        assert _sha256(expected) in outputs
    assert all(block["document"] == "index.md" for block in blocks)

    # Every executed block can be located back in the sources.
    sources = _fenced_codes(_INDEX_MD) + _fenced_codes(_SNIPPET_MD)
    source_digests = {_sha256(code.rstrip("\n")) for code in sources}
    for block in blocks:
        if block["code_sha256"] is not None:
            assert block["code_sha256"] in source_digests

    # Secrets never leak into the manifest.
    payload = json.dumps(manifest)
    assert "topsecret" not in payload
    assert "MARKDOWN_EXEC_SECRET" not in payload


def test_mkdocs_manifest_deterministic_and_pages_unchanged(built_project: Path, tmp_path: Path) -> None:
    """Rebuilding gives byte-identical manifests and pages; pages match a manifest-less build."""
    site_dir = built_project / "site"
    manifest_before = (site_dir / "execution-manifest.json").read_bytes()
    page_before = _page_content(site_dir)

    # Rebuild the very same project: manifest and pages must be byte-identical.
    _build(built_project / "mkdocs.yml")
    assert (site_dir / "execution-manifest.json").read_bytes() == manifest_before
    assert _page_content(site_dir) == page_before

    # Build the same sources without the manifest: pages must not change.
    other = tmp_path / "no-manifest"
    other.mkdir()
    config_path = _write_project(other, manifest=False)
    _build(config_path)
    assert _page_content(other / "site") == page_before
    assert not (other / "site" / "execution-manifest.json").exists()


def test_mkdocs_failed_build_publishes_no_manifest(tmp_path: Path) -> None:
    """A failing build never publishes a manifest, not even a partial one."""
    index = dedent(
        """
        # Home

        ```python exec="yes" id="dup"
        print(1)
        ```

        ```python exec="yes" id="dup"
        print(2)
        ```
        """,
    )
    config_path = _write_project(tmp_path, index=index)
    with pytest.raises(ManifestError, match="duplicate explicit id"):
        _build(config_path)
    site_dir = tmp_path / "site"
    assert not (site_dir / "execution-manifest.json").exists()
    if site_dir.exists():
        assert not list(site_dir.glob("**/*.tmp"))
