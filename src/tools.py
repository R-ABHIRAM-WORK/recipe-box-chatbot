"""Deterministic SQL/Python recipe tools backed by `data/recipes.db`.

All factual lookups, statistics, pantry matching, hard-constraint filtering,
and local substitution evidence discovery happen here—never guessed by an LLM.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Any, Optional

from src.normalizer import (
    NON_DAIRY_EXCEPTIONS,
    classify_broad_family,
    normalize_ingredient_name,
    parse_ingredient_line,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "recipes.db"

# Recognized broad restriction groups -> (family_key, extra_raw_keywords, safe_exceptions)
RESTRICTION_GROUPS: dict[str, tuple[Optional[str], tuple[str, ...], tuple[str, ...]]] = {
    "dairy": (
        "dairy",
        ("milk", "butter", "cream", "cheese", "yogurt", "yoghurt", "cheddar", "parmesan",
         "mozzarella", "ricotta", "feta", "ghee", "buttermilk", "whey", "casein", "paneer",
         "mascarpone", "brie", "gouda", "gruyere", "half-and-half", "sour cream"),
        NON_DAIRY_EXCEPTIONS,
    ),
    "peanut": ("peanut", ("peanut", "peanuts", "groundnut"), ()),
    "peanuts": ("peanut", ("peanut", "peanuts", "groundnut"), ()),
    "tree_nut": (
        "tree_nut",
        ("almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut", "macadamia", "pine nut", "chestnut"),
        (),
    ),
    "nut": (
        None,
        ("peanut", "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut", "macadamia", "pine nut", "chestnut"),
        ("butternut", "nutmeg", "coconut", "donut", "doughnut"),
    ),
    "nuts": (
        None,
        ("peanut", "almond", "walnut", "pecan", "cashew", "pistachio", "hazelnut", "macadamia", "pine nut", "chestnut"),
        ("butternut", "nutmeg", "coconut", "donut", "doughnut"),
    ),
    "egg": ("egg", ("egg", "eggs", "mayonnaise", "mayo"), ("eggplant", "egg noodle", "eggplant")),
    "eggs": ("egg", ("egg", "eggs", "mayonnaise", "mayo"), ("eggplant", "egg noodle")),
    "shellfish": (
        "shellfish",
        ("shrimp", "prawn", "crab", "lobster", "clam", "mussel", "oyster", "scallop", "squid", "calamari"),
        (),
    ),
    "soy": ("soy", ("soy sauce", "tofu", "edamame", "tempeh", "miso", "soybean", "soy milk", "tamari"), ()),
    "gluten": (
        None,
        ("wheat", "flour", "bread", "pasta", "penne", "spaghetti", "noodle", "barley", "rye",
         "couscous", "semolina", "farro", "bulgur", "panko", "cracker", "biscuit", "tortilla", "soy sauce"),
        ("almond flour", "coconut flour", "rice flour", "corn flour", "chickpea flour", "oat flour",
         "buckwheat", "gluten-free", "gluten free", "rice noodle", "corn tortilla"),
    ),
    "meat": (
        None,
        ("beef", "pork", "chicken", "turkey", "lamb", "veal", "bacon", "ham", "sausage",
         "steak", "prosciutto", "pancetta", "salami", "pepperoni", "chorizo", "duck", "venison", "brisket"),
        ("vegetable", "vegetarian", "vegan", "beefsteak tomato"),
    ),
}

_RE_FTS_TOKEN = re.compile(r"[a-zA-Z0-9]+")
_STOPWORDS = {
    "a", "an", "the", "with", "and", "or", "in", "of", "for", "to", "on", "my",
    "recipe", "recipes", "dish", "make", "how", "quick", "easy", "best", "classic",
    "homemade", "style", "simple", "good", "great", "old", "fashioned",
}


class RecipeDB:
    """Thin, fast, read-only query interface over `data/recipes.db`."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"SQLite database not found at {self.db_path}. Run `python -m src.build_db` first."
            )
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._meta = {
            r["key"]: r["value"]
            for r in self.conn.execute("SELECT key, value FROM dataset_metadata")
        }
        self._exclusion_cache: dict[str, set[str]] = {}

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _fts_query(self, text: str) -> str:
        tokens = [t.lower() for t in _RE_FTS_TOKEN.findall(text) if len(t) >= 2]
        if not tokens:
            return ""
        return " AND ".join(f'"{t}"' for t in tokens)

    def _excluded_ids_for_single_term(self, term: str) -> set[str]:
        if term in self._exclusion_cache:
            return self._exclusion_cache[term]

        excluded: set[str] = set()
        if term in RESTRICTION_GROUPS:
            fam, keywords, exceptions = RESTRICTION_GROUPS[term]
            if fam:
                rows = self.conn.execute(
                    "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE broad_family = ?",
                    (fam,),
                ).fetchall()
                excluded.update(r[0] for r in rows)

            # Also check FTS5 on ingredients_text for safety against uncategorized rows
            if keywords:
                fts_or = " OR ".join(f'"{k}"' for k in keywords)
                cand_rows = self.conn.execute(
                    "SELECT recipe_id, ingredients_text FROM recipes_fts WHERE ingredients_text MATCH ?",
                    (fts_or,),
                ).fetchall()
                kw_pat = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b", re.IGNORECASE)
                exc_pat = (
                    re.compile(r"\b(?:" + "|".join(re.escape(e) for e in exceptions) + r")\b", re.IGNORECASE)
                    if exceptions
                    else None
                )
                for r_id, ing_text in cand_rows:
                    if r_id in excluded:
                        continue
                    segments = ing_text.split("|", 1)[0].split(" ; ")
                    for seg in segments:
                        if exc_pat and exc_pat.search(seg):
                            stripped = exc_pat.sub(" ", seg)
                            if kw_pat.search(stripped):
                                excluded.add(r_id)
                                break
                        elif kw_pat.search(seg):
                            excluded.add(r_id)
                            break
        else:
            canon = normalize_ingredient_name(term) or term
            fam = classify_broad_family(canon)
            rows = self.conn.execute(
                "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE canonical_name = ?",
                (canon,),
            ).fetchall()
            excluded.update(r[0] for r in rows)
            fts_q = self._fts_query(canon)
            if fts_q:
                pat = re.compile(rf"\b{re.escape(canon)}s?\b", re.IGNORECASE)
                for r_id, ing_text in self.conn.execute(
                    "SELECT recipe_id, ingredients_text FROM recipes_fts WHERE ingredients_text MATCH ?",
                    (fts_q,),
                ):
                    if pat.search(ing_text):
                        excluded.add(r_id)
            if fam and term == fam:
                for r in self.conn.execute(
                    "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE broad_family = ?",
                    (fam,),
                ):
                    excluded.add(r[0])

        self._exclusion_cache[term] = excluded
        return excluded

    def _excluded_recipe_ids(self, exclude_terms: list[str]) -> set[str]:
        """Compute exact set of recipe_ids violating ANY hard exclusion."""
        excluded: set[str] = set()
        for raw_term in exclude_terms:
            term = raw_term.strip().lower()
            if term:
                excluded.update(self._excluded_ids_for_single_term(term))
        return excluded

    def _recipe_ids_for_ingredient(self, term: str, match_mode: str = "canonical") -> tuple[set[str], str]:
        """Resolve an ingredient name to matching recipe_ids.
        Returns (set_of_recipe_ids, explanation_string).
        """
        canon = normalize_ingredient_name(term) or term.strip().lower()
        fam = classify_broad_family(canon)

        if match_mode == "family" and fam:
            rows = self.conn.execute(
                "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE broad_family = ?",
                (fam,),
            ).fetchall()
            return {r[0] for r in rows}, f"broad_family='{fam}' (includes variants)"

        # Default: exact canonical_name match
        rows = self.conn.execute(
            "SELECT DISTINCT recipe_id FROM recipe_ingredients WHERE canonical_name = ?",
            (canon,),
        ).fetchall()
        ids = {r[0] for r in rows}
        return ids, f"canonical_name='{canon}'"

    def _fetch_recipe_cards(self, recipe_ids: list[str], include_steps: bool = False) -> list[dict[str, Any]]:
        if not recipe_ids:
            return []
        placeholders = ",".join("?" for _ in recipe_ids)
        rows = self.conn.execute(
            f"""
            SELECT recipe_id, source, title, ingredients_clean_json, steps_json,
                   step_count, ingredient_count, unique_ingredient_count
            FROM recipes
            WHERE recipe_id IN ({placeholders})
            """,
            recipe_ids,
        ).fetchall()
        by_id = {r["recipe_id"]: r for r in rows}
        cards: list[dict[str, Any]] = []
        for rid in recipe_ids:
            r = by_id.get(rid)
            if not r:
                continue
            card: dict[str, Any] = {
                "recipe_id": r["recipe_id"],
                "title": r["title"],
                "source": r["source"],
                "ingredient_count": r["ingredient_count"],
                "step_count": r["step_count"],
                "ingredients": json.loads(r["ingredients_clean_json"]),
            }
            if include_steps:
                card["steps"] = json.loads(r["steps_json"])
            cards.append(card)
        return cards

    # ------------------------------------------------------------------
    # TOOL 1: Recipe Search (Task 1)
    # ------------------------------------------------------------------
    def search_recipes(
        self,
        query: Optional[str] = None,
        include_ingredients: Optional[list[str]] = None,
        exclude_ingredients: Optional[list[str]] = None,
        max_steps: Optional[int] = None,
        max_ingredients: Optional[int] = None,
        source: Optional[str] = None,
        ingredient_match_mode: str = "canonical",
        include_steps: bool = False,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Search recipes by dish name / keywords and/or ingredients with strict filters."""
        include_ingredients = include_ingredients or []
        exclude_ingredients = exclude_ingredients or []

        # 1. Start with ranked candidate list from FTS5 if query given, else all valid recipes
        ordered_ids: list[str]
        if query and query.strip():
            q_clean = query.strip()
            fts_q = self._fts_query(q_clean)
            if not fts_q:
                ordered_ids = []
            else:
                # Prioritize title matches first, then full-record BM25
                title_hits = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT recipe_id FROM recipes_fts WHERE title MATCH ? ORDER BY bm25(recipes_fts) LIMIT 5000",
                        (fts_q,),
                    ).fetchall()
                ]
                seen = set(title_hits)
                full_hits = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT recipe_id FROM recipes_fts WHERE recipes_fts MATCH ? ORDER BY bm25(recipes_fts) LIMIT 5000",
                        (fts_q,),
                    ).fetchall()
                    if r[0] not in seen
                ]
                ordered_ids = title_hits + full_hits
        else:
            ordered_ids = [
                r[0]
                for r in self.conn.execute(
                    "SELECT recipe_id FROM recipes WHERE has_ingredients = 1 AND has_instructions = 1 AND is_junk = 0 AND is_duplicate = 0"
                ).fetchall()
            ]

        # 2. Apply required include_ingredients filter
        inc_notes: list[str] = []
        if include_ingredients:
            allowed: Optional[set[str]] = None
            for ing in include_ingredients:
                ids, note = self._recipe_ids_for_ingredient(ing, match_mode=ingredient_match_mode)
                inc_notes.append(note)
                allowed = ids if allowed is None else (allowed & ids)
            allowed = allowed or set()
            ordered_ids = [rid for rid in ordered_ids if rid in allowed]

        # 3. Apply HARD exclude_ingredients filter
        if exclude_ingredients:
            excluded = self._excluded_recipe_ids(exclude_ingredients)
            ordered_ids = [rid for rid in ordered_ids if rid not in excluded]

        # 4. Apply quality + numeric / metadata filters (exclude junk/duplicates & empty records)
        if ordered_ids:
            clauses = ["has_ingredients = 1", "has_instructions = 1", "is_junk = 0", "is_duplicate = 0"]
            params: list[Any] = []
            if max_steps is not None:
                clauses.append("step_count <= ?")
                params.append(max_steps)
            if max_ingredients is not None:
                clauses.append("ingredient_count <= ?")
                params.append(max_ingredients)
            if source:
                clauses.append("source = ?")
                params.append(source.lower())
            if len(ordered_ids) <= 2000:
                valid_meta: set[str] = set()
                for i in range(0, len(ordered_ids), 900):
                    chunk = ordered_ids[i : i + 900]
                    ph = ",".join("?" for _ in chunk)
                    for r in self.conn.execute(
                        f"SELECT recipe_id FROM recipes WHERE recipe_id IN ({ph}) AND {' AND '.join(clauses)}",
                        chunk + params,
                    ):
                        valid_meta.add(r[0])
            else:
                valid_meta = {
                    r[0]
                    for r in self.conn.execute(
                        f"SELECT recipe_id FROM recipes WHERE {' AND '.join(clauses)}", params
                    ).fetchall()
                }
            ordered_ids = [rid for rid in ordered_ids if rid in valid_meta]

        total_matches = len(ordered_ids)
        top_ids = ordered_ids[:limit]
        results = self._fetch_recipe_cards(top_ids, include_steps=include_steps)

        return {
            "tool": "search_recipes",
            "filters_applied": {
                "query": query,
                "include_ingredients": include_ingredients,
                "include_resolution": inc_notes,
                "exclude_ingredients": exclude_ingredients,
                "max_steps": max_steps,
                "max_ingredients": max_ingredients,
                "source": source,
            },
            "total_matches": total_matches,
            "returned_count": len(results),
            "results": results,
            "note": (
                "No recipes matched all filters (hard constraints were not relaxed)."
                if total_matches == 0
                else None
            ),
        }

    # ------------------------------------------------------------------
    # TOOL 2: Get Full Recipe Details (Task 1 & 3)
    # ------------------------------------------------------------------
    def get_recipe(self, recipe_id: str) -> dict[str, Any]:
        """Fetch full grounded record for one recipe_id."""
        r = self.conn.execute(
            """
            SELECT recipe_id, source, title, ingredients_clean_json,
                   instructions_raw, steps_json, step_count,
                   ingredient_count, unique_ingredient_count, picture_link
            FROM recipes
            WHERE recipe_id = ?
            """,
            (recipe_id,),
        ).fetchone()
        if not r:
            return {"tool": "get_recipe", "found": False, "recipe_id": recipe_id}

        ing_rows = self.conn.execute(
            """
            SELECT line_index, raw_text, quantity, qty_max, unit, unit_type,
                   quantity_base, canonical_name, broad_family, is_optional
            FROM recipe_ingredients
            WHERE recipe_id = ?
            ORDER BY line_index, id
            """,
            (recipe_id,),
        ).fetchall()

        return {
            "tool": "get_recipe",
            "found": True,
            "recipe_id": r["recipe_id"],
            "source": r["source"],
            "title": r["title"],
            "ingredient_count": r["ingredient_count"],
            "unique_ingredient_count": r["unique_ingredient_count"],
            "step_count": r["step_count"],
            "ingredients": json.loads(r["ingredients_clean_json"]),
            "parsed_ingredients": [dict(ir) for ir in ing_rows],
            "steps": json.loads(r["steps_json"]),
            "instructions_raw": r["instructions_raw"],
            "unavailable_attributes": [
                "nutrition_facts (calories, macros, sodium)",
                "structured_prep_or_cook_time",
                "verified_servings_yield",
                "user_ratings_or_reviews",
            ],
        }

    # ------------------------------------------------------------------
    # TOOL 3: Exact Programmatic Statistics (Task 1)
    # ------------------------------------------------------------------
    def compute_stats(
        self,
        metric: str,
        include_ingredients: Optional[list[str]] = None,
        exclude_ingredients: Optional[list[str]] = None,
        query: Optional[str] = None,
        match_mode: str = "canonical",
        limit: int = 5,
    ) -> dict[str, Any]:
        """Compute reproducible statistics over the FULL filtered dataset.

        Supported metrics:
          - 'count_and_percentage'  (computes both canonical & family when applicable)
          - 'ingredient_count_stats' (avg, median, min, max ingredient counts)
          - 'fewest_steps'          (top N recipes with fewest instruction steps)
          - 'most_steps'            (top N recipes with most instruction steps)
          - 'top_ingredients'       (most common canonical ingredients in subset)
          - 'dataset_overview'      (overall counts by source)
        """
        include_ingredients = include_ingredients or []
        exclude_ingredients = exclude_ingredients or []

        total_valid_recipes = self.conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        total_with_ingredients = self.conn.execute(
            "SELECT COUNT(*) FROM recipes WHERE has_ingredients = 1"
        ).fetchone()[0]
        total_complete = self.conn.execute(
            "SELECT COUNT(*) FROM recipes WHERE has_ingredients = 1 AND has_instructions = 1"
        ).fetchone()[0]

        if metric == "dataset_overview":
            by_source = {
                r["source"]: r["cnt"]
                for r in self.conn.execute(
                    "SELECT source, COUNT(*) AS cnt FROM recipes GROUP BY source"
                ).fetchall()
            }
            return {
                "tool": "compute_stats",
                "metric": metric,
                "total_valid_recipes": total_valid_recipes,
                "recipes_with_ingredients": total_with_ingredients,
                "complete_recipes_with_ingredients_and_steps": total_complete,
                "by_source": by_source,
                "normalization_rule": self._meta.get("ingredient_normalization_rule"),
                "step_counting_rule": self._meta.get("step_counting_rule"),
            }

        def get_filtered_ids(mode: str) -> tuple[set[str], list[str]]:
            notes: list[str] = []
            base_ids: Optional[set[str]] = None

            # Intersect selective filters FIRST instead of scanning all 125k rows!
            for ing in include_ingredients:
                ids, note = self._recipe_ids_for_ingredient(ing, match_mode=mode)
                base_ids = ids if base_ids is None else (base_ids & ids)
                notes.append(note)

            if query and query.strip():
                fts_q = self._fts_query(query)
                if fts_q:
                    q_ids = {
                        r[0]
                        for r in self.conn.execute(
                            "SELECT recipe_id FROM recipes_fts WHERE recipes_fts MATCH ?",
                            (fts_q,),
                        ).fetchall()
                    }
                    base_ids = q_ids if base_ids is None else (base_ids & q_ids)
                    notes.append(f"fts_match={fts_q}")

            if base_ids is None:
                if metric in ("fewest_steps", "most_steps"):
                    base_ids = {
                        r[0]
                        for r in self.conn.execute(
                            "SELECT recipe_id FROM recipes WHERE has_ingredients = 1 AND has_instructions = 1 AND step_count > 0"
                        ).fetchall()
                    }
                else:
                    base_ids = {
                        r[0]
                        for r in self.conn.execute(
                            "SELECT recipe_id FROM recipes WHERE has_ingredients = 1"
                        ).fetchall()
                    }
            elif metric in ("fewest_steps", "most_steps") and base_ids:
                # Filter the small candidate set for valid instructions/steps
                valid_step_ids: set[str] = set()
                id_list = list(base_ids)
                for i in range(0, len(id_list), 900):
                    chunk = id_list[i : i + 900]
                    ph = ",".join("?" for _ in chunk)
                    for r in self.conn.execute(
                        f"SELECT recipe_id FROM recipes WHERE recipe_id IN ({ph}) AND has_instructions = 1 AND step_count > 0",
                        chunk,
                    ):
                        valid_step_ids.add(r[0])
                base_ids = valid_step_ids

            if exclude_ingredients and base_ids:
                excluded = self._excluded_recipe_ids(exclude_ingredients)
                base_ids -= excluded
                notes.append(f"excluded={exclude_ingredients}")

            return base_ids, notes

        if metric == "count_and_percentage":
            exact_ids, exact_notes = get_filtered_ids("canonical")
            fam_ids, fam_notes = get_filtered_ids("family")
            denom = total_with_ingredients
            return {
                "tool": "compute_stats",
                "metric": metric,
                "filters": {
                    "query": query,
                    "include_ingredients": include_ingredients,
                    "exclude_ingredients": exclude_ingredients,
                },
                "denominator": {
                    "recipes_with_ingredients": denom,
                    "all_valid_recipes": total_valid_recipes,
                },
                "exact_canonical_match": {
                    "count": len(exact_ids),
                    "percentage_of_recipes_with_ingredients": round(len(exact_ids) / denom * 100, 2),
                    "percentage_of_all_valid_recipes": round(len(exact_ids) / total_valid_recipes * 100, 2),
                    "resolution": exact_notes,
                },
                "broad_family_match": {
                    "count": len(fam_ids),
                    "percentage_of_recipes_with_ingredients": round(len(fam_ids) / denom * 100, 2),
                    "percentage_of_all_valid_recipes": round(len(fam_ids) / total_valid_recipes * 100, 2),
                    "resolution": fam_notes,
                },
                "normalization_rule": self._meta.get("ingredient_normalization_rule"),
            }

        filtered_ids, notes = get_filtered_ids(match_mode)
        analyzed_count = len(filtered_ids)

        if analyzed_count == 0:
            return {
                "tool": "compute_stats",
                "metric": metric,
                "recipes_analyzed": 0,
                "resolution": notes,
                "results": [],
                "note": "No recipes matched the specified filters.",
            }

        # Fetch needed columns for all matching IDs (chunked safely for SQLite param limits)
        if metric == "ingredient_count_stats":
            ing_counts: list[int] = []
            step_counts: list[int] = []
            id_list = list(filtered_ids)
            for i in range(0, len(id_list), 900):
                chunk = id_list[i : i + 900]
                ph = ",".join("?" for _ in chunk)
                for r in self.conn.execute(
                    f"SELECT ingredient_count, step_count FROM recipes WHERE recipe_id IN ({ph})",
                    chunk,
                ):
                    ing_counts.append(r[0])
                    step_counts.append(r[1])
            return {
                "tool": "compute_stats",
                "metric": metric,
                "recipes_analyzed": analyzed_count,
                "resolution": notes,
                "ingredient_count": {
                    "mean": round(statistics.mean(ing_counts), 2),
                    "median": statistics.median(ing_counts),
                    "min": min(ing_counts),
                    "max": max(ing_counts),
                },
                "step_count": {
                    "mean": round(statistics.mean(step_counts), 2),
                    "median": statistics.median(step_counts),
                    "min": min(step_counts),
                    "max": max(step_counts),
                },
            }

        if metric in ("fewest_steps", "most_steps"):
            order = "ASC" if metric == "fewest_steps" else "DESC"
            # Gather (step_count, ingredient_count, recipe_id, title) across ALL filtered_ids
            candidates: list[tuple[int, int, str, str]] = []
            id_list = list(filtered_ids)
            for i in range(0, len(id_list), 900):
                chunk = id_list[i : i + 900]
                ph = ",".join("?" for _ in chunk)
                for r in self.conn.execute(
                    f"SELECT step_count, ingredient_count, recipe_id, title FROM recipes WHERE recipe_id IN ({ph})",
                    chunk,
                ):
                    candidates.append((r[0], r[1], r[2], r[3]))

            if order == "ASC":
                candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            else:
                candidates.sort(key=lambda x: (-x[0], x[1], x[2]))

            top = candidates[:limit]
            top_cards = self._fetch_recipe_cards([t[2] for t in top], include_steps=True)
            return {
                "tool": "compute_stats",
                "metric": metric,
                "recipes_analyzed": analyzed_count,
                "resolution": notes,
                "tie_breaker": "step_count, then ingredient_count ASC, then recipe_id ASC",
                "step_counting_rule": self._meta.get("step_counting_rule"),
                "results": top_cards,
            }

        if metric == "top_ingredients":
            counts: Counter[str] = Counter()
            id_list = list(filtered_ids)
            for i in range(0, len(id_list), 900):
                chunk = id_list[i : i + 900]
                ph = ",".join("?" for _ in chunk)
                for r in self.conn.execute(
                    f"SELECT DISTINCT recipe_id, canonical_name FROM recipe_ingredients WHERE recipe_id IN ({ph})",
                    chunk,
                ):
                    counts[r[1]] += 1
            top = [
                {
                    "canonical_name": name,
                    "recipe_count": cnt,
                    "percentage_of_subset": round(cnt / analyzed_count * 100, 2),
                }
                for name, cnt in counts.most_common(limit)
            ]
            return {
                "tool": "compute_stats",
                "metric": metric,
                "recipes_analyzed": analyzed_count,
                "resolution": notes,
                "results": top,
            }

        raise ValueError(f"Unsupported metric: {metric}")

    # ------------------------------------------------------------------
    # TOOL 4: Pantry / Ingredient-Based Recipe Matching (Task 2)
    # ------------------------------------------------------------------
    def match_pantry(
        self,
        available_ingredients: list[str],
        exclude_ingredients: Optional[list[str]] = None,
        max_steps: Optional[int] = None,
        max_missing: Optional[int] = None,
        prefer: str = "fewest_missing",
        limit: int = 5,
    ) -> dict[str, Any]:
        """Rank existing recipes from the dataset using available ingredients.

        Hard constraints (`exclude_ingredients`, `max_steps`, `max_missing`) are NEVER
        silently relaxed. If 0 eligible recipes exist, returns `eligible_found=False`
        and separately lists closest alternatives clearly tagged with their violations.
        """
        exclude_ingredients = exclude_ingredients or []

        # 1. Parse user's available ingredients with the EXACT same normalizer
        pantry: dict[str, dict[str, Any]] = {}
        for raw_item in available_ingredients:
            parsed_list = parse_ingredient_line(raw_item)
            if not parsed_list:
                canon = normalize_ingredient_name(raw_item)
                if canon:
                    pantry[canon] = {
                        "raw_input": raw_item,
                        "canonical_name": canon,
                        "quantity": None,
                        "unit": None,
                        "unit_type": "unspecified",
                        "quantity_base": None,
                    }
            else:
                for p in parsed_list:
                    pantry[p.canonical_name] = {
                        "raw_input": raw_item,
                        "canonical_name": p.canonical_name,
                        "quantity": p.quantity,
                        "unit": p.unit,
                        "unit_type": p.unit_type,
                        "quantity_base": p.quantity_base,
                    }

        if not pantry:
            return {
                "tool": "match_pantry",
                "eligible_found": False,
                "message": "No recognizable ingredients were provided.",
                "results": [],
            }

        pantry_keys = list(pantry.keys())
        ph = ",".join("?" for _ in pantry_keys)

        # 2. Candidate retrieval: all recipes using >= 1 of the user's canonical ingredients
        cand_rows = self.conn.execute(
            f"""
            SELECT m.recipe_id,
                   m.matched_cnt,
                   r.title,
                   r.source,
                   r.step_count,
                   r.ingredient_count,
                   r.unique_ingredient_count
            FROM (
                SELECT recipe_id, COUNT(DISTINCT canonical_name) AS matched_cnt
                FROM recipe_ingredients
                WHERE canonical_name IN ({ph})
                GROUP BY recipe_id
            ) m
            JOIN recipes r ON r.recipe_id = m.recipe_id
            WHERE r.has_ingredients = 1
              AND r.has_instructions = 1
              AND r.is_junk = 0
              AND r.is_duplicate = 0
            """,
            pantry_keys,
        ).fetchall()

        excluded_ids = self._excluded_recipe_ids(exclude_ingredients) if exclude_ingredients else set()

        def score_key(row: sqlite3.Row) -> tuple:
            matched = row["matched_cnt"]
            extra_est = max(row["unique_ingredient_count"] - matched, 0)
            steps = row["step_count"]
            coverage = matched / max(row["unique_ingredient_count"], 1)
            if prefer == "fewest_steps":
                return (-matched, steps, extra_est, -coverage, row["recipe_id"])
            if prefer == "most_matched":
                return (-matched, -coverage, extra_est, steps, row["recipe_id"])
            # Default: 'fewest_missing' / balanced — prioritize using available items AND minimal extra ingredients
            # Transparent composite score: matched * 2.0 - extra * 0.5 + coverage * 3.0
            composite = (matched * 2.0) + (coverage * 3.0) - (extra_est * 0.5)
            return (-round(composite, 4), -matched, extra_est, steps, row["recipe_id"])

        eligible_rows: list[sqlite3.Row] = []
        violating_rows: list[tuple[sqlite3.Row, list[str]]] = []

        for row in cand_rows:
            violations: list[str] = []
            if row["recipe_id"] in excluded_ids:
                violations.append(f"contains excluded ingredient/group ({', '.join(exclude_ingredients)})")
            if max_steps is not None and row["step_count"] > max_steps:
                violations.append(f"step_count={row['step_count']} exceeds max_steps={max_steps}")
            extra_est = max(row["unique_ingredient_count"] - row["matched_cnt"], 0)
            if max_missing is not None and extra_est > max_missing:
                violations.append(f"missing_ingredients={extra_est} exceeds max_missing={max_missing}")

            if not violations:
                eligible_rows.append(row)
            else:
                violating_rows.append((row, violations))

        eligible_rows.sort(key=score_key)

        def build_detailed_match(row: sqlite3.Row, violations: Optional[list[str]] = None) -> dict[str, Any]:
            rid = row["recipe_id"]
            ing_rows = self.conn.execute(
                """
                SELECT raw_text, quantity, qty_max, unit, unit_type,
                       quantity_base, canonical_name, is_optional
                FROM recipe_ingredients
                WHERE recipe_id = ?
                ORDER BY line_index, id
                """,
                (rid,),
            ).fetchall()

            matched_details: list[dict[str, Any]] = []
            extra_required: list[str] = []
            extra_optional: list[str] = []
            matched_canonicals: set[str] = set()
            seen_extra_req: set[str] = set()
            seen_extra_opt: set[str] = set()
            insufficient_count = 0

            for ir in ing_rows:
                cname = ir["canonical_name"]
                if cname in pantry:
                    matched_canonicals.add(cname)
                    u = pantry[cname]
                    # Quantity sufficiency check (ONLY when both have compatible unit_type!)
                    if u["quantity"] is None:
                        qty_status = "name_match_only (user did not specify quantity)"
                    elif ir["quantity_base"] is None or u["quantity_base"] is None:
                        qty_status = (
                            f"name_match_only (quantity not verified: you have '{u['raw_input']}', "
                            f"recipe asks for '{ir['raw_text']}')"
                        )
                    elif u["unit_type"] != ir["unit_type"]:
                        qty_status = (
                            f"name_match_only (incompatible units {u['unit_type']} vs {ir['unit_type']}; "
                            f"quantity not verified: '{u['raw_input']}' vs '{ir['raw_text']}')"
                        )
                    elif u["quantity_base"] >= ir["quantity_base"] - 1e-6:
                        qty_status = (
                            f"sufficient (you have {u['quantity']} {u['unit']}, "
                            f"recipe needs {ir['quantity']} {ir['unit']})"
                        )
                    else:
                        insufficient_count += 1
                        qty_status = (
                            f"insufficient (you have {u['quantity']} {u['unit']}, "
                            f"recipe needs {ir['quantity']} {ir['unit']})"
                        )
                    matched_details.append(
                        {
                            "canonical_name": cname,
                            "recipe_raw": ir["raw_text"],
                            "quantity_check": qty_status,
                        }
                    )
                else:
                    if ir["is_optional"]:
                        if cname not in seen_extra_opt:
                            seen_extra_opt.add(cname)
                            extra_optional.append(f"{cname} ({ir['raw_text']})")
                    else:
                        if cname not in seen_extra_req:
                            seen_extra_req.add(cname)
                            extra_required.append(f"{cname} ({ir['raw_text']})")

            unused_pantry = [k for k in pantry_keys if k not in matched_canonicals]
            coverage = len(matched_canonicals) / max(len(matched_canonicals) + len(extra_required), 1)
            composite = round(
                (len(matched_canonicals) * 2.0)
                + (coverage * 3.0)
                - (len(extra_required) * 0.5)
                - (insufficient_count * 1.0),
                2,
            )

            entry: dict[str, Any] = {
                "recipe_id": rid,
                "title": row["title"],
                "source": row["source"],
                "score": composite,
                "matched_count": len(matched_canonicals),
                "pantry_total": len(pantry_keys),
                "extra_required_count": len(extra_required),
                "extra_optional_count": len(extra_optional),
                "step_count": row["step_count"],
                "coverage_ratio": round(coverage, 2),
                "matched_ingredients": matched_details,
                "extra_required_ingredients": extra_required,
                "extra_optional_ingredients": extra_optional,
                "unused_available_ingredients": unused_pantry,
                "ranking_explanation": (
                    f"Matches {len(matched_canonicals)}/{len(pantry_keys)} of your ingredients "
                    f"({round(coverage * 100)}% of required ingredients), needs {len(extra_required)} "
                    f"additional required ingredient(s), {row['step_count']} step(s)"
                    + (f", {insufficient_count} quantity shortage(s)" if insufficient_count else "")
                    + "."
                ),
            }
            if violations:
                entry["violated_constraints"] = violations
            return entry

        # Re-rank top 50 eligible candidates using exact optional-aware & quantity-aware score
        # and deduplicate exact duplicate scraped entries (same title + same required ingredients)
        detailed_raw = [build_detailed_match(r) for r in eligible_rows[: max(limit * 10, 50)]]
        if prefer == "fewest_steps":
            detailed_raw.sort(
                key=lambda d: (-d["matched_count"], d["step_count"], d["extra_required_count"], d["recipe_id"])
            )
        elif prefer == "most_matched":
            detailed_raw.sort(
                key=lambda d: (-d["matched_count"], d["extra_required_count"], d["step_count"], d["recipe_id"])
            )
        else:
            detailed_raw.sort(
                key=lambda d: (-d["score"], -d["matched_count"], d["extra_required_count"], d["step_count"], d["recipe_id"])
            )

        detailed_eligible: list[dict[str, Any]] = []
        seen_signatures: set[tuple] = set()
        for d in detailed_raw:
            sig = (
                d["title"].strip().lower(),
                tuple(sorted(m["canonical_name"] for m in d["matched_ingredients"])),
                tuple(sorted(d["extra_required_ingredients"])),
            )
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            detailed_eligible.append(d)

        # If NO eligible match exists, explicitly state so and provide labeled violating near-misses separately
        violating_alternatives: list[dict[str, Any]] = []
        if not detailed_eligible and violating_rows:
            violating_rows.sort(key=lambda rv: (len(rv[1]), score_key(rv[0])))
            violating_alternatives = [
                build_detailed_match(r, violations=v) for r, v in violating_rows[:3]
            ]

        return {
            "tool": "match_pantry",
            "parsed_pantry": list(pantry.values()),
            "hard_constraints": {
                "exclude_ingredients": exclude_ingredients,
                "max_steps": max_steps,
                "max_missing": max_missing,
            },
            "preference": prefer,
            "scoring_formula": (
                "score = 2.0 * matched_count + 3.0 * required_coverage - 0.5 * extra_required_count "
                "- 1.0 * verified_quantity_shortages"
            ),
            "eligible_found": len(detailed_eligible) > 0,
            "total_eligible_candidates": len(eligible_rows),
            "results": detailed_eligible[:limit],
            "no_match_message": (
                "No existing recipe in the dataset satisfies all hard constraints."
                if not detailed_eligible
                else None
            ),
            "closest_violating_alternatives": violating_alternatives,
        }

    # ------------------------------------------------------------------
    # TOOL 5: Local Evidence Finder for Ingredient Substitution (Task 3)
    # ------------------------------------------------------------------
    def find_substitution_evidence(
        self,
        remove_ingredient: str,
        recipe_id: Optional[str] = None,
        dish_query: Optional[str] = None,
        exclude_restrictions: Optional[list[str]] = None,
        limit_supporting: int = 8,
    ) -> dict[str, Any]:
        """Search the LOCAL dataset for real recipe variants that omit `remove_ingredient`
        (and obey all `exclude_restrictions`) to ground ingredient substitutions in evidence.
        """
        exclude_restrictions = list(exclude_restrictions or [])
        remove_canon = normalize_ingredient_name(remove_ingredient) or remove_ingredient.strip().lower()

        target_recipe: Optional[dict[str, Any]] = None
        if recipe_id:
            rec = self.get_recipe(recipe_id)
            if rec.get("found"):
                target_recipe = rec

        search_phrase = dish_query
        if not search_phrase and target_recipe:
            # Extract meaningful dish keywords from target title (e.g. "Classic Banana Bread" -> "banana bread")
            words = [
                w.lower()
                for w in _RE_FTS_TOKEN.findall(target_recipe["title"])
                if w.lower() not in _STOPWORDS and len(w) >= 3
            ]
            search_phrase = " ".join(words[:4])

        if not search_phrase:
            return {
                "tool": "find_substitution_evidence",
                "evidence_strength": "none",
                "note": "Need either a valid recipe_id or a dish_query to search local recipe variants.",
            }

        # Combine removal target + user's hard restrictions
        all_exclusions = list(dict.fromkeys([remove_ingredient] + exclude_restrictions))
        excluded_ids = self._excluded_recipe_ids(all_exclusions)

        # Find direct dish-title matches in local DB
        fts_q = self._fts_query(search_phrase)
        direct_ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT recipe_id FROM recipes_fts WHERE title MATCH ? ORDER BY bm25(recipes_fts) LIMIT 1500",
                (fts_q,),
            ).fetchall()
        ] if fts_q else []

        # If too few direct title matches, also try broader head-noun fallback (indirect evidence!)
        evidence_type = "direct_dish_variants"
        broad_fallback_phrase: Optional[str] = None
        valid_direct = [rid for rid in direct_ids if rid not in excluded_ids and rid != recipe_id]

        supporting_pool = valid_direct
        if len(supporting_pool) < 5:
            tokens = [t for t in _RE_FTS_TOKEN.findall(search_phrase.lower()) if t not in _STOPWORDS and len(t) >= 3]
            if len(tokens) >= 2:
                # Try last word (usually the dish category, e.g., "bread", "pasta", "cake", "cookies")
                broad_fallback_phrase = tokens[-1]
                broad_ids = [
                    r[0]
                    for r in self.conn.execute(
                        "SELECT recipe_id FROM recipes_fts WHERE title MATCH ? ORDER BY bm25(recipes_fts) LIMIT 1500",
                        (f'"{broad_fallback_phrase}"',),
                    ).fetchall()
                ]
                valid_broad = [
                    rid
                    for rid in broad_ids
                    if rid not in excluded_ids and rid != recipe_id and rid not in set(supporting_pool)
                ]
                if valid_broad:
                    supporting_pool = supporting_pool + valid_broad
                    evidence_type = (
                        "mixed_direct_and_indirect" if valid_direct else "indirect_category_variants"
                    )

        # Baseline: recipes of the SAME dish that DO contain remove_ingredient
        baseline_ids = [rid for rid in direct_ids if rid in excluded_ids and rid != recipe_id]

        def ingredient_freq(ids: list[str]) -> tuple[Counter[str], dict[str, list[str]]]:
            freq: Counter[str] = Counter()
            examples: dict[str, list[str]] = {}
            sample_ids = ids[:400]
            for i in range(0, len(sample_ids), 200):
                chunk = sample_ids[i : i + 200]
                ph = ",".join("?" for _ in chunk)
                for rid, cname in self.conn.execute(
                    f"SELECT DISTINCT recipe_id, canonical_name FROM recipe_ingredients WHERE recipe_id IN ({ph})",
                    chunk,
                ):
                    freq[cname] += 1
                    lst = examples.setdefault(cname, [])
                    if len(lst) < 5:
                        lst.append(rid)
            return freq, examples

        target_canonicals = (
            {p["canonical_name"] for p in target_recipe["parsed_ingredients"]}
            if target_recipe
            else set()
        )

        # Filter out trivial seasoning/staple noise when ranking enriched candidate substitutes
        trivials = {"salt", "black pepper", "water", "sugar", "all-purpose flour"}

        valid_freq, valid_examples = ingredient_freq(supporting_pool)
        base_freq, _ = ingredient_freq(baseline_ids)

        n_valid = max(min(len(supporting_pool), 400), 1)
        n_base = max(min(len(baseline_ids), 400), 1)

        enriched_candidates: list[dict[str, Any]] = []
        for cname, v_cnt in valid_freq.items():
            if cname in trivials or cname == remove_canon:
                continue
            if v_cnt < 2 and len(supporting_pool) > 25:
                continue
            v_rate = v_cnt / n_valid
            b_rate = base_freq.get(cname, 0) / n_base
            lift = round((v_rate + 0.01) / (b_rate + 0.01), 2)
            already_in_target = cname in target_canonicals
            # Prefer ingredients that appear more often in the restricted-free variants than baseline
            if (not already_in_target and v_rate >= 0.05) or lift >= 1.4:
                enriched_candidates.append(
                    {
                        "candidate_ingredient": cname,
                        "present_in_valid_variants": v_cnt,
                        "valid_variant_rate_pct": round(v_rate * 100, 1),
                        "baseline_rate_pct": round(b_rate * 100, 1),
                        "enrichment_lift": lift,
                        "already_in_target_recipe": already_in_target,
                        "supporting_recipe_ids": valid_examples.get(cname, []),
                    }
                )

        enriched_candidates.sort(
            key=lambda c: (
                c["already_in_target_recipe"],  # new substitute ingredients first
                -c["enrichment_lift"],
                -c["present_in_valid_variants"],
            )
        )

        # Determine honest evidence strength
        if len(valid_direct) >= 10:
            strength = "strong"
        elif len(valid_direct) >= 3:
            strength = "moderate"
        elif len(supporting_pool) >= 3:
            strength = "weak_indirect"
        else:
            strength = "weak_or_none"

        supporting_cards = self._fetch_recipe_cards(supporting_pool[:limit_supporting], include_steps=True)

        return {
            "tool": "find_substitution_evidence",
            "target_recipe": (
                {
                    "recipe_id": target_recipe["recipe_id"],
                    "title": target_recipe["title"],
                    "ingredients": target_recipe["ingredients"],
                    "steps": target_recipe["steps"],
                }
                if target_recipe
                else None
            ),
            "remove_ingredient": remove_ingredient,
            "remove_canonical": remove_canon,
            "all_hard_exclusions": all_exclusions,
            "dish_search_used": search_phrase,
            "broad_fallback_used": broad_fallback_phrase,
            "evidence_type": evidence_type,
            "evidence_strength": strength,
            "counts": {
                "direct_matching_variants_without_restricted": len(valid_direct),
                "total_supporting_variants_analyzed": len(supporting_pool),
                "baseline_variants_with_restricted": len(baseline_ids),
            },
            "enriched_local_substitutes": enriched_candidates[:12],
            "supporting_recipes": supporting_cards,
        }
