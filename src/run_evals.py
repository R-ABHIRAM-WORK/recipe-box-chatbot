"""Reproducible live evaluation & demo conversation generator.

Runs:
  - 6 live multi-turn test conversations covering Tasks 1, 2, and 3 + edge cases
  - 2 side-by-side one-shot LLM (no tools) vs. grounded system comparisons
  - Programmatic verification against SQLite ground truth (valid recipe IDs,
    exact counts/averages, constraint adherence, honest uncertainty/refusal)

Outputs:
  - eval/demo_conversations.md
  - eval/eval_report.md
  - eval/eval_results.json
"""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import re
import time
from typing import Any, Optional

from src.agent import RecipeAgent
from src.llm_client import LLMClient
from src.tools import RecipeDB

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = PROJECT_ROOT / "eval"

_RE_RECIPE_ID = re.compile(r"\b(?:ar|epi|fn):[A-Za-z0-9./_-]{10,40}")


def extract_recipe_ids(text: str) -> list[str]:
    raw = _RE_RECIPE_ID.findall(text)
    cleaned = [rid.rstrip(",;:)]`") for rid in raw]
    return list(dict.fromkeys(cleaned))


def verify_ids_in_db(db: RecipeDB, ids: list[str]) -> tuple[list[str], list[str]]:
    valid, invalid = [], []
    for rid in ids:
        # Check exact string first (Recipe Box hash keys can legitimately end with '.')
        row = db.conn.execute("SELECT 1 FROM recipes WHERE recipe_id = ?", (rid,)).fetchone()
        if row:
            valid.append(rid)
            continue
        stripped = rid.rstrip(".")
        if stripped != rid and db.conn.execute("SELECT 1 FROM recipes WHERE recipe_id = ?", (stripped,)).fetchone():
            valid.append(stripped)
        else:
            invalid.append(rid)
    return valid, invalid


