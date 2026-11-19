# AI Collaboration Brief & Design Discussion Summary

This document summarizes the key architectural discussions, engineering trade-offs, data-quality findings, and debugging iterations made while building the **Recipe Box Grounded Chatbot** with AI coding assistance (Antigravity).

---

## 1. Core Architectural Decision: Tool-Using SQL Engine vs. Plain RAG
- **Initial question:** Should this be a plain vector-RAG chatbot (embed recipes → top-k retrieval → LLM) or a structured database with LLM tool calling?
- **Conclusion:** Plain top-k RAG fails on two core assignment requirements:
  1. **Dataset-wide statistics (Task 1):** Queries like *"How many recipes contain both garlic and onion?"* or *"Which 5 tomato recipes have the fewest steps?"* must evaluate the **entire** filtered dataset (~125k recipes), not top-5 semantic chunks.
  2. **Hard constraints & pantry scoring (Task 2):** Allergies (*no peanuts*, *no dairy*) and step limits (*≤ 6 steps*) cannot rely on fuzzy similarity; they must be strictly enforced in deterministic code.
- **Final architecture:** Offline ETL into **SQLite + FTS5** (`data/recipes.db`) exposed through **5 deterministic Python/SQL tools** (`search_recipes`, `get_recipe`, `compute_stats`, `match_pantry`, `find_substitution_evidence`) plus a gated secondary web fallback (`web_search_substitution`) for Task 3.

---

## 2. Offline Cleaning vs. Just-In-Time (JIT) Parsing
- **Decision:** Clean and index all 3 raw JSON files (~195 MB, 125,164 entries, ~1.29M ingredient rows) **once upfront** via `src/build_db.py` rather than parsing raw JSON at query time.
- **Query-time consistency:** Only the user's short query/pantry input is parsed at runtime, using the **exact same `src/normalizer.py` functions** used during ingestion.

---

## 3. Safe, Deterministic Ingredient Normalization & Quantity Parsing
- **Key concern raised during design:** Over-aggressive normalization can destroy real culinary distinctions (e.g., collapsing *long brinjal* into *brinjal*, *tomato paste* into *tomato*, or *coconut milk* into dairy *milk*).
- **Solution — Two-level conservative schema:**
  - `raw_text`: original wording preserved (minus Allrecipes `ADVERTISEMENT` noise).
  - `canonical_name`: deterministic Python pipeline that strips quantities, units, prep actions, and generic size words (`large`, `fresh`, `chopped`), singularizes head nouns, and maps only strict synonyms (`scallions` → `green onion`) while preserving distinct varieties and forms.
  - `broad_family`: allergen/produce groups (`dairy`, `peanut`, `tree_nut`, `egg`, `shellfish`, `soy`, `tomato`, `onion`, `garlic`) with explicit non-dairy exception guards (`coconut milk`, `almond milk`, `peanut butter`, `cream of tartar`, `butternut`) plus FTS5 raw-text safety checks for hard exclusions.
- **Quantity sufficiency:** Extracted `quantity`, `qty_max`, `unit`, `unit_type` (`volume`/`weight`/`count`/`other`/`unspecified`), and `quantity_base` (`ml`/`g`/`count`). Cross-dimension conversions (e.g., `200g onions` vs `1 medium onion` or `1/4 cup`) are **never guessed**; they are explicitly reported as **`name_match_only (quantity not verified)`**.

---

