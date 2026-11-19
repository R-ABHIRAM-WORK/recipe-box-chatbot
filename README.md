# Recipe Box Grounded Chatbot

A multi-turn conversational recipe chatbot built over the full **Eight Portions Recipe Box** dataset (~125,000 recipes across Allrecipes, Epicurious, and Food Network).

**Core design principle:** Deterministic SQL/Python tools handle every factual lookup, dataset statistic, hard-constraint filter, quantity comparison, and local substitution evidence search. The LLM handles natural-language understanding, tool orchestration, and grounded recipe adaptation—never guessing counts, IDs, or unavailable attributes.

---

## Quick Start (Zero External Dependencies — Pure Python 3.10+ Stdlib)

No `pip install` is required; everything runs on the Python 3.10+ standard library (`sqlite3`, `json`, `re`, `urllib`).

### 1. Download all 3 dataset JSON files
```bash
python -m src.download_data
```
Downloads and extracts `recipes_raw_nosource_ar.json`, `recipes_raw_nosource_epi.json`, and `recipes_raw_nosource_fn.json` into `data/raw/`.

### 2. Build the cleaned SQLite database
```bash
python -m src.build_db
```
Creates `data/recipes.db` (~124,595 valid recipes, ~1.29M parsed ingredient rows, SQL indexes, `ingredients_summary`, and an FTS5 full-text index). Takes ~2 minutes once offline.

*(Optional)* Verify the database & deterministic tools without needing an API key:
```bash
python -m src.verify_db
python -m src.test_tools
```

### 3. Configure an API key (free tier works — Google AI Studio, Groq, or OpenRouter)
Copy `.env.example` to `.env` and add **at least one** free API key (`GEMINI_API_KEY` from Google AI Studio, `GROQ_API_KEY`, or `OPENROUTER_API_KEY`). The client automatically falls back across whichever providers/models are configured if one hits a rate limit:
```bash
cp .env.example .env
```

### 4. Run the interactive multi-turn chatbot
```bash
python -m src.chat
# Show tool calls, model used, and latency per turn:
python -m src.chat --debug
# Disable optional Task 3 external web fallback completely:
python -m src.chat --no-web
```
CLI commands inside chat: `/state` (inspect active session filters), `/reset`, `/debug`, `/quit`.

### 5. Re-run the reproducible live evaluation suite
```bash
python -m src.run_evals
```
Generates updated `eval/demo_conversations.md`, `eval/eval_report.md`, and `eval/eval_results.json` (including 6 multi-turn conversations and 2 side-by-side one-shot LLM comparisons).

---

## Architecture Overview

```text
User Turn ─▶ RecipeAgent (src/agent.py)
               ├─ Explicit Python SessionState (active query, pantry, hard excludes, max_steps, active recipe)
               ├─ OpenAI-compatible Tool-Calling LLM (src/llm_client.py, pecking-order fallback on 429/5xx)
               └─ Deterministic Tools (src/tools.py) ─▶ SQLite + FTS5 (data/recipes.db)
                    1. search_recipes             (FTS5 BM25 + title boost + strict filters)
                    2. get_recipe                 (exact ingredients, steps, explicit unavailable attributes)
                    3. compute_stats              (full-dataset counts, %, mean/median, top ings, step rankings)
                    4. match_pantry               (hard constraints, quantity checks, explainable score, no-match guard)
                    5. find_substitution_evidence (local dataset variants + enrichment lift + recipe_id citations)
                    6. web_search_substitution    (gated optional secondary fallback for weak local evidence only)
```

---

## Key Design Decisions

### 1. Database Schema & Data Quality Cleaning (`src/build_db.py`)
- **Stable Recipe IDs:** Every recipe is keyed as `<source>:<original_json_key>` (`ar:...`, `epi:...`, `fn:...`), preserving original titles, raw ingredient lists, and raw instructions alongside cleaned fields.
- **5 Tables:** `recipes`, `recipe_ingredients` (indexed by `canonical_name`, `broad_family`, and `recipe_id`), `ingredients_summary`, `recipes_fts` (SQLite FTS5 with Porter stemming), and `dataset_metadata`.
- **Audit-Driven Cleanup:**
  - Stripped ~39.5k Allrecipes `"ADVERTISEMENT"` artifacts and ~3,650 non-ingredient lines (`Special equipment:`, Epicurious `*Available at...` footnotes, `/em` HTML fragments, bare numbers, section headers).
  - Flagged 10 scraped QA/test entries (e.g., Food Network `"Tst"`, `"Tstcgdgdgdgdgdgdgd"`, `"QA"` with `"method method method"`) via `is_junk=1` and 1,625 exact `(title, ingredients)` copies via `is_duplicate=1` without deleting raw records, keeping both stats and recommendations clean (`121,368` complete unique non-junk recipes).

