"""Open document state tracking for backend replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class OpenDocument:
    uri: str
    language_id: str
    version: int
    content: str


class DocumentStore:
    """Tracks the current state of every open document.

    The proxy forces Full sync (textDocumentSync=1), so every didChange
    carries the complete document text and change() simply replaces the
    stored content.
    """

    def __init__(self) -> None:
        self._docs: dict[str, OpenDocument] = {}

    def open(self, params: dict[str, Any]) -> None:
        text_doc = params["textDocument"]
        self._docs[text_doc["uri"]] = OpenDocument(
            uri=text_doc["uri"],
            language_id=text_doc["languageId"],
            version=text_doc["version"],
            content=text_doc["text"],
        )

    def change(self, params: dict[str, Any]) -> None:
        uri = params["textDocument"]["uri"]
        if uri not in self._docs:
            return
        doc = self._docs[uri]
        doc.version = params["textDocument"]["version"]
        changes: list[dict[str, Any]] = params["contentChanges"]
        if changes and "range" not in changes[0]:
            # Full sync: single entry with the complete new text.
            doc.content = changes[0]["text"]

    def close(self, uri: str) -> None:
        self._docs.pop(uri, None)

    def all_open(self) -> list[OpenDocument]:
        return list(self._docs.values())
