"""Turn the raw transaction-level sales DataFrame into a set of natural
language summary "chunks" for RAG retrieval.

Every number quoted in a chunk comes straight out of a pandas aggregation, so
the retrieval step hands the model pre-computed facts instead of asking it to
infer totals from a handful of raw rows.
"""

import pandas as pd


def _fmt_money(x: float) -> str:
    return f"Rs {x:,.0f}"


def _fmt_qty(x: float) -> str:
    return f"{x:,.0f}"


def _month_label(month: str) -> str:
    return pd.Period(month, freq="M").strftime("%B %Y")


def _rank_by_magnitude(sums: pd.Series, n: int | None = None) -> pd.Series:
    """Rank a Series of (all-negative) net sales by sales magnitude - largest
    absolute transaction volume first - rather than by raw signed value.
    Raw signed value would rank the *smallest* sellers first, since e.g.
    -601,329 > -1,044,044,000 numerically even though the latter reflects
    ~1700x more sales activity."""
    ranked = sums.reindex(sums.abs().sort_values(ascending=False).index)
    return ranked.head(n) if n is not None else ranked


def _brand_month_chunks(df: pd.DataFrame) -> list[dict]:
    chunks = []
    grouped = df.groupby(["month", "category", "brand"], dropna=True)
    for (month, category, brand), g in grouped:
        top_customer = (
            _rank_by_magnitude(g.groupby("customer_name")["net_sale"].sum(), 1).index[0]
            if not g.empty else None
        )
        text = (
            f"In {_month_label(month)}, the {category} brand '{brand}' had net "
            f"sales of {_fmt_money(g['net_sale'].sum())} and gross sales of "
            f"{_fmt_money(g['gross_sales'].sum())}, across "
            f"{g['invoice_no'].nunique()} invoices and "
            f"{g['customer_name'].nunique()} distinct customers. Total quantity "
            f"sold was {_fmt_qty(g['quantity'].sum())} units "
            f"({_fmt_qty(g['kgs_litres'].sum())} kgs/litres). "
            f"Top customer by net sales: {top_customer}."
        )
        chunks.append({
            "text": text,
            "month": month, "category": category, "brand": brand,
            "chunk_type": "brand_month",
        })
    return chunks


def _channel_month_chunks(df: pd.DataFrame) -> list[dict]:
    chunks = []
    grouped = df.groupby(["month", "category", "channel"], dropna=True)
    for (month, category, channel), g in grouped:
        top_brand = (
            _rank_by_magnitude(g.groupby("brand")["net_sale"].sum(), 1).index[0]
            if not g.empty else None
        )
        text = (
            f"In {_month_label(month)}, the '{channel}' channel for {category} "
            f"generated net sales of {_fmt_money(g['net_sale'].sum())} across "
            f"{g['invoice_no'].nunique()} invoices and "
            f"{g['customer_name'].nunique()} distinct customers. "
            f"The top-selling brand through this channel was '{top_brand}'."
        )
        chunks.append({
            "text": text,
            "month": month, "category": category, "channel": channel,
            "chunk_type": "channel_month",
        })
    return chunks


def _category_month_totals_chunks(df: pd.DataFrame) -> list[dict]:
    """Totals only (net/gross sales, invoice count, customer count, row
    count) - kept separate from the brand ranking below so a narrow question
    like "how many invoices" gets a tightly-focused chunk to match against,
    instead of one long chunk whose embedding is diluted by an unrelated
    brand list.

    Row count (line items) is a DIFFERENT number from invoice count - one
    invoice has many line items, one per material/SKU on it - so both are
    stated explicitly rather than assuming either implies the other."""
    chunks = []
    grouped = df.groupby(["month", "category"], dropna=True)
    for (month, category), g in grouped:
        text = (
            f"Overall, {category} sales in {_month_label(month)} totaled "
            f"{_fmt_money(g['net_sale'].sum())} in net sales and "
            f"{_fmt_money(g['gross_sales'].sum())} in gross sales, across "
            f"{g['invoice_no'].nunique()} unique invoices and "
            f"{g['customer_name'].nunique()} distinct customers. The sheet "
            f"has {len(g):,} line items (rows) for {category} in "
            f"{_month_label(month)} - not the same as the invoice count, "
            f"since one invoice can span multiple line items."
        )
        chunks.append({
            "text": text,
            "month": month, "category": category,
            "chunk_type": "category_month_totals",
        })
    return chunks