### 2. Conservative Two-Level Ingredient Normalization (`src/normalizer.py`)
- **Deterministic Python rules** (used identically at ingestion time and query time on user inputs).
- **Strict `canonical_name`:** Strips quantities, units, parentheticals, alternative `" or ..."` clauses, and non-identity size/prep modifiers (`large`, `fresh`, `chopped`, `beaten`), then singularizes and maps strict synonyms (`scallions` → `green onion`, `Parmigiano-Reggiano` → `parmesan cheese`).
- **Preserves culinary identity:** Never collapses distinct forms or varieties (`tomato` ≠ `tomato paste`, `milk` ≠ `coconut milk`, `butter` ≠ `peanut butter`, `egg` ≠ `egg noodle` / `eggplant`, `brinjal` ≠ `long brinjal`).
- **Broad `broad_family` + Safety Guards:** Allergen/produce families (`dairy`, `peanut`, `tree_nut`, `egg`, `shellfish`, `soy`, `tomato`, `onion`, `garlic`) support broad stats and hard exclusions, with explicit non-dairy exception guards (`coconut milk`, `almond milk`, `peanut butter`, `cream of tartar`, `butternut`) plus raw-text FTS5 cross-checks so allergies aren't bypassed by unusual wording.

### 3. Reproducible Step-Counting Rule
- Removes boilerplate lines (`"Watch how to make this recipe."`), splits on newlines, and strips leading numbering (`"1."`, `"Step 2:"`).
- If `>= 2` non-empty lines exist, those lines are the steps; if instructions are a single block paragraph, splits on sentence boundaries while preserving abbreviations (`oz.`, `tbsp.`, `approx.`).

### 4. Pantry Matching, Quantities, and Hard Constraints (`match_pantry`)
- **Explainable Scoring:**
  `score = 2.0 * matched_count + 3.0 * required_coverage - 0.5 * extra_required_count - 1.0 * verified_quantity_shortages`
  *(Optional/garnish ingredients are separated from required extra ingredients.)*
- **Quantity Sufficiency:** Compares quantities **only within the same unit dimension** (`count ↔ count`, `volume ↔ volume` via `ml`, `weight ↔ weight` via `g`). Incompatible dimensions (e.g., `200g onions` vs `1 medium onion` or `1/4 cup`) are never guessed—they are reported as **`name_match_only (quantity not verified)`**.
- **No Silent Relaxation:** Hard constraints (`exclude_ingredients`, `max_steps`, `max_missing`) are strictly enforced. If 0 eligible recipes exist, `eligible_found=False` is stated first and closest near-misses are returned only in a separate `closest_violating_alternatives` list with each violated constraint named.

### 5. Evidence-Based Substitution & Uncertainty (`find_substitution_evidence`)
- **Local Dataset Primary:** Searches local recipes matching the target dish that omit the removed ingredient and obey all active restrictions, computes ingredient enrichment vs. baseline recipes containing that ingredient, and cites exact local `recipe_id`s.
- **Honest Evidence Rating:** Rates evidence `strong` (≥10 direct local variants), `moderate` (3–9), `weak_indirect` (broad category fallback), or `weak_or_none` (<3).
- **Optional Web Fallback:** Strictly gated to Task 3 when local evidence is weak/none (and disabled via `--no-web` or when users request dataset-only ingredients), with external URLs separated from local recipe citations.
- **Medical Safety:** Respects all stated allergies and never asserts medical safety based only on recipe text.

---

## Demo Conversations & Evaluation

- **Live Multi-Turn Transcripts (7 conversations across Tasks 1–3 & edge cases — 7/7 verified PASS, 0 invalid recipe IDs):** [`eval/demo_conversations.md`](eval/demo_conversations.md)
- **Automated Verification Report & One-Shot LLM Failure Comparison:** [`eval/eval_report.md`](eval/eval_report.md)

---

## Project Structure

```text
├── src/
│   ├── download_data.py   # Downloads & extracts the 3 Recipe Box JSON files
│   ├── normalizer.py      # Conservative ingredient/quantity parser & step splitter
│   ├── build_db.py        # Offline ETL builder for data/recipes.db
│   ├── tools.py           # 5 deterministic SQL/Python tools (search, detail, stats, pantry, substitution)
│   ├── llm_client.py      # Zero-dependency OpenRouter / OpenAI-compatible client with model fallback
│   ├── agent.py           # Multi-turn RecipeAgent + Python SessionState + grounding prompt
│   ├── chat.py            # Interactive CLI chatbot
│   ├── verify_db.py       # Quick DB sanity & latency benchmark
│   ├── test_tools.py      # 10 deterministic tool test cases (no API key needed)
│   └── run_evals.py       # Reproducible live eval suite & one-shot baseline comparator
├── eval/
│   ├── demo_conversations.md
│   ├── eval_report.md
│   └── eval_results.json
├── .env.example
├── .gitignore
├── AI_BRIEF.md            # Summary of design discussions, data audit, and walkthrough map
└── README.md
```

---

## AI Tools Disclosure

AI coding assistance (Antigravity) was used collaboratively for code scaffolding, regex/normalization edge-case debugging, SQLite indexing optimization, and drafting test harnesses and documentation. All architectural decisions (SQLite schema, conservative two-level normalization, deterministic tool separation, hard-constraint handling, and evaluation criteria) were reviewed, audited against the real dataset, and verified end-to-end. See [`AI_BRIEF.md`](AI_BRIEF.md) for a concise summary of the design discussion and iterations.
