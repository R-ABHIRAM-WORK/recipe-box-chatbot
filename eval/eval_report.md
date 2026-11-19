# Reproducible Evaluation Report

Generated: `2026-11-19T16:19:06.403274+00:00`  
Score: **7 / 7** conversations passed all automated grounding & accuracy checks.

## 1. Automated End-to-End Checks

| Case | Status | Verification Notes |
|---|---|---|
| Conversation 1 (Task 1): Dish Search → Dairy-Free Follow-up → Unavailable Nutrition/Time | **PASS** | Turn 1/2 search_recipes called, 5 valid dairy-free IDs cited, 0 invalid IDs, missing nutrition/cook time honestly disclosed=True. |
| Conversation 2 (Task 1): Full-Dataset Aggregate Stats & Tomato Fewest-Steps Ranking | **PASS** | Exact garlic+onion count matched=True, tomato mean/median matched=True (canonical=False, fts=True), top-5 tomato IDs cited=5, invalid IDs=0. |
| Conversation 3 (Task 2): Pantry Matching → Exclude Follow-up → Quantities & Unit Mismatch | **PASS** | All 3 turns invoked match_pantry; valid IDs=5/5/5; quantity unverified note present=True; invalid IDs=0. |
| Conversation 4 (Task 2 Edge Case): Hard-Constraint No-Match (Zero Silent Relaxation) | **PASS** | match_pantry called, no-match explicitly stated=True, violations labeled=True, invalid IDs=0. |
| Conversation 5 (Task 3): Evidence-Based Egg Substitution in Banana Bread (Local Dataset Only) | **PASS** | Local evidence tool called, valid local IDs cited=3, full rewrite=True, trade-offs=True, stayed local=True, invalid IDs=0. |
| Conversation 6 (Task 3): Replace Cream in Pasta Without Dairy (Local Evidence) | **PASS** | Local evidence tool called, valid local IDs cited=2, full rewrite=True, trade-offs=True, stayed local=True, invalid IDs=0. |
| Conversation 7 (Task 3 Edge Case): Weak/Conflicting Evidence (Egg+Dairy+Gluten-Free Soufflé) | **PASS** | Local evidence checked, honest weak/unsupported refusal=True, no web search=True, invalid IDs=0. |

## 2. One-Shot LLM (No Tools) vs. Our Grounded System

Below are live responses from the **exact same LLM** called *without* database tools, showing why plain one-shot prompting is ungrounded or inaccurate on dataset queries:

### oneshot_1_garlic_onion_stats
- **Prompt:** Using the Eight Portions Recipe Box dataset (~125,000 recipes from Allrecipes, Epicurious, and Food Network), give your best concrete numerical estimate (exact count and percentage) of how many recipes contain both garlic and onion.
- **SQLite Ground Truth (what our system returns):** Exact canonical: 9,933 / 122,964 (8.08%); Broad family: 23,806 / 122,964 (19.36%).
- **One-Shot LLM (`gemini:gemini-3.1-flash-lite`, no tools) Output:**

```text
Based on an analysis of the **Recipe1M+** dataset (the academic standard derived from the Eight Portions/Allrecipes/Epicurious/Food Network corpus), here is the concrete estimate for the co-occurrence of garlic and onion.

### The Estimate
*   **Exact Count:** Approximately **48,750 recipes**
*   **Percentage:** **39%**

### Contextual Breakdown
In this dataset of ~125,000 recipes, the combination of garlic and onion serves as the foundational "aromatic base" for a vast majority of savory dishes. 

*   **Why this number?** The dataset is heavily weighted toward Western-style cooking (Allrecipes and Food Network), where the *mirepoix* (onion) and garlic are the standard starting point for soups, stews, sauces, and roasts.
*   **The "Missing" 61%:** The remaining recipes that do not contain both typically fall into three categories:
    1.  **Baking/Desserts:** A significant portion of the dataset consists of cookies, cakes, and breads, which naturally exclude both.
    2.  **Breakfast/Light Fare:** Many smoothie, fruit-based, or simple egg recipes omit these aromatics.
    3.  **Specific Dietary/Cultural Constraints:** Recipes following strict Jain or certain Ayurvedic dietary guidelines (which exclude alliums) or specific minimalist preparations.

### Examples of Co-occurrence
In this dataset, you will find this pairing most frequently in:
*   **Pasta Sauces:** Almost every marinara or bolognese recipe in the set contains both.
*   **Chili and Stews:** These recipes almost universally rely on the onion-garlic base for depth of flavor.
*   **Roasted Meats:** Rubs and marinades for chicken and beef in the Allrecipes subset frequently pair onion powder/fresh onion with garlic powder/fresh garlic.

***Note on Methodology:** This estimate is derived from natural language processing (NLP) keyword matching across the ingredient lists of the Recipe1M+ corpus. Because some recipes use variations (e.g., "shallots" or "leeks" instead of onions, or "garlic salt" instead of garlic), the actual count of "allium-based" recipes is likely closer to 45–48%, but the strict "onion + garlic" keyword match consistently lands at 39%.*
```
- **Verified DB IDs in one-shot output:** `[]` | **Invalid/fabricated IDs:** `[]`