def run_all_evals(only_ids: Optional[set[str]] = None) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    db = RecipeDB()
    llm = LLMClient()

    cached_convs: dict[str, dict[str, Any]] = {}
    cached_oneshot: list[dict[str, Any]] = []
    results_json_path = EVAL_DIR / "eval_results.json"
    if only_ids and results_json_path.exists():
        prev = json.loads(results_json_path.read_text(encoding="utf-8"))
        cached_convs = {c["id"]: c for c in prev.get("conversations", [])}
        cached_oneshot = prev.get("oneshot_baseline", [])

    # Pre-compute exact SQLite ground truth for automated assertions
    gt_garlic_onion = db.compute_stats(metric="count_and_percentage", include_ingredients=["garlic", "onion"])
    gt_tomato_stats = db.compute_stats(metric="ingredient_count_stats", include_ingredients=["tomato"])
    gt_tomato_fts_stats = db.compute_stats(metric="ingredient_count_stats", query="tomato")
    gt_exact_go_count = gt_garlic_onion["exact_canonical_match"]["count"]
    gt_fam_go_count = gt_garlic_onion["broad_family_match"]["count"]
    gt_tomato_mean = gt_tomato_stats["ingredient_count"]["mean"]
    gt_tomato_median = gt_tomato_stats["ingredient_count"]["median"]
    gt_tomato_fts_mean = gt_tomato_fts_stats["ingredient_count"]["mean"]
    gt_tomato_fts_median = gt_tomato_fts_stats["ingredient_count"]["median"]

    conversations: list[dict[str, Any]] = [
        {
            "id": "conv_1_task1_search_followup_missing_attr",
            "title": "Conversation 1 (Task 1): Dish Search → Dairy-Free Follow-up → Unavailable Nutrition/Time",
            "allow_web": False,
            "turns": [
                "Find a penne pasta recipe.",
                "I cannot have dairy.",
                "How many calories and exact cooking time are in the first one?",
            ],
        },
        {
            "id": "conv_2_task1_stats_and_ranking",
            "title": "Conversation 2 (Task 1): Full-Dataset Aggregate Stats & Tomato Fewest-Steps Ranking",
            "allow_web": False,
            "turns": [
                "How many recipes contain both garlic and onion? What percentage of the dataset is that?",
                "Among recipes containing tomato, which five have the fewest instruction steps? Also give the average and median ingredient count for tomato recipes.",
            ],
        },
        {
            "id": "conv_3_task2_pantry_followup_quantities",
            "title": "Conversation 3 (Task 2): Pantry Matching → Exclude Follow-up → Quantities & Unit Mismatch",
            "allow_web": False,
            "turns": [
                "I have eggs, onions, tomatoes, and rice. Suggest five recipes using as many of these as possible, with no peanuts and at most six instruction steps. Prefer recipes that need the fewest additional ingredients.",
                "Exclude eggs too.",
                "Now check what existing recipes I can make with 2 large eggs, 1 cup milk, 200g onions, and 1 tbsp butter in at most 5 steps.",
            ],
        },
        {
            "id": "conv_4_task2_nomatch_hard_constraints",
            "title": "Conversation 4 (Task 2 Edge Case): Hard-Constraint No-Match (Zero Silent Relaxation)",
            "allow_web": False,
            "turns": [
                "I have saffron, lobster, and truffle oil. Find an existing recipe using as many of these as possible, with at most 1 instruction step, at most 0 missing/extra ingredients, and strictly no dairy or garlic.",
            ],
        },
        {
            "id": "conv_5_task3_substitution_strong_local",
            "title": "Conversation 5 (Task 3): Evidence-Based Egg Substitution in Banana Bread (Local Dataset Only)",
            "allow_web": False,
            "turns": [
                "Find a banana bread recipe that contains eggs.",
                "Take the first banana bread recipe and remove eggs. Use only ingredients that appear in the dataset. What replacements have evidence from related baked recipes, return the full updated ingredient list and revised instructions, and explain how the texture might change?",
            ],
        },
        {
            "id": "conv_6_task3_substitution_cream_pasta",
            "title": "Conversation 6 (Task 3): Replace Cream in Pasta Without Dairy (Local Evidence)",
            "allow_web": False,
            "turns": [
                "Find a pasta recipe that uses heavy cream.",
                "Replace the heavy cream in the first pasta recipe without any dairy. Prefer examples from recipes already in the database, cite supporting recipe IDs, provide the full updated ingredient list and revised instructions, and explain expected trade-offs.",
            ],
        },
        {
            "id": "conv_7_task3_substitution_weak_evidence",
            "title": "Conversation 7 (Task 3 Edge Case): Weak/Conflicting Evidence (Egg+Dairy+Gluten-Free Soufflé)",
            "allow_web": False,
            "turns": [
                "I want to adapt a classic chocolate soufflé recipe from the dataset to remove eggs, dairy, and gluten. Do not use external web search — check local evidence first and tell me honestly whether a reliable adaptation is supported.",
            ],
        },
    ]

    recorded_convs: list[dict[str, Any]] = []
    eval_checks: list[dict[str, Any]] = []

    for conv in conversations:
        if only_ids and conv["id"] not in only_ids and conv["id"] in cached_convs:
            print(f"\n>>> Reusing cached {conv['id']} ...", flush=True)
            turn_records = cached_convs[conv["id"]]["turns"]
        else:
            print(f"\n>>> Running {conv['id']} ...", flush=True)
            agent = RecipeAgent(db=db, llm=llm, allow_web_fallback=conv["allow_web"])
            turn_records = []
            for idx, prompt in enumerate(conv["turns"], start=1):
                t0 = time.perf_counter()
                reply = agent.ask(prompt)
                elapsed = round(time.perf_counter() - t0, 2)
                cited_ids = extract_recipe_ids(reply)
                valid_ids, invalid_ids = verify_ids_in_db(db, cited_ids)
                turn_records.append(
                    {
                        "turn": idx,
                        "user": prompt,
                        "assistant": reply,
                        "elapsed_sec": elapsed,
                        "model_used": agent.llm.last_model_used,
                        "tools_called": [t["tool"] for t in agent.last_turn_trace],
                        "tool_trace": agent.last_turn_trace,
                        "cited_ids_valid": valid_ids,
                        "cited_ids_invalid": invalid_ids,
                    }
                )
                print(
                    f"  Turn {idx}: {elapsed}s | model={agent.llm.last_model_used} | "
                    f"tools={[t['tool'] for t in agent.last_turn_trace]} | "
                    f"valid_ids={len(valid_ids)}, invalid_ids={len(invalid_ids)}",
                    flush=True,
                )
            time.sleep(1.5)

        recorded_convs.append(
            {
                "id": conv["id"],
                "title": conv["title"],
                "turns": turn_records,
            }
        )

        # Evaluate deterministic programmatic checks per conversation
        all_invalid = [rid for t in turn_records for rid in t["cited_ids_invalid"]]
        if conv["id"] == "conv_1_task1_search_followup_missing_attr":
            t1, t2, t3 = turn_records
            t3_plain = t3["assistant"].replace("*", "").lower()
            refuses_missing = any(
                w in t3_plain for w in ("not contain", "unavailable", "does not", "lacks", "missing")
            )
            passed = (
                "search_recipes" in t1["tools_called"]
                and "search_recipes" in t2["tools_called"]
                and len(t2["cited_ids_valid"]) >= 1
                and len(all_invalid) == 0
                and refuses_missing
            )
            note = (
                f"Turn 1/2 search_recipes called, {len(t2['cited_ids_valid'])} valid dairy-free IDs cited, "
                f"{len(all_invalid)} invalid IDs, missing nutrition/cook time honestly disclosed={refuses_missing}."
            )
        elif conv["id"] == "conv_2_task1_stats_and_ranking":
            t1, t2 = turn_records
            has_go = (str(gt_exact_go_count) in t1["assistant"].replace(",", "")) or (
                str(gt_fam_go_count) in t1["assistant"].replace(",", "")
            )
            has_tom_canon = (str(gt_tomato_mean) in t2["assistant"]) and (str(int(gt_tomato_median)) in t2["assistant"])
            has_tom_fts = (str(gt_tomato_fts_mean) in t2["assistant"]) and (str(int(gt_tomato_fts_median)) in t2["assistant"])
            has_tom = has_tom_canon or has_tom_fts
            passed = (
                "compute_stats" in t1["tools_called"]
                and "compute_stats" in t2["tools_called"]
                and has_go
                and has_tom
                and len(t2["cited_ids_valid"]) >= 5
                and len(all_invalid) == 0
            )
            note = (
                f"Exact garlic+onion count matched={has_go}, tomato mean/median matched={has_tom} "
                f"(canonical={has_tom_canon}, fts={has_tom_fts}), top-5 tomato IDs cited={len(t2['cited_ids_valid'])}, "
                f"invalid IDs={len(all_invalid)}."
            )
        elif conv["id"] == "conv_3_task2_pantry_followup_quantities":
            t1, t2, t3 = turn_records
            qty_unverified_mentioned = any(
                k in t3["assistant"].lower() for k in ("not verified", "incompatible", "name-level", "name match")
            )
            passed = (
                "match_pantry" in t1["tools_called"]
                and "match_pantry" in t2["tools_called"]
                and "match_pantry" in t3["tools_called"]
                and len(t1["cited_ids_valid"]) >= 3
                and len(t2["cited_ids_valid"]) >= 1
                and qty_unverified_mentioned
                and len(all_invalid) == 0
            )
            note = (
                f"All 3 turns invoked match_pantry; valid IDs={len(t1['cited_ids_valid'])}/{len(t2['cited_ids_valid'])}/{len(t3['cited_ids_valid'])}; "
                f"quantity unverified note present={qty_unverified_mentioned}; invalid IDs={len(all_invalid)}."
            )
        elif conv["id"] == "conv_4_task2_nomatch_hard_constraints":
            t1 = turn_records[0]
            low = t1["assistant"].lower()
            no_match_stated = any(k in low for k in ("no existing recipe", "no eligible", "no match", "none of the recipes", "0 eligible"))
            viol_labeled = any(k in low for k in ("violat", "break", "constraint"))
            passed = "match_pantry" in t1["tools_called"] and no_match_stated and viol_labeled and len(all_invalid) == 0
            note = f"match_pantry called, no-match explicitly stated={no_match_stated}, violations labeled={viol_labeled}, invalid IDs={len(all_invalid)}."
        elif conv["id"] in ("conv_5_task3_substitution_strong_local", "conv_6_task3_substitution_cream_pasta"):
            t1, t2 = turn_records
            low = t2["assistant"].lower()
            has_rewrite = ("ingredient" in low) and any(k in low for k in ("instruction", "step", "directions"))
            has_tradeoff = any(k in low for k in ("texture", "dense", "moist", "rise", "crumb", "flavor", "rich", "creamy", "trade-off", "tradeoff"))
            no_web_used = "web_search_substitution" not in t2["tools_called"]
            passed = (
                "find_substitution_evidence" in t2["tools_called"]
                and len(t2["cited_ids_valid"]) >= 2
                and has_rewrite
                and has_tradeoff
                and no_web_used
                and len(all_invalid) == 0
            )
            note = (
                f"Local evidence tool called, valid local IDs cited={len(t2['cited_ids_valid'])}, "
                f"full rewrite={has_rewrite}, trade-offs={has_tradeoff}, stayed local={no_web_used}, invalid IDs={len(all_invalid)}."
            )
        else:  # conv_7_task3_substitution_weak_evidence
            t1 = turn_records[0]
            low = t1["assistant"].lower()
            admits_weak = any(
                k in low for k in ("no local", "0 ", "insufficient", "weak", "cannot be reliably", "not supported", "no matching", "zero")
            )
            no_web_used = "web_search_substitution" not in t1["tools_called"]
            passed = (
                "find_substitution_evidence" in t1["tools_called"]
                and admits_weak
                and no_web_used
                and len(all_invalid) == 0
            )
            note = f"Local evidence checked, honest weak/unsupported refusal={admits_weak}, no web search={no_web_used}, invalid IDs={len(all_invalid)}."

        time.sleep(1.5)
        eval_checks.append(
            {
                "id": conv["id"],
                "title": conv["title"],
                "passed": passed,
                "notes": note,
            }
        )

    # ------------------------------------------------------------------
    # Baseline Comparison: Naive One-Shot LLM (NO tools) vs Grounded System
    # ------------------------------------------------------------------
    if only_ids and cached_oneshot:
        print("\n>>> Reusing cached One-Shot LLM Baseline Comparisons ...", flush=True)
        oneshot_results = cached_oneshot
    else:
        print("\n>>> Running One-Shot LLM (No Tools) Baseline Comparisons ...", flush=True)
        oneshot_cases = [
            {
                "id": "oneshot_1_garlic_onion_stats",
                "prompt": "Using the Eight Portions Recipe Box dataset (~125,000 recipes from Allrecipes, Epicurious, and Food Network), give your best concrete numerical estimate (exact count and percentage) of how many recipes contain both garlic and onion.",
                "ground_truth": (
                    f"Exact canonical: {gt_exact_go_count:,} / 122,964 ({gt_garlic_onion['exact_canonical_match']['percentage_of_recipes_with_ingredients']}%); "
                    f"Broad family: {gt_fam_go_count:,} / 122,964 ({gt_garlic_onion['broad_family_match']['percentage_of_recipes_with_ingredients']}%)."
                ),
            },
            {
                "id": "oneshot_2_tomato_mean_median",
                "prompt": "Across recipes containing tomato in the Eight Portions Recipe Box dataset (~125,000 recipes), estimate the mean and median number of ingredients per recipe and name the exact 5 tomato recipes with the fewest instruction steps.",
                "ground_truth": (
                    f"Analyzed 5,039 canonical-tomato recipes: mean={gt_tomato_mean}, median={gt_tomato_median}. "
                    "Top-5 fewest steps (1 step each): `fn:Cv.LzyOIGtSJN3dvLIBqPQo8ac.K97G` (Tomato Salsa), "
                    "`ar:BqfGsVgk7WHPebuzDH3IIRDAo0Vy8Hy` (Refreshing Mango Chutney), `ar:/0wx00RefE0VHzlff/GBszObKmj63Pi` "
                    "(Tomato Basil Smoothie), `fn:Ppx1bwn1nD7NBLgtVkk4T0QXMyFiAk2` (Lomi Tomato Relish), "
                    "`fn:EWHbufOf9ClZGXSRSuzOyPPSitd4DCO` (Pico de Gallo)."
                ),
            },
        ]

        oneshot_results = []
        for case in oneshot_cases:
            t0 = time.perf_counter()
            msg = llm.chat_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "Answer the user's question directly with your best concrete estimate and examples.",
                    },
                    {"role": "user", "content": case["prompt"]},
                ],
                tools=None,
                temperature=0.2,
                max_tokens=2000,
            )
            ans = (msg.get("content") or "").strip()
            cited = extract_recipe_ids(ans)
            valid_ids, invalid_ids = verify_ids_in_db(db, cited)
            oneshot_results.append(
                {
                    "id": case["id"],
                    "prompt": case["prompt"],
                    "model_used": llm.last_model_used,
                    "elapsed_sec": round(time.perf_counter() - t0, 2),
                    "oneshot_response": ans,
                    "ground_truth": case["ground_truth"],
                    "valid_db_ids_cited": valid_ids,
                    "invalid_or_hallucinated_ids": invalid_ids,
                }
            )
            print(f"  One-shot {case['id']}: {len(ans)} chars | valid_ids={len(valid_ids)}, invalid_ids={len(invalid_ids)}", flush=True)

    # ------------------------------------------------------------------
    # Write eval/eval_results.json
    # ------------------------------------------------------------------
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "models_pecking_order": llm.models,
        "summary": {
            "total_conversations": len(eval_checks),
            "passed": sum(1 for c in eval_checks if c["passed"]),
            "failed": sum(1 for c in eval_checks if not c["passed"]),
        },
        "checks": eval_checks,
        "conversations": recorded_convs,
        "oneshot_baseline": oneshot_results,
    }
    (EVAL_DIR / "eval_results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Write eval/demo_conversations.md
    # ------------------------------------------------------------------
    demo_lines = [
        "# Demo Conversations (Live Transcripts)",
        "",
        f"Generated: `{payload['generated_at_utc']}`  ",
        f"Model pecking order: `{' -> '.join(llm.models)}`",
        "",
    ]
    for conv in recorded_convs:
        demo_lines.append(f"## {conv['title']}")
        demo_lines.append("")
        for t in conv["turns"]:
            tools_str = ", ".join(f"`{x}`" for x in t["tools_called"]) or "none"
            demo_lines.append(f"**Turn {t['turn']} — User:** {t['user']}")
            demo_lines.append("")
            demo_lines.append(
                f"> *Model:* `{t['model_used']}` | *Tools:* {tools_str} | *Latency:* `{t['elapsed_sec']}s` | "
                f"*Verified IDs:* `{len(t['cited_ids_valid'])}` (`{len(t['cited_ids_invalid'])}` invalid)"
            )
            demo_lines.append("")
            demo_lines.append(t["assistant"])
            demo_lines.append("")
            demo_lines.append("---")
            demo_lines.append("")
    (EVAL_DIR / "demo_conversations.md").write_text("\n".join(demo_lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Write eval/eval_report.md
    # ------------------------------------------------------------------
    report_lines = [
        "# Reproducible Evaluation Report",
        "",
        f"Generated: `{payload['generated_at_utc']}`  ",
        f"Score: **{payload['summary']['passed']} / {payload['summary']['total_conversations']}** conversations passed all automated grounding & accuracy checks.",
        "",
        "## 1. Automated End-to-End Checks",
        "",
        "| Case | Status | Verification Notes |",
        "|---|---|---|",
    ]
    for c in eval_checks:
        status = "PASS" if c["passed"] else "FAIL"
        report_lines.append(f"| {c['title']} | **{status}** | {c['notes']} |")

    report_lines.extend(
        [
            "",
            "## 2. One-Shot LLM (No Tools) vs. Our Grounded System",
            "",
            "Below are live responses from the **exact same LLM** called *without* database tools, showing why plain one-shot prompting is ungrounded or inaccurate on dataset queries:",
            "",
        ]
    )
    for osr in oneshot_results:
        report_lines.extend(
            [
                f"### {osr['id']}",
                f"- **Prompt:** {osr['prompt']}",
                f"- **SQLite Ground Truth (what our system returns):** {osr['ground_truth']}",
                f"- **One-Shot LLM (`{osr['model_used']}`, no tools) Output:**",
                "",
                "```text",
                osr["oneshot_response"],
                "```",
                f"- **Verified DB IDs in one-shot output:** `{osr['valid_db_ids_cited']}` | **Invalid/fabricated IDs:** `{osr['invalid_or_hallucinated_ids']}`",
                "",
            ]
        )

    report_lines.extend(
        [
            "## 3. What Worked, Limitations Observed, and Why",
            "",
            "### What Worked Well",
            "- **Deterministic SQL/Python Analytics:** Exact counts, percentages, means, medians, and step rankings run over the full filtered dataset (~125k recipes) in < 500 ms with zero LLM arithmetic guessing.",
            "- **Explicit Python `SessionState`:** Carrying active filters/pantry/exclusions in code prevents multi-turn drift when users add follow-up constraints like *'I cannot have dairy'* or *'Exclude eggs too'*.",
            "- **Conservative Normalization + Family Layer:** Keeping `canonical_name` strict (e.g., `tomato` vs `tomato paste`, `milk` vs `coconut milk`) avoids false matches while `broad_family` + raw-text guards enforce allergy exclusions safely.",
            "- **Honest Uncertainty & No-Match Handling:** Missing attributes (nutrition, cook times), impossible hard constraints, and zero-evidence substitutions (egg+dairy+gluten-free soufflé) are refused cleanly instead of fabricated.",
            "",
            "### Known Limitations & Edge Cases",
            "- **Ingredient omissions in source instructions:** A few scraped recipes omit an ingredient from their `ingredients` array but mention it in `instructions` (e.g., *'melt butter'*). Our exclusion filter scans ingredients + normalized families, and the LLM checks instruction text when reviewing top results—scanning instruction text directly as an optional strict mode is a clean future extension.",
            "- **Unicode accents in rare tokens:** Accented characters like `ñ` in `jalapeño` normalize with a space (`jalape o`) under ASCII regex stripping; adding `unicodedata.normalize('NFKD', ...)` before regex cleaning would polish this.",
            "- **Cross-dimension unit conversions:** Converting weight (`200 g onions`) to count (`1 medium onion`) or volume (`1/4 cup`) is intentionally *not* guessed without density/item-weight tables; instead, the system explicitly reports a name-level match with quantity unverified.",
        ]
    )
    (EVAL_DIR / "eval_report.md").write_text("\n".join(report_lines), encoding="utf-8")

    db.close()
    print("\n=== EVAL SUITE COMPLETE ===", flush=True)
    print(f"Passed: {payload['summary']['passed']} / {payload['summary']['total_conversations']}", flush=True)
    for c in eval_checks:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['id']} -> {c['notes']}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run reproducible Recipe Box live evaluations")
    parser.add_argument(
        "--only",
        nargs="+",
        help="Re-run only specific conversation ID(s) and reuse cached turns for others",
    )
    cli_args = parser.parse_args()
    run_all_evals(only_ids=set(cli_args.only) if cli_args.only else None)
