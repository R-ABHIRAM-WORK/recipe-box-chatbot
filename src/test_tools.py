"""Act as the AI: run end-to-end deterministic tool tests across all 3 tasks + edge cases."""

from __future__ import annotations

import time
from src.tools import RecipeDB


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"TEST: {title}")
    print("=" * 78)


def main() -> None:
    db = RecipeDB()

    # ------------------------------------------------------------------
    # TEST 1: Dish search + multi-turn follow-up restriction ("no dairy")
    # ------------------------------------------------------------------
    section("1. Dish Search + Follow-up ('penne pasta' -> 'I cannot have dairy')")
    t0 = time.perf_counter()
    r1 = db.search_recipes(query="penne pasta", limit=3)
    print(f"Turn 1 ('penne pasta'): {r1['total_matches']:,} matches [{(time.perf_counter()-t0)*1000:.0f} ms]")
    for c in r1["results"]:
        print(f"  - [{c['recipe_id']}] {c['title']} ({c['ingredient_count']} ings, {c['step_count']} steps)")

    t0 = time.perf_counter()
    r2 = db.search_recipes(query="penne pasta", exclude_ingredients=["dairy"], limit=3)
    print(f"\nTurn 2 (+ exclude=['dairy']): {r2['total_matches']:,} matches [{(time.perf_counter()-t0)*1000:.0f} ms]")
    for c in r2["results"]:
        print(f"  - [{c['recipe_id']}] {c['title']}")
        print(f"      evidence ingredients: {c['ingredients'][:6]}")

    # ------------------------------------------------------------------
    # TEST 2: Exact Count & Percentage ("garlic AND onion")
    # ------------------------------------------------------------------
    section("2. Aggregate Count & Percentage ('both garlic and onion')")
    t0 = time.perf_counter()
    s1 = db.compute_stats(metric="count_and_percentage", include_ingredients=["garlic", "onion"])
    print(f"Elapsed: {(time.perf_counter()-t0)*1000:.0f} ms | Denominator (with ingredients): {s1['denominator']['recipes_with_ingredients']:,}")
    ex = s1["exact_canonical_match"]
    fm = s1["broad_family_match"]
    print(f"  Exact canonical ({ex['resolution']}): {ex['count']:,} ({ex['percentage_of_recipes_with_ingredients']}%)")
    print(f"  Broad family    ({fm['resolution']}): {fm['count']:,} ({fm['percentage_of_recipes_with_ingredients']}%)")

    # ------------------------------------------------------------------
    # TEST 3: Tomato recipes — fewest steps + avg/median ingredient count
    # ------------------------------------------------------------------
    section("3. Analytical Ranking & Averages ('tomato' -> fewest steps & avg/median ings)")
    t0 = time.perf_counter()
    s2 = db.compute_stats(metric="fewest_steps", include_ingredients=["tomato"], match_mode="canonical", limit=5)
    s3 = db.compute_stats(metric="ingredient_count_stats", include_ingredients=["tomato"], match_mode="canonical")
    print(f"Analyzed {s2['recipes_analyzed']:,} canonical-tomato recipes [{(time.perf_counter()-t0)*1000:.0f} ms]")
    print(f"Ingredient count stats: {s3['ingredient_count']} | Step count stats: {s3['step_count']}")
    print("Top 5 fewest steps:")
    for c in s2["results"]:
        print(f"  - [{c['recipe_id']}] {c['title']} | steps={c['step_count']}, ings={c['ingredient_count']}")
        print(f"      step 1: {c['steps'][0][:95]!r}")

    # ------------------------------------------------------------------
    # TEST 4: Missing attributes guard (nutrition / cook time)
    # ------------------------------------------------------------------
    section("4. Single Recipe Detail + Unavailable Attributes Guard")
    first_id = r2["results"][0]["recipe_id"]
    rec = db.get_recipe(first_id)
    print(f"Recipe [{rec['recipe_id']}]: {rec['title']} ({rec['ingredient_count']} ings, {rec['step_count']} steps)")
    print(f"Explicitly unavailable in dataset: {rec['unavailable_attributes']}")

    # ------------------------------------------------------------------
    # TEST 5: Pantry Match + Follow-up ("Exclude eggs too")
    # ------------------------------------------------------------------
    section("5. Pantry Match ('eggs, onions, tomatoes, rice; no peanuts; <=6 steps') + Follow-up")
    t0 = time.perf_counter()
    p1 = db.match_pantry(
        available_ingredients=["eggs", "onions", "tomatoes", "rice"],
        exclude_ingredients=["peanuts"],
        max_steps=6,
        prefer="fewest_missing",
        limit=5,
    )
    print(f"Turn 1: eligible_found={p1['eligible_found']} (pool={p1['total_eligible_candidates']:,}) [{(time.perf_counter()-t0)*1000:.0f} ms]")
    for item in p1["results"]:
        print(f"  - [{item['recipe_id']}] {item['title']} | score={item['score']} | {item['ranking_explanation']}")
        print(f"      matched: {[m['canonical_name'] for m in item['matched_ingredients']]} | extra req: {item['extra_required_ingredients'][:5]}")

    t0 = time.perf_counter()
    p2 = db.match_pantry(
        available_ingredients=["onions", "tomatoes", "rice"],
        exclude_ingredients=["peanuts", "eggs"],
        max_steps=6,
        prefer="fewest_missing",
        limit=5,
    )
    print(f"\nTurn 2 ('Exclude eggs too'): eligible_found={p2['eligible_found']} (pool={p2['total_eligible_candidates']:,}) [{(time.perf_counter()-t0)*1000:.0f} ms]")
    for item in p2["results"][:3]:
        print(f"  - [{item['recipe_id']}] {item['title']} | score={item['score']} | {item['ranking_explanation']}")

    # ------------------------------------------------------------------
    # TEST 6: Edge Case — Quantities (sufficient vs insufficient vs unverified)
    # ------------------------------------------------------------------
    section("6. Edge Case: Pantry Matching WITH Quantities & Unit Mismatches")
    p_qty = db.match_pantry(
        available_ingredients=["2 large eggs", "1 cup milk", "200g onions", "1 tbsp butter"],
        max_steps=5,
        limit=3,
    )
    for item in p_qty["results"]:
        print(f"  - [{item['recipe_id']}] {item['title']} | score={item['score']}")
        for m in item["matched_ingredients"]:
            print(f"      {m['canonical_name']:10s}: {m['quantity_check']}")

    # ------------------------------------------------------------------
    # TEST 7: Edge Case — Hard-Constraint NO-MATCH (never silently relax!)
    # ------------------------------------------------------------------
    section("7. Edge Case: Impossible Hard Constraints (No-Match + Labeled Violating Alternatives)")
    p_none = db.match_pantry(
        available_ingredients=["egg", "onion", "tomato", "rice"],
        exclude_ingredients=["egg", "onion", "tomato", "rice"],
        max_steps=1,
        max_missing=0,
        limit=3,
    )
    print(f"eligible_found={p_none['eligible_found']} | results={len(p_none['results'])} | msg={p_none['no_match_message']!r}")
    print("Separately labeled closest violating alternatives:")
    for alt in p_none["closest_violating_alternatives"]:
        print(f"  - [{alt['recipe_id']}] {alt['title']} | VIOLATIONS: {alt['violated_constraints']}")

    # ------------------------------------------------------------------
    # TEST 8: Edge Case — Coconut Milk NOT falsely flagged as Dairy!
    # ------------------------------------------------------------------
    section("8. Edge Case: Non-Dairy False Positive Guard ('coconut milk' + exclude=['dairy'])")
    r_coco = db.search_recipes(include_ingredients=["coconut milk"], exclude_ingredients=["dairy"], limit=3)
    print(f"Dairy-free recipes with coconut milk: {r_coco['total_matches']:,} matches")
    for c in r_coco["results"]:
        print(f"  - [{c['recipe_id']}] {c['title']} | sample ings: {c['ingredients'][:4]}")

    # ------------------------------------------------------------------
    # TEST 9: Task 3 Substitution Evidence — Banana Bread without Eggs
    # ------------------------------------------------------------------
    section("9. Task 3 Local Substitution Evidence ('banana bread' without 'egg')")
    t0 = time.perf_counter()
    bb_search = db.search_recipes(query="banana bread", include_ingredients=["egg"], limit=1)
    bb_id = bb_search["results"][0]["recipe_id"]
    ev1 = db.find_substitution_evidence(
        recipe_id=bb_id,
        remove_ingredient="egg",
        dish_query="banana bread",
        limit_supporting=5,
    )
    print(f"Target: [{ev1['target_recipe']['recipe_id']}] {ev1['target_recipe']['title']}")
    print(f"Evidence: strength={ev1['evidence_strength']}, type={ev1['evidence_type']}, counts={ev1['counts']} [{(time.perf_counter()-t0)*1000:.0f} ms]")
    print("Top enriched local substitutes:")
    for cand in ev1["enriched_local_substitutes"][:6]:
        print(
            f"  - {cand['candidate_ingredient']:22s} | in {cand['present_in_valid_variants']} egg-free variants "
            f"({cand['valid_variant_rate_pct']}% vs {cand['baseline_rate_pct']}% baseline, lift={cand['enrichment_lift']}x) "
            f"| cite: {cand['supporting_recipe_ids'][:3]}"
        )

    # ------------------------------------------------------------------
    # TEST 10: Task 3 Edge Case — Weak / Conflicting Evidence
    # ------------------------------------------------------------------
    section("10. Task 3 Edge Case: Weak/Conflicting Evidence (egg+dairy+gluten-free souffle)")
    t0 = time.perf_counter()
    ev_weak = db.find_substitution_evidence(
        dish_query="chocolate souffle",
        remove_ingredient="egg",
        exclude_restrictions=["dairy", "gluten"],
        limit_supporting=5,
    )
    print(f"Evidence: strength={ev_weak['evidence_strength']}, type={ev_weak['evidence_type']}, fallback={ev_weak['broad_fallback_used']}, counts={ev_weak['counts']} [{(time.perf_counter()-t0)*1000:.0f} ms]")
    print(f"Supporting local recipes found: {len(ev_weak['supporting_recipes'])}")

    db.close()


if __name__ == "__main__":
    main()