def _category_month_top_brands_chunks(df: pd.DataFrame) -> list[dict]:
    chunks = []
    grouped = df.groupby(["month", "category"], dropna=True)
    for (month, category), g in grouped:
        top_brands = _rank_by_magnitude(g.groupby("brand")["net_sale"].sum(), 5)
        top_brands_str = "; ".join(
            f"{b} ({_fmt_money(v)})" for b, v in top_brands.items()
        )
        text = (
            f"Top 5 {category} brands by sales magnitude in "
            f"{_month_label(month)}, largest first (figures are negative by "
            f"export convention - a larger magnitude means more sales "
            f"activity, regardless of sign): {top_brands_str}."
        )
        chunks.append({
            "text": text,
            "month": month, "category": category,
            "chunk_type": "category_month_top_brands",
        })
    return chunks


def _category_trend_chunks(df: pd.DataFrame) -> list[dict]:
    """One chunk per category giving the full month-by-month total, plus the
    highest/lowest month already picked out by pandas - so "which month was
    highest/lowest for Cereals" never requires the model to compare several
    separately-retrieved monthly chunks itself."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        by_month = g.groupby("month")["net_sale"].sum().sort_index()
        trend_str = "; ".join(
            f"{_month_label(m)}: {_fmt_money(v)}" for m, v in by_month.items()
        )
        highest_month = _rank_by_magnitude(by_month, 1).index[0]
        lowest_month = by_month.abs().idxmin()
        text = (
            f"Month-by-month total net sales for {category} across the "
            f"available data: {trend_str}. By sales magnitude, the HIGHEST "
            f"month for {category} was {_month_label(highest_month)} "
            f"({_fmt_money(by_month[highest_month])}) and the LOWEST month "
            f"was {_month_label(lowest_month)} ({_fmt_money(by_month[lowest_month])})."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "category_trend",
        })
    return chunks


def _brand_trend_chunks(df: pd.DataFrame) -> list[dict]:
    chunks = []
    grouped = df.groupby(["category", "brand"], dropna=True)
    for (category, brand), g in grouped:
        by_month = g.groupby("month")["net_sale"].sum().sort_index()
        trend_str = "; ".join(
            f"{_month_label(m)}: {_fmt_money(v)}" for m, v in by_month.items()
        )
        total = by_month.sum()
        text = (
            f"Month-by-month net sales trend for {category} brand '{brand}' "
            f"across the available data: {trend_str}. TOTAL net sales for "
            f"'{brand}' summed across all {len(by_month)} months: "
            f"{_fmt_money(total)}."
        )
        chunks.append({
            "text": text,
            "category": category, "brand": brand,
            "chunk_type": "brand_trend",
        })
    return chunks


def _customer_trend_chunks(df: pd.DataFrame, top_n: int = 20) -> list[dict]:
    """Month-by-month trend for the top N customers per category (by total
    magnitude) - the customer-level equivalent of brand_trend. Without this,
    a question about one specific customer's month-by-month pattern has no
    matching chunk, and retrieval falls back to whatever loosely-related
    chunk scores highest - confirmed by testing to sometimes be a
    completely different entity (a brand chunk mistaken for a customer)."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        totals = g.groupby("customer_name")["net_sale"].sum()
        top_customers = _rank_by_magnitude(totals, top_n).index
        for customer in top_customers:
            cg = g[g["customer_name"] == customer]
            by_month = cg.groupby("month")["net_sale"].sum().sort_index()
            trend_str = "; ".join(
                f"{_month_label(m)}: {_fmt_money(v)}" for m, v in by_month.items()
            )
            total = by_month.sum()
            avg = by_month.mean()
            text = (
                f"Month-by-month net sales trend for {category} customer "
                f"'{customer}' across the available data: {trend_str}. "
                f"TOTAL net sales for '{customer}' summed across all "
                f"{len(by_month)} months: {_fmt_money(total)}. AVERAGE net "
                f"sales per month across all {len(by_month)} months: "
                f"{_fmt_money(avg)}."
            )
            chunks.append({
                "text": text,
                "category": category, "customer_name": customer,
                "chunk_type": "customer_trend",
            })
    return chunks


