"""fastembed with BAAI/bge-small-en-v1.5: ONNX on CPU, runs in the app container, no API call."""

from pathlib import Path

from pkos.embed.base import DIM

MODEL = "BAAI/bge-small-en-v1.5"


class FastEmbedder:
    name = MODEL

    def __init__(self, cache_dir: Path):
        import warnings

        from fastembed import TextEmbedding  # heavy import: only when actually embedding

        cache_dir.mkdir(parents=True, exist_ok=True)
        with warnings.catch_warnings():  # fastembed fights HF_HUB_DISABLE_PROGRESS_BARS
            warnings.filterwarnings("ignore", message="Cannot enable progress bars")
            self._model = TextEmbedding(MODEL, cache_dir=str(cache_dir))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = [v.tolist() for v in self._model.embed(texts)]
        assert all(len(v) == DIM for v in vecs)
        return vecs

    def embed_query(self, text: str) -> list[float]:
        # bge expects a query instruction prefix; query_embed applies it.
        return next(iter(self._model.query_embed([text]))).tolist()
