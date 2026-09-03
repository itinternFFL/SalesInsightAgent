"""Build/cache the embedded chunk index and run hybrid top-k retrieval."""

import re
from pathlib import Path

import numpy as np
import pandas as pd
from rank_bm25 import BM25Okapi

from src.embeddings import embed
from src.ingest import MASTER_PARQUET, load_all
from src.summarize import build_chunks

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CHUNKS_PARQUET = CACHE_DIR / "chunks.parquet"

RRF_K = 60  # standard Reciprocal Rank Fusion damping constant

# This dataset's chunks are heavily templated ("...across N invoices and M
# distinct customers...") so BM25 can't reliably tell chunk types apart on
# vocabulary alone - it was displacing the genuinely relevant chunk in
# testing. Dense embeddings were consistently correct on their own, so BM25
# is weighted low: it can only break near-ties in the dense ranking, not
# override a strong semantic match.
DENSE_WEIGHT = 0.9
BM25_WEIGHT = 0.1

# A handful of chunk types state a single definitive, whole-period answer
# (a category's overall top brand/customer/material, its highest/lowest
# month, the dataset grand total) - there are only ~9 of these total. They
# were getting crowded out of the top-k purely by volume once per-entity
# chunk types (44 brand_trend + 40 customer_trend = 84 chunks) were added,
# even when they scored well individually, since so many similarly-phrased
# per-entity chunks now compete for the same "category + timeframe" queries.
# A modest boost keeps these findable without needing a much larger k (which
# testing showed hurts the model's accuracy more than it helps recall).
#
# The boost is applied ONLY when the query doesn't name a specific month -
# applying it unconditionally was tested and found to crowd out the correct
# per-month chunk for month-specific questions (a whole-period chunk isn't
# more relevant just because it's "authoritative"; it's simply the wrong
# answer to "top customers in March" specifically).
AUTHORITATIVE_CHUNK_TYPES = {
    "category_trend",
    "category_brand_totals",
    "category_customer_totals",
    "category_material_totals",
    "category_channel_totals",
    "sale_type_totals",
    "dataset_totals",
}
AUTHORITATIVE_BOOST = 1.5

MONTH_TOKENS = {
    "january", "february", "march", "april", "may", "june",
    "jan", "feb", "mar", "apr", "jun",
}


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _mentions_specific_month(query: str) -> bool:
    return bool(set(_tokenize(query)) & MONTH_TOKENS)


def _mentioned_category(query: str, categories: list[str]) -> str | None:
    """If the query names exactly one of the dataset's categories (e.g.
    'Dairy'), return it - used to filter out wrong-category chunks before
    they're even scored. Testing showed prompt instructions alone weren't
    reliable at stopping the model from substituting a same-entity,
    wrong-category chunk (e.g. a customer's Cereals data for a Dairy
    question) when no correct chunk existed - removing the wrong-category
    chunks from the candidate pool entirely is the reliable fix."""
    q_lower = query.lower()
    mentioned = [c for c in categories if c.lower() in q_lower]
    return mentioned[0] if len(mentioned) == 1 else None


def _cache_is_fresh() -> bool:
    if not CHUNKS_PARQUET.exists() or not MASTER_PARQUET.exists():
        return False
    return CHUNKS_PARQUET.stat().st_mtime >= MASTER_PARQUET.stat().st_mtime


def get_index(use_cache: bool = True, verbose: bool = True) -> pd.DataFrame:
    """Return the chunk index (chunk_id, text, metadata columns, embedding),
    building and caching it if needed."""
    if use_cache and _cache_is_fresh():
        return pd.read_parquet(CHUNKS_PARQUET)

    if verbose:
        print("Building RAG index (first run, or data/ changed)...")

    data = load_all(use_cache=use_cache)
    chunks_df = build_chunks(data)

    if verbose:
        print(f"  {len(chunks_df)} summary chunks, embedding with sentence-transformers...")
    vectors = embed(chunks_df["text"].tolist())
    chunks_df["embedding"] = list(vectors)

    CACHE_DIR.mkdir(exist_ok=True)
    chunks_df.to_parquet(CHUNKS_PARQUET, index=False)
    if verbose:
        print("  Index built and cached.")
    return chunks_df


def retrieve(query: str, index_df: pd.DataFrame, k: int = 8) -> pd.DataFrame:
    """Hybrid retrieval: combines dense embedding similarity (semantic - good
    at "which brand did well", paraphrases, concepts) with BM25 lexical
    matching (good at exact terms - brand names, months, numbers that a pure
    embedding search can underrank), merged by Reciprocal Rank Fusion so
    neither method's raw scores need calibrating against the other."""
    if "category" in index_df.columns:
        categories = index_df["category"].dropna().unique().tolist()
        mentioned = _mentioned_category(query, categories)
    else:
        mentioned = None

    if mentioned:
        keep = index_df["category"].isna() | (index_df["category"] == mentioned)
        working_df = index_df[keep].reset_index(drop=True)
    else:
        working_df = index_df

    query_vec = embed([query])[0]
    matrix = np.stack(working_df["embedding"].to_numpy())
    dense_scores = matrix @ query_vec

    corpus_tokens = [_tokenize(t) for t in working_df["text"]]
    bm25 = BM25Okapi(corpus_tokens)
    bm25_scores = np.array(bm25.get_scores(_tokenize(query)))

    dense_rank = np.argsort(np.argsort(-dense_scores))  # 0 = best match
    bm25_rank = np.argsort(np.argsort(-bm25_scores))

    fused_score = (
        DENSE_WEIGHT / (RRF_K + dense_rank + 1)
        + BM25_WEIGHT / (RRF_K + bm25_rank + 1)
    )

    if not _mentions_specific_month(query):
        is_authoritative = working_df["chunk_type"].isin(AUTHORITATIVE_CHUNK_TYPES).to_numpy()
        fused_score = np.where(is_authoritative, fused_score * AUTHORITATIVE_BOOST, fused_score)

    top_k = np.argsort(-fused_score)[:k]
    result = working_df.iloc[top_k].copy()
    result["score"] = fused_score[top_k]
    result["dense_score"] = dense_scores[top_k]
    result["bm25_score"] = bm25_scores[top_k]
    return result


if __name__ == "__main__":
    idx = get_index()
    results = retrieve("How did the Coated brand perform over time?", idx, k=5)
    for _, row in results.iterrows():
        print(f"[{row['score']:.3f}] {row['text']}")
