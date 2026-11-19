# AI Collaboration Brief & Engineering Log

This document summarizes the design discussions, architectural trade-offs, data-quality findings, and debugging iterations from building the **Recipe Box Grounded Chatbot** with AI coding assistance (Antigravity).

---

## 1. Architecture Discussion: Deterministic SQL Tools vs. Plain RAG
- **Design question:** Should the system use plain vector RAG (embed recipes → top-k retrieval → LLM) or a structured relational database behind LLM tool calls?
- **Decision:** Pure top-k vector retrieval breaks on two core requirements:
  1. **Full-dataset statistics (Task 1):** Analytical questions (*"How many recipes contain both garlic and onion?"*, *"Which 5 tomato recipes have the fewest steps?"*) must scan the **entire** filtered subset (~125k recipes), not a small top-k sample.
  2. **Hard constraints & pantry scoring (Task 2):** Allergies (*no peanuts*, *no dairy*) and step limits (*≤ 6 steps*) must be enforced deterministically without fuzzy approximation.
- **Outcome:** Built an offline ETL into **SQLite + FTS5** (`data/recipes.db`) exposed via **5 deterministic SQL/Python tools** (`search_recipes`, `get_recipe`, `compute_stats`, `match_pantry`, `find_substitution_evidence`) plus a strictly gated secondary web fallback (`web_search_substitution`) for Task 3 when local evidence is weak.

---

## 2. Offline Preprocessing vs. Query-Time (JIT) Parsing
- **Decision:** Parse, clean, and index all 3 raw JSON files (~195 MB, 125,164 entries, ~1.29M ingredient rows) **once upfront** via `src/build_db.py` rather than cleaning raw JSON on the fly.
- **Consistency:** User inputs at chat time (e.g., `"2 large eggs, 200g onions"`) pass through the **exact same `src/normalizer.py` functions** used during ingestion so user text and database records normalize identically.

---

## 3. Conservative Two-Level Normalization & Quantity Sufficiency
- **Key concern:** Aggressive normalization can collapse distinct culinary items (e.g., *long brinjal* vs. *brinjal*, *tomato paste* vs. *tomato*, *coconut milk* vs. dairy *milk*).
- **Design:**
  - `raw_text`: preserves the original ingredient wording (minus Allrecipes `ADVERTISEMENT` artifacts).
  - `canonical_name`: deterministic rule-based cleaning that removes quantities, units, prep notes, and generic modifiers (`large`, `fresh`, `chopped`), singularizes head nouns, and maps only strict synonyms (`scallions` → `green onion`) while preserving distinct varieties and product forms.
  - `broad_family`: tags allergen/produce groups (`dairy`, `peanut`, `tree_nut`, `egg`, `shellfish`, `soy`, `tomato`, `onion`, `garlic`) with explicit non-dairy exceptions (`coconut milk`, `almond milk`, `peanut butter`, `cream of tartar`, `butternut`) plus FTS5 raw-text safety checks on hard exclusions.
- **Quantity checks:** Extracted `quantity`, `qty_max`, `unit`, `unit_type` (`volume`/`weight`/`count`/`other`/`unspecified`), and `quantity_base` (`ml`/`g`/`count`). Comparisons are performed only within compatible dimensions; cross-dimension pairs (e.g., `200g onions` vs. `1 medium onion` or `1/4 cup`) are explicitly reported as **`name_match_only (quantity not verified)`**.

---

## 4. Database-First Testing & Data-Quality Audit
Before integrating an LLM, we wrote `src/test_tools.py` and audited all 1.29M ingredient rows in `data/recipes.db`, resolving 5 real-world dataset issues:
1. **Allrecipes spam lines:** Stripped `"ADVERTISEMENT"` suffixes and standalone `"ADVERTISEMENT"` entries across 39,519 Allrecipes records.
2. **Epicurious non-ingredient rows:** Filtered out `Special equipment:` lines, `*Available at specialty foods shops...` footnotes, HTML fragments (`/em`), and bare numbers (~3,650 garbage rows removed; unique canonicals reduced from `86,776` → `76,556`).
3. **Flavor-list & `"or ..."` clauses:** Guarded comma-splitting so `"1 (1/2-gallon) vanilla, mint chip, or coffee ice cream"` no longer mapped to `"vanilla extract"` (and tagged as `dairy`), and trimmed alternative `" or ..."` clauses cleanly.
4. **Leading-adjective commas:** Fixed lines like `"4 skinless, boneless chicken breast halves"` so commas between noise adjectives do not truncate the ingredient name.
5. **Scraped QA/test records & duplicates:** Flagged 10 Food Network test records (`QA`, `Tst` ×7, `Tstcgdgdgdgdgdgdgd`, `ttt`) via `is_junk=1` and 1,625 exact `(title, ingredients)` duplicates via `is_duplicate=1` without deleting raw records, leaving `121,368` clean, unique complete recipes for search and matching.

---

## 5. Multi-Turn Reliability & Provider Resilience
- **Explicit Python `SessionState`:** Active search filters, pantry lists, hard exclusions, step caps, and recently shown recipe IDs are maintained in Python and injected into the system prompt each turn so follow-up restrictions (*"I cannot have dairy"*, *"Exclude eggs too"*) persist reliably across turns.
- **Zero external dependencies:** Implemented the full pipeline (ETL, SQLite/FTS5, HTTP client, `.env` loader, CLI, and evaluation harness) using only the Python 3.10+ standard library for portable local setup.
- **Free-tier rate-limit engineering:** Live testing surfaced OpenRouter's 50 req/day cap and Groq's 8,000 TPM minute window. We addressed both in `src/llm_client.py` and `src/agent.py` via:
  - Automatic multi-provider fallback across **Google AI Studio (`gemini-3.1-flash-lite`)**, **Groq (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-20b`)**, and **OpenRouter (`nemotron-3-ultra`, `gemma-4-31b`)**.
  - Automatic parsing of `"try again in Xs"` backoff hints on per-minute `HTTP 429` responses.
  - Prompt history compaction for older turns (relying on `SessionState` for exact active IDs/filters) and sufficient `max_tokens` headroom so reasoning models never truncate full Task 3 recipe rewrites.

---

## 6. Verification Results
- **Offline deterministic tool suite (`src/test_tools.py`):** 10/10 cases passing.
- **Live end-to-end evaluation (`src/run_evals.py`):** **7 / 7 conversations PASS** across Tasks 1–3 and edge cases with **0 invalid/fabricated recipe IDs**.
- **One-shot baseline comparison (`eval/eval_report.md`):** Verified concrete failure modes of ungrounded prompting—the same LLM without tools estimated `48,750 / 39%` for garlic+onion recipes (vs. exact SQLite `9,933 / 8.08%` canonical and `23,806 / 19.36%` family) and `mean ~9.4, median 8` for tomato recipes (vs. exact SQLite `mean 13.44–13.57, median 12`).