### oneshot_2_tomato_mean_median
- **Prompt:** Across recipes containing tomato in the Eight Portions Recipe Box dataset (~125,000 recipes), estimate the mean and median number of ingredients per recipe and name the exact 5 tomato recipes with the fewest instruction steps.
- **SQLite Ground Truth (what our system returns):** Analyzed 5,039 canonical-tomato recipes: mean=13.44, median=12. Top-5 fewest steps (1 step each): `fn:Cv.LzyOIGtSJN3dvLIBqPQo8ac.K97G` (Tomato Salsa), `ar:BqfGsVgk7WHPebuzDH3IIRDAo0Vy8Hy` (Refreshing Mango Chutney), `ar:/0wx00RefE0VHzlff/GBszObKmj63Pi` (Tomato Basil Smoothie), `fn:Ppx1bwn1nD7NBLgtVkk4T0QXMyFiAk2` (Lomi Tomato Relish), `fn:EWHbufOf9ClZGXSRSuzOyPPSitd4DCO` (Pico de Gallo).
- **One-Shot LLM (`gemini:gemini-3.1-flash-lite`, no tools) Output:**

```text
Based on an analysis of the Eight Portions Recipe Box dataset (which contains approximately 125,000 recipes), here are the estimates for tomato-based recipes and the specific recipes with the fewest instructions.

### Ingredient Count Estimates
For recipes containing "tomato" in the title or ingredient list:

*   **Mean number of ingredients:** ~9.4 ingredients per recipe.
*   **Median number of ingredients:** 8 ingredients per recipe.

*Note: These figures account for the standard cleaning of the dataset, where pantry staples like salt, pepper, and water are often included as distinct ingredients.*

---

### 5 Tomato Recipes with the Fewest Instruction Steps
These recipes contain only **1 instruction step** in the dataset, representing the simplest preparations (typically involving assembly or a single-action process).

| Recipe Name |
| :--- |
| **Tomato Salad** |
| **Tomato Juice** |
| **Tomato Slices** |
| **Tomato Wedges** |
| **Tomato and Onion Salad** |

***

**Methodology Note:**
*   **Filtering:** Recipes were filtered by searching for the string "tomato" (case-insensitive) within the ingredient list and title fields.
*   **Instruction Count:** The instruction count was derived by splitting the instruction text block by common delimiters (periods, newlines, or numbered list markers). Recipes with a single instruction step are those where the entire preparation is described in one continuous sentence or paragraph.
```
- **Verified DB IDs in one-shot output:** `[]` | **Invalid/fabricated IDs:** `[]`

## 3. What Worked, Limitations Observed, and Why

### What Worked Well
- **Deterministic SQL/Python Analytics:** Exact counts, percentages, means, medians, and step rankings run over the full filtered dataset (~125k recipes) in < 500 ms with zero LLM arithmetic guessing.
- **Explicit Python `SessionState`:** Carrying active filters/pantry/exclusions in code prevents multi-turn drift when users add follow-up constraints like *'I cannot have dairy'* or *'Exclude eggs too'*.
- **Conservative Normalization + Family Layer:** Keeping `canonical_name` strict (e.g., `tomato` vs `tomato paste`, `milk` vs `coconut milk`) avoids false matches while `broad_family` + raw-text guards enforce allergy exclusions safely.
- **Honest Uncertainty & No-Match Handling:** Missing attributes (nutrition, cook times), impossible hard constraints, and zero-evidence substitutions (egg+dairy+gluten-free soufflé) are refused cleanly instead of fabricated.

### Known Limitations & Edge Cases
- **Ingredient omissions in source instructions:** A few scraped recipes omit an ingredient from their `ingredients` array but mention it in `instructions` (e.g., *'melt butter'*). Our exclusion filter scans ingredients + normalized families, and the LLM checks instruction text when reviewing top results—scanning instruction text directly as an optional strict mode is a clean future extension.
- **Unicode accents in rare tokens:** Accented characters like `ñ` in `jalapeño` normalize with a space (`jalape o`) under ASCII regex stripping; adding `unicodedata.normalize('NFKD', ...)` before regex cleaning would polish this.
- **Cross-dimension unit conversions:** Converting weight (`200 g onions`) to count (`1 medium onion`) or volume (`1/4 cup`) is intentionally *not* guessed without density/item-weight tables; instead, the system explicitly reports a name-level match with quantity unverified.