## 4. Data-Quality Audit Findings (Fixed Before Adding the LLM)
We built and tested the deterministic tools first (`src/test_tools.py`) and audited all 1.29M rows of `data/recipes.db`, uncovering and fixing 5 real quirks in the raw Recipe Box dataset:
1. **Allrecipes spam lines:** Nearly every Allrecipes record (39,519) contained `"ADVERTISEMENT"` appended to ingredients and standalone `"ADVERTISEMENT"` entries.
2. **Epicurious non-ingredient lines:** `Special equipment:` entries, `*Available at specialty foods shops...` footnotes, HTML fragments (`/em`), and bare numbers were stripped (~3,650 garbage rows removed; unique canonicals dropped from `86,776` → `76,556`).
3. **Flavor-list & `"or ..."` bugs:** Fixed comma-splitting so `"1 (1/2-gallon) vanilla, mint chip, or coffee ice cream"` no longer mis-mapped to `"vanilla extract"` (and properly tagged as `dairy`), and trimmed alternative `" or ..."` clauses cleanly.
4. **Leading-adjective comma bug:** Fixed `"4 skinless, boneless chicken breast halves"` so commas between pure noise words no longer truncate the core ingredient.
5. **Food Network QA/test junk & duplicates:** Flagged 10 scraped QA/test entries (`QA` with `"method method method"`, `Tst` ×7, `Tstcgdgdgdgdgdgdgd`, `ttt`) via `is_junk=1` and 1,625 exact `(title, ingredients)` duplicates via `is_duplicate=1` while preserving raw-dataset traceability (`121,368` clean unique complete recipes).

---

## 5. Multi-Turn Reliability & Free-Tier LLM Engineering
- **Explicit Python `SessionState`:** Stored active search filters, pantry list, hard exclusions, step limits, and recently shown recipe IDs in Python (injected into the system prompt every turn) so follow-ups like *"I cannot have dairy"* or *"Exclude eggs too"* never drop prior state—even on lightweight free models.
- **Zero external dependencies:** Implemented the entire project (ETL, SQLite/FTS5, OpenAI-compatible HTTP client, `.env` loader, CLI, and eval runner) using only the Python 3.10+ standard library so the interviewer can run it without `pip install` friction.
- **Free-tier rate-limit resilience:** During live evaluation, we hit OpenRouter's 50 req/day free limit and Groq's 8,000 TPM minute window. We solved both cleanly in `src/llm_client.py` and `src/agent.py` by:
  - Supporting automatic multi-provider fallback across **Google AI Studio (`gemini-3.1-flash-lite`)**, **Groq (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`)**, and **OpenRouter (`nemotron-3-ultra`, `gemma-4-31b`)**.
  - Parsing provider `"try again in Xs"` headers for automatic TPM backoff.
  - Compacting older assistant turns in prompt history (since `SessionState` already holds exact structured filters & IDs) and increasing `max_tokens` so reasoning models don't truncate full Task 3 recipe rewrites.

---

## 6. Evaluation & Verification Summary
- **Deterministic tool tests (`src/test_tools.py`):** 10/10 cases passing offline in milliseconds to <1s.
- **Live end-to-end eval suite (`src/run_evals.py`):** **7 / 7 conversations PASS** across Tasks 1–3 and edge cases with **0 invalid/hallucinated recipe IDs** across all turns.
- **One-shot LLM baseline proof (`eval/eval_report.md`):** Demonstrated concrete hallucinations when the same LLM is called without tools (e.g., guessing `48,750 / 39%` for garlic+onion recipes vs. SQLite ground truth `9,933 / 8.08%` canonical and `23,806 / 19.36%` broad family; guessing `mean ~9.4, median 8` for tomato recipes vs. actual `mean 13.44–13.57, median 12`).

---

## 7. Quick Interview Code-Walkthrough Map
- **Data cleaning & normalization:** `src/normalizer.py` (`parse_ingredient_line`, `normalize_ingredient_name`, `classify_broad_family`, `split_instructions`)
- **Database build & schema:** `src/build_db.py` (`SCHEMA_SQL`, `INDEX_SQL`, `build_database`)
- **All 5 deterministic tools:** `src/tools.py` (`RecipeDB.search_recipes`, `get_recipe`, `compute_stats`, `match_pantry`, `find_substitution_evidence`)
- **Multi-turn agent & state:** `src/agent.py` (`SessionState`, `SYSTEM_PROMPT`, `RecipeAgent._execute_tool`, `RecipeAgent.ask`)
- **Multi-provider LLM client:** `src/llm_client.py` (`LLMClient._build_routes_from_env`, `chat_completion`)
- **CLI & reproducible evals:** `src/chat.py`, `src/run_evals.py`
