"""Tests for DocumentStore."""

from clangd_lsp_proxy.document_store import DocumentStore


def _open_params(uri: str, text: str = "hello", version: int = 1) -> dict:
    return {
        "textDocument": {
            "uri": uri,
            "languageId": "cpp",
            "version": version,
            "text": text,
        }
    }


def _change_params(uri: str, text: str, version: int) -> dict:
    return {
        "textDocument": {"uri": uri, "version": version},
        "contentChanges": [{"text": text}],
    }


def test_open_adds_document() -> None:
    store = DocumentStore()
    store.open(_open_params("file:///a.cpp", "int main() {}"))
    docs = store.all_open()
    assert len(docs) == 1
    doc = docs[0]
    assert doc.uri == "file:///a.cpp"
    assert doc.language_id == "cpp"
    assert doc.version == 1
    assert doc.content == "int main() {}"


def test_change_updates_version_and_content() -> None:
    store = DocumentStore()
    store.open(_open_params("file:///a.cpp"))
    store.change(_change_params("file:///a.cpp", "new content", 2))
    doc = store.all_open()[0]
    assert doc.version == 2
    assert doc.content == "new content"


def test_close_removes_document() -> None:
    store = DocumentStore()
    store.open(_open_params("file:///a.cpp"))
    store.close("file:///a.cpp")
    assert store.all_open() == []


def test_all_open_after_open_close_returns_empty() -> None:
    store = DocumentStore()
    store.open(_open_params("file:///a.cpp"))
    store.open(_open_params("file:///b.cpp"))
    store.close("file:///a.cpp")
    store.close("file:///b.cpp")
    assert store.all_open() == []


def test_change_unknown_uri_is_noop() -> None:
    store = DocumentStore()
    store.change(_change_params("file:///nonexistent.cpp", "x", 5))
    assert store.all_open() == []


def test_all_open_multiple_documents() -> None:
    store = DocumentStore()
    uris = ["file:///a.cpp", "file:///b.cpp", "file:///c.cpp"]
    for uri in uris:
        store.open(_open_params(uri))
    result_uris = {d.uri for d in store.all_open()}
    assert result_uris == set(uris)


def test_change_with_range_does_not_update_content() -> None:
    store = DocumentStore()
    store.open(_open_params("file:///a.cpp", "original"))
    # Incremental change (has "range") — store ignores it (Full sync only).
    store.change({
        "textDocument": {"uri": "file:///a.cpp", "version": 2},
        "contentChanges": [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}, "text": "NEW"}],
    })
    doc = store.all_open()[0]
    assert doc.content == "original"
