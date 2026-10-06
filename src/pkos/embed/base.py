from typing import Protocol

DIM = 384  # must match vector(384) in migrations/0003_chunks.sql


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...
