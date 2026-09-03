"""Local embedding model wrapper (sentence-transformers, no API key needed).

The model is loaded lazily and cached at module level so it's only loaded
once per process, even if embed() is called many times.
"""

import os

# The model is already downloaded and cached locally after the first run, so
# skip the Hugging Face Hub version-check request entirely - this also
# silences the "unauthenticated requests" warning and the weight-loading
# progress bar, which have nothing useful to say on every subsequent run.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import numpy as np

MODEL_NAME = "all-MiniLM-L6-v2"

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed(texts: list[str]) -> np.ndarray:
    """Embed a list of texts into normalized vectors (unit length), so cosine
    similarity reduces to a plain dot product at retrieval time."""
    model = _get_model()
    return model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=len(texts) > 50,
    )


if __name__ == "__main__":
    vectors = embed(["total sales for Cereals in April", "top dairy brand in May"])
    print(f"Embedded {len(vectors)} texts, dimension {vectors.shape[1]}")
