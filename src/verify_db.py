"""Quick verification & sanity-check queries on `data/recipes.db`."""

from pathlib import Path
import sqlite3
import time

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "recipes.db"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=== 1. METADATA ===")
    for row in conn.execute("SELECT key, value FROM dataset_metadata ORDER BY key"):
        val = row["value"]
        if len(val) > 120:
            val = val[:117] + "..."
        print(f"  {row['key']}: {val}")

    print("\n=== 2. TOP 20 CANONICAL INGREDIENTS ===")
    for row in conn.execute(
        "SELECT canonical_name, broad_family, recipe_count, sample_raw "
        "FROM ingredients_summary ORDER BY recipe_count DESC LIMIT 20"
    ):
        print(
            f"  {row['canonical_name']:22s} | fam={str(row['broad_family']):8s} "
            f"| recipes={row['recipe_count']:6,d} | e.g. {row['sample_raw'][:45]}"
        )

    print("\n=== 3. SAMPLE PARSED INGREDIENT ROWS ===")
    for row in conn.execute(
        "SELECT recipe_id, raw_text, quantity, qty_max, unit, unit_type, quantity_base, canonical_name, broad_family "
        "FROM recipe_ingredients WHERE quantity IS NOT NULL LIMIT 10"
    ):
        print(
            f"  [{row['recipe_id'][:12]}] {row['raw_text'][:42]:42s} -> "
            f"qty={row['quantity']} {str(row['unit']):6s} ({row['unit_type']}, base={row['quantity_base']}) "
            f"canon={row['canonical_name']!r} fam={row['broad_family']}"
        )

    print("\n=== 4. BENCHMARK QUERIES (SPEED & ACCURACY) ===")

    # Query A: Garlic + Onion count & percentage
    t0 = time.perf_counter()
    total_recipes = conn.execute("SELECT COUNT(*) FROM recipes WHERE has_ingredients = 1").fetchone()[0]
    both_exact = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT recipe_id FROM recipe_ingredients
            WHERE canonical_name IN ('garlic', 'onion')
            GROUP BY recipe_id
            HAVING COUNT(DISTINCT canonical_name) = 2
        )
        """
    ).fetchone()[0]
    both_family = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT recipe_id FROM recipe_ingredients
            WHERE broad_family IN ('garlic', 'onion')
            GROUP BY recipe_id
            HAVING COUNT(DISTINCT broad_family) = 2
        )
        """
    ).fetchone()[0]
    dt = (time.perf_counter() - t0) * 1000
    print(f"  Garlic + Onion (exact canonical): {both_exact:,} / {total_recipes:,} ({both_exact/total_recipes*100:.2f}%)")
    print(f"  Garlic + Onion (broad family):    {both_family:,} / {total_recipes:,} ({both_family/total_recipes*100:.2f}%) [{dt:.1f} ms]")

    # Query B: Pantry match (egg, onion, tomato, rice; no peanuts; <= 6 steps)
    t0 = time.perf_counter()
    rows = conn.execute(
        """
        WITH matched AS (
            SELECT recipe_id, COUNT(DISTINCT canonical_name) AS matched_cnt
            FROM recipe_ingredients
            WHERE canonical_name IN ('egg', 'onion', 'tomato', 'rice')
            GROUP BY recipe_id
        ),
        peanut_recipes AS (
            SELECT DISTINCT recipe_id FROM recipe_ingredients
            WHERE broad_family = 'peanut' OR LOWER(raw_text) LIKE '%peanut%'
        )
        SELECT r.recipe_id, r.title, m.matched_cnt,
               r.unique_ingredient_count,
               (r.unique_ingredient_count - m.matched_cnt) AS extra_needed,
               r.step_count
        FROM matched m
        JOIN recipes r ON r.recipe_id = m.recipe_id
        WHERE r.has_ingredients = 1
          AND r.has_instructions = 1
          AND r.step_count <= 6
          AND r.recipe_id NOT IN (SELECT recipe_id FROM peanut_recipes)
        ORDER BY m.matched_cnt DESC, extra_needed ASC, r.step_count ASC
        LIMIT 5
        """
    ).fetchall()
    dt = (time.perf_counter() - t0) * 1000
    print(f"\n  Pantry match (egg, onion, tomato, rice; no peanuts; <=6 steps) [{dt:.1f} ms]:")
    for r in rows:
        print(
            f"    - [{r['recipe_id']}] {r['title']} | matched={r['matched_cnt']}/4, "
            f"extra={r['extra_needed']}, steps={r['step_count']}"
        )

    # Query C: FTS5 search for dairy-free penne pasta
    t0 = time.perf_counter()
    rows = conn.execute(
        """
        WITH dairy_recipes AS (
            SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE broad_family = 'dairy'
        )
        SELECT r.recipe_id, r.title, r.ingredient_count, r.step_count
        FROM recipes_fts f
        JOIN recipes r ON r.recipe_id = f.recipe_id
        WHERE recipes_fts MATCH 'penne AND pasta'
          AND r.recipe_id NOT IN (SELECT recipe_id FROM dairy_recipes)
        ORDER BY bm25(recipes_fts)
        LIMIT 5
        """
    ).fetchall()
    dt = (time.perf_counter() - t0) * 1000
    print(f"\n  FTS5 Dairy-free Penne Pasta [{dt:.1f} ms]:")
    for r in rows:
        print(f"    - [{r['recipe_id']}] {r['title']} (ings={r['ingredient_count']}, steps={r['step_count']})")

    conn.close()


if __name__ == "__main__":
    main()