def _category_channel_totals_chunks(df: pd.DataFrame) -> list[dict]:
    """One chunk per category ranking every channel by its TOTAL net sales
    summed across the whole period - not per-month. Same gap as brand,
    customer, and material totals: without this, "top channel overall" has
    no precomputed whole-period answer, and retrieval falls back to whatever
    other whole-period chunk it can find - confirmed by testing to sometimes
    be a completely different dimension (Sale Type mistaken for Channel)."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        n_months = g["month"].nunique()
        channel_totals = g.groupby("channel")["net_sale"].sum()
        ranked = _rank_by_magnitude(channel_totals)
        ranked_str = "; ".join(f"{c} ({_fmt_money(v)})" for c, v in ranked.items())
        text = (
            f"TOTAL net sales per {category} channel, summed across all "
            f"{n_months} months of available data (not per-month), ranked "
            f"by sales magnitude - largest first: {ranked_str}. The "
            f"top-selling {category} channel OVERALL across the whole "
            f"period was '{ranked.index[0]}' with total net sales of "
            f"{_fmt_money(ranked.iloc[0])}."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "category_channel_totals",
        })
    return chunks


def _category_brand_totals_chunks(df: pd.DataFrame) -> list[dict]:
    """One chunk per category ranking every brand by its TOTAL net sales
    summed across the whole period - not per-month. Without this, a question
    like "top-selling brand overall (all N months)" has no precomputed sum to
    point to, and the model either has to add up several months itself
    (unreliable) or mistakes a single month's figure for the total."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        n_months = g["month"].nunique()
        brand_totals = g.groupby("brand")["net_sale"].sum()
        ranked = _rank_by_magnitude(brand_totals)
        ranked_str = "; ".join(f"{b} ({_fmt_money(v)})" for b, v in ranked.items())
        text = (
            f"TOTAL net sales per {category} brand, summed across all "
            f"{n_months} months of available data (not per-month), ranked "
            f"by sales magnitude - largest first: {ranked_str}. The "
            f"top-selling {category} brand OVERALL across the whole period "
            f"was '{ranked.index[0]}' with total net sales of "
            f"{_fmt_money(ranked.iloc[0])}."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "category_brand_totals",
        })
    return chunks


def _category_customer_totals_chunks(df: pd.DataFrame, top_n: int = 10) -> list[dict]:
    """One chunk per category ranking the top customers by TOTAL net sales
    summed across the whole period - not per-month. Same gap as brand totals:
    without this, "top customer overall (all N months)" has no precomputed
    sum, and the model mistakes a single month's figure for the total."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        n_months = g["month"].nunique()
        customer_totals = g.groupby("customer_name")["net_sale"].sum()
        ranked = _rank_by_magnitude(customer_totals, top_n)
        ranked_str = "; ".join(f"{c} ({_fmt_money(v)})" for c, v in ranked.items())
        text = (
            f"TOTAL net sales for the top {top_n} {category} customers, "
            f"summed across all {n_months} months of available data (not "
            f"per-month), ranked by sales magnitude - largest first: "
            f"{ranked_str}. The top {category} customer OVERALL across the "
            f"whole period was '{ranked.index[0]}' with total net sales of "
            f"{_fmt_money(ranked.iloc[0])}."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "category_customer_totals",
        })
    return chunks


def _sale_type_totals_chunks(df: pd.DataFrame) -> list[dict]:
    """Cash Sale vs Credit Sale totals, summed across the whole period. Only
    the Dairy sheet has a Sale Type column - Cereals has none, so this is
    skipped for categories where the field is entirely empty rather than
    emitting a chunk with nothing useful in it."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        if g["sale_type"].isna().all():
            continue
        n_months = g["month"].nunique()
        totals = g.groupby("sale_type")["net_sale"].sum()
        ranked = _rank_by_magnitude(totals)
        ranked_str = "; ".join(f"{s} ({_fmt_money(v)})" for s, v in ranked.items())
        text = (
            f"{category} net sales by Sale Type, summed across all "
            f"{n_months} months of available data: {ranked_str}. By sales "
            f"magnitude, '{ranked.index[0]}' is LARGER than the other sale "
            f"type(s) for {category}."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "sale_type_totals",
        })
    return chunks


