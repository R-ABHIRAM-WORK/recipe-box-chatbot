"""Build the cleaned SQLite database (`data/recipes.db`) from all 3 Recipe Box JSON files.

Tables created:
  1. recipes              - One row per valid recipe (stable ID, original fields, step & ingredient counts)
  2. recipe_ingredients   - Normalized ingredient rows linked to `recipe_id` with parsed quantities,
                            units, base-unit conversions (ml/g/count), canonical names, and families
  3. ingredients_summary  - Materialized stats per canonical ingredient (recipe_count, family, sample)
  4. recipes_fts          - SQLite FTS5 virtual table for fast keyword/BM25 search
  5. dataset_metadata     - Build provenance, counts, and normalization/step-counting rules
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from src.normalizer import (
    clean_raw_ingredient_line,
    is_junk_recipe,
    parse_ingredient_line,
    split_instructions,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
DB_PATH = PROJECT_ROOT / "data" / "recipes.db"

SOURCE_FILES: list[tuple[str, str]] = [
    ("ar", "recipes_raw_nosource_ar.json"),
    ("epi", "recipes_raw_nosource_epi.json"),
    ("fn", "recipes_raw_nosource_fn.json"),
]

SCHEMA_SQL = """
CREATE TABLE recipes (
    recipe_id               TEXT PRIMARY KEY,
    source                  TEXT NOT NULL,
    title                   TEXT NOT NULL,
    title_lower             TEXT NOT NULL,
    ingredients_raw_json    TEXT NOT NULL,
    ingredients_clean_json  TEXT NOT NULL,
    instructions_raw        TEXT NOT NULL,
    steps_json              TEXT NOT NULL,
    step_count              INTEGER NOT NULL,
    ingredient_count        INTEGER NOT NULL,
    unique_ingredient_count INTEGER NOT NULL,
    picture_link            TEXT,
    has_ingredients         INTEGER NOT NULL,
    has_instructions        INTEGER NOT NULL,
    is_junk                 INTEGER NOT NULL DEFAULT 0,
    is_duplicate            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE recipe_ingredients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       TEXT NOT NULL REFERENCES recipes(recipe_id),
    line_index      INTEGER NOT NULL,
    raw_text        TEXT NOT NULL,
    quantity        REAL,
    qty_max         REAL,
    unit            TEXT,
    unit_type       TEXT NOT NULL,
    quantity_base   REAL,
    canonical_name  TEXT NOT NULL,
    broad_family    TEXT,
    is_optional     INTEGER NOT NULL DEFAULT 0,
    prep_notes      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE ingredients_summary (
    canonical_name  TEXT PRIMARY KEY,
    broad_family    TEXT,
    recipe_count    INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL,
    sample_raw      TEXT NOT NULL
);

CREATE VIRTUAL TABLE recipes_fts USING fts5(
    recipe_id UNINDEXED,
    title,
    ingredients_text,
    instructions_text,
    tokenize = 'porter unicode61'
);

CREATE TABLE dataset_metadata (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX idx_recipes_source         ON recipes(source);
CREATE INDEX idx_recipes_title_lower    ON recipes(title_lower);
CREATE INDEX idx_recipes_step_count     ON recipes(step_count);
CREATE INDEX idx_recipes_ing_count      ON recipes(ingredient_count);
CREATE INDEX idx_recipes_unique_ing     ON recipes(unique_ingredient_count);
CREATE INDEX idx_recipes_quality        ON recipes(has_ingredients, has_instructions, is_junk, is_duplicate);

CREATE INDEX idx_ri_recipe_id           ON recipe_ingredients(recipe_id);
CREATE INDEX idx_ri_canonical           ON recipe_ingredients(canonical_name);
CREATE INDEX idx_ri_canonical_recipe    ON recipe_ingredients(canonical_name, recipe_id);
CREATE INDEX idx_ri_family              ON recipe_ingredients(broad_family);
CREATE INDEX idx_ri_family_recipe       ON recipe_ingredients(broad_family, recipe_id);

CREATE INDEX idx_ing_summary_count      ON ingredients_summary(recipe_count DESC);
CREATE INDEX idx_ing_summary_family     ON ingredients_summary(broad_family);
"""


def build_database(db_path: Path = DB_PATH, raw_dir: Path = RAW_DIR) -> Path:
    t0 = time.perf_counter()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Build into a fresh temp file and swap atomically so partial builds never corrupt DB
    tmp_db = db_path.with_suffix(".db.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA synchronous = OFF;")
    conn.execute("PRAGMA journal_mode = MEMORY;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA cache_size = -128000;")  # ~128 MB cache during bulk load

    conn.executescript(SCHEMA_SQL)

    total_raw_entries = 0
    skipped_empty = 0
    junk_flagged = 0
    duplicate_flagged = 0
    loaded_by_source: dict[str, int] = {}
    total_ingredient_rows = 0
    seen_signatures: set[tuple[str, str]] = set()

    recipe_batch: list[tuple] = []
    ing_batch: list[tuple] = []
    fts_batch: list[tuple] = []
    BATCH_SIZE = 2000

    def flush_batches() -> None:
        if recipe_batch:
            conn.executemany(
                """
                INSERT INTO recipes (
                    recipe_id, source, title, title_lower,
                    ingredients_raw_json, ingredients_clean_json,
                    instructions_raw, steps_json,
                    step_count, ingredient_count, unique_ingredient_count,
                    picture_link, has_ingredients, has_instructions,
                    is_junk, is_duplicate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                recipe_batch,
            )
            recipe_batch.clear()
        if ing_batch:
            conn.executemany(
                """
                INSERT INTO recipe_ingredients (
                    recipe_id, line_index, raw_text,
                    quantity, qty_max, unit, unit_type, quantity_base,
                    canonical_name, broad_family, is_optional, prep_notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ing_batch,
            )
            ing_batch.clear()
        if fts_batch:
            conn.executemany(
                """
                INSERT INTO recipes_fts (
                    recipe_id, title, ingredients_text, instructions_text
                ) VALUES (?, ?, ?, ?)
                """,
                fts_batch,
            )
            fts_batch.clear()

    for source, fname in SOURCE_FILES:
        fpath = raw_dir / fname
        if not fpath.exists():
            raise FileNotFoundError(f"Missing raw dataset file: {fpath}")

        print(f"Loading {fname} (source='{source}') ...", flush=True)
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        source_loaded = 0
        for raw_key, rec in data.items():
            total_raw_entries += 1
            if not isinstance(rec, dict) or not rec.get("title"):
                skipped_empty += 1
                continue

            recipe_id = f"{source}:{raw_key}"
            title = str(rec["title"]).strip()
            if not title:
                skipped_empty += 1
                continue

            raw_ingredients = rec.get("ingredients") or []
            if not isinstance(raw_ingredients, list):
                raw_ingredients = []

            instructions_raw = str(rec.get("instructions") or "").strip()
            picture_link = rec.get("picture_link")
            if picture_link is not None:
                picture_link = str(picture_link).strip() or None

            # Clean display ingredient list (removes ADVERTISEMENT artifacts while preserving wording)
            clean_ing_display: list[str] = []
            parsed_items = []
            for idx, raw_line in enumerate(raw_ingredients):
                if not isinstance(raw_line, str):
                    continue
                cleaned_line = clean_raw_ingredient_line(raw_line)
                if cleaned_line and cleaned_line.lower() != "advertisement":
                    clean_ing_display.append(cleaned_line)
                for parsed in parse_ingredient_line(raw_line):
                    parsed_items.append((idx, parsed))

            unique_canonicals = {p.canonical_name for _, p in parsed_items}
            steps = split_instructions(instructions_raw)
            clean_json = json.dumps(clean_ing_display, ensure_ascii=False)

            junk_flag = 1 if is_junk_recipe(title, instructions_raw) else 0
            sig = (title.lower(), clean_json)
            dup_flag = 1 if sig in seen_signatures else 0
            seen_signatures.add(sig)
            if junk_flag:
                junk_flagged += 1
            if dup_flag:
                duplicate_flagged += 1

            recipe_batch.append(
                (
                    recipe_id,
                    source,
                    title,
                    title.lower(),
                    json.dumps(raw_ingredients, ensure_ascii=False),
                    clean_json,
                    instructions_raw,
                    json.dumps(steps, ensure_ascii=False),
                    len(steps),
                    len(parsed_items),
                    len(unique_canonicals),
                    picture_link,
                    1 if parsed_items else 0,
                    1 if steps else 0,
                    junk_flag,
                    dup_flag,
                )
            )

            for line_idx, p in parsed_items:
                ing_batch.append(
                    (
                        recipe_id,
                        line_idx,
                        p.raw_text,
                        p.quantity,
                        p.qty_max,
                        p.unit,
                        p.unit_type,
                        p.quantity_base,
                        p.canonical_name,
                        p.broad_family,
                        1 if p.is_optional else 0,
                        p.prep_notes,
                    )
                )
                total_ingredient_rows += 1

            # Combine clean lines + canonical names for rich full-text search
            ing_search_blob = " ; ".join(clean_ing_display)
            if unique_canonicals:
                ing_search_blob = f"{ing_search_blob} | {' '.join(sorted(unique_canonicals))}"
            fts_batch.append((recipe_id, title, ing_search_blob, instructions_raw))

            source_loaded += 1
            if len(recipe_batch) >= BATCH_SIZE:
                flush_batches()

        flush_batches()
        loaded_by_source[source] = source_loaded
        print(f"  -> {source_loaded:,} valid recipes loaded from {source}", flush=True)

    print("Building SQL indexes ...", flush=True)
    conn.executescript(INDEX_SQL)

    print("Building ingredients_summary lookup table ...", flush=True)
    conn.execute(
        """
        INSERT INTO ingredients_summary (
            canonical_name, broad_family, recipe_count, occurrence_count, sample_raw
        )
        SELECT
            canonical_name,
            MAX(broad_family) AS broad_family,
            COUNT(DISTINCT recipe_id) AS recipe_count,
            COUNT(*) AS occurrence_count,
            MIN(raw_text) AS sample_raw
        FROM recipe_ingredients
        GROUP BY canonical_name
        """
    )

    total_loaded = sum(loaded_by_source.values())
    unique_canonical_total = conn.execute("SELECT COUNT(*) FROM ingredients_summary").fetchone()[0]

    metadata_rows = [
        ("built_at_utc", datetime.now(timezone.utc).isoformat()),
        ("total_raw_entries", str(total_raw_entries)),
        ("skipped_empty_entries", str(skipped_empty)),
        ("total_valid_recipes", str(total_loaded)),
        ("junk_recipes_flagged", str(junk_flagged)),
        ("duplicate_recipes_flagged", str(duplicate_flagged)),
        ("loaded_by_source", json.dumps(loaded_by_source)),
        ("total_ingredient_rows", str(total_ingredient_rows)),
        ("unique_canonical_ingredients", str(unique_canonical_total)),
        (
            "step_counting_rule",
            "Split instructions on newlines (excluding empty and boilerplate lines like 'Watch how to make this recipe.'); "
            "strip leading numbering; if only 1 paragraph remains, split on sentence boundaries while preserving abbreviations.",
        ),
        (
            "ingredient_normalization_rule",
            "Deterministic conservative pipeline: strip Allrecipes ADVERTISEMENT artifacts, equipment/footnotes, section headers, "
            "HTML fragments, alternative 'or ...' clauses, quantities/units, and non-identity size/prep modifiers; singularize head "
            "nouns and map strict synonyms only while preserving distinct forms/varieties (e.g. tomato vs tomato paste, milk vs "
            "coconut milk, butter vs peanut butter, brinjal vs long brinjal).",
        ),
    ]
    conn.executemany("INSERT INTO dataset_metadata (key, value) VALUES (?, ?)", metadata_rows)

    print("Running ANALYZE and finalizing SQLite file ...", flush=True)
    conn.execute("ANALYZE;")
    conn.commit()
    conn.close()

    # Replace final DB atomically (remove stale WAL/SHM sidecars if present)
    for sidecar in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if sidecar.exists():
            sidecar.unlink()
    tmp_db.rename(db_path)

    # Set clean runtime PRAGMAs (WAL mode)
    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA journal_mode = WAL;")
    conn2.close()

    elapsed = time.perf_counter() - t0
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print("\n=== BUILD COMPLETE ===", flush=True)
    print(f"Database:            {db_path} ({size_mb:.1f} MB)", flush=True)
    print(f"Elapsed time:        {elapsed:.1f}s", flush=True)
    print(f"Raw entries read:    {total_raw_entries:,} (skipped empty: {skipped_empty:,})", flush=True)
    print(f"Valid recipes:       {total_loaded:,} {loaded_by_source}", flush=True)
    print(f"Flagged quality:     junk={junk_flagged:,}, exact_duplicates={duplicate_flagged:,}", flush=True)
    print(f"Ingredient rows:     {total_ingredient_rows:,}", flush=True)
    print(f"Unique canonicals:   {unique_canonical_total:,}", flush=True)
    return db_path


if __name__ == "__main__":
    build_database()