def _category_material_totals_chunks(df: pd.DataFrame, top_n: int = 10) -> list[dict]:
    """One chunk per category ranking the top materials (SKUs, by mat_name)
    by TOTAL net sales summed across the whole period. Same gap as brand and
    customer totals: without this, "top-selling material/SKU" has no
    precomputed answer at that granularity, and the model substitutes the
    brand-level answer instead - a different, coarser question."""
    chunks = []
    grouped = df.groupby("category", dropna=True)
    for category, g in grouped:
        n_months = g["month"].nunique()
        material_totals = g.groupby("mat_name")["net_sale"].sum()
        ranked = _rank_by_magnitude(material_totals, top_n)
        ranked_str = "; ".join(f"{m} ({_fmt_money(v)})" for m, v in ranked.items())
        text = (
            f"TOTAL net sales for the top {top_n} {category} materials "
            f"(SKUs), summed across all {n_months} months of available data "
            f"(not per-month), ranked by sales magnitude - largest first: "
            f"{ranked_str}. The top-selling {category} material/SKU OVERALL "
            f"across the whole period was '{ranked.index[0]}' with total "
            f"net sales of {_fmt_money(ranked.iloc[0])}."
        )
        chunks.append({
            "text": text,
            "category": category,
            "chunk_type": "category_material_totals",
        })
    return chunks


def _dataset_totals_chunks(df: pd.DataFrame) -> list[dict]:
    """One chunk for the whole dataset: the grand total across ALL categories
    combined, plus each category's own total for reference. Every other
    total chunk is scoped to one category or one month - a cross-category
    "grand total" has no precomputed answer without this, so the model has
    to sum several already-retrieved figures itself. Local models are
    unreliable at exact multi-digit arithmetic (confirmed by testing: it
    correctly retrieved every individual component figure but got both
    sub-totals and the final sum wrong), so this must be computed in
    pandas, not left to the model."""
    by_category = df.groupby("category")["net_sale"].sum()
    per_category_str = "; ".join(
        f"{c} ({_fmt_money(v)})" for c, v in by_category.items()
    )
    grand_total = df["net_sale"].sum()
    n_months = df["month"].nunique()
    text = (
        f"GRAND TOTAL net sales across ALL categories combined "
        f"({' + '.join(by_category.index)}) and all {n_months} months of "
        f"available data: {_fmt_money(grand_total)}. Per-category totals "
        f"that sum to this grand total: {per_category_str}. Total invoices: "
        f"{df['invoice_no'].nunique()}. Total distinct customers: "
        f"{df['customer_name'].nunique()}."
    )
    return [{"text": text, "chunk_type": "dataset_totals"}]


def _top_customer_chunks(df: pd.DataFrame, top_n: int = 10) -> list[dict]:
    chunks = []
    grouped = df.groupby(["month", "category"], dropna=True)
    for (month, category), g in grouped:
        top = _rank_by_magnitude(g.groupby("customer_name")["net_sale"].sum(), top_n)
        top_str = "; ".join(f"{c} ({_fmt_money(v)})" for c, v in top.items())
        text = (
            f"Top {top_n} customers by sales magnitude for {category} in "
            f"{_month_label(month)}, largest first (figures are negative by "
            f"export convention - a larger magnitude means more sales "
            f"activity, regardless of sign): {top_str}."
        )
        chunks.append({
            "text": text,
            "month": month, "category": category,
            "chunk_type": "top_customers",
        })
    return chunks


def build_chunks(df: pd.DataFrame) -> pd.DataFrame:
    """Build the full set of summary chunks from the master sales DataFrame."""
    all_chunks = (
        _brand_month_chunks(df)
        + _channel_month_chunks(df)
        + _category_month_totals_chunks(df)
        + _category_month_top_brands_chunks(df)
        + _category_trend_chunks(df)
        + _brand_trend_chunks(df)
        + _customer_trend_chunks(df)
        + _category_brand_totals_chunks(df)
        + _category_channel_totals_chunks(df)
        + _category_customer_totals_chunks(df)
        + _category_material_totals_chunks(df)
        + _sale_type_totals_chunks(df)
        + _dataset_totals_chunks(df)
        + _top_customer_chunks(df)
    )
    chunks_df = pd.DataFrame(all_chunks)
    chunks_df.insert(0, "chunk_id", range(len(chunks_df)))
    return chunks_df


if __name__ == "__main__":
    from src.ingest import load_all

    data = load_all()
    chunks = build_chunks(data)
    print(f"Built {len(chunks)} chunks")
    print(chunks["chunk_type"].value_counts())
    print("\nSample chunks:")
    for text in chunks["text"].sample(3, random_state=0):
        print("-", text)
