"""Multi-turn grounded Recipe Box chatbot agent with explicit Python SessionState
and deterministic SQL/Python tool execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Optional
import urllib.parse
import urllib.request

from src.llm_client import LLMClient
from src.tools import RecipeDB

SYSTEM_PROMPT = """You are a reliable, data-grounded culinary assistant over the local Recipe Box dataset (~124,595 recipes from Allrecipes, Epicurious, and Food Network).

CORE RULES (NON-NEGOTIABLE):
1. GROUNDING FIRST: Always call the provided database tools for every factual search, count, average, ranking, pantry match, or recipe lookup. NEVER invent recipe IDs, recipe titles, ingredient lists, statistics, or counts from memory.
2. PRESERVE STABLE IDs: Whenever you mention a recipe from the dataset, include its exact `recipe_id` (e.g., `ar:...`, `epi:...`, `fn:...`) and original title.
3. NO FABRICATED ATTRIBUTES: This dataset does NOT contain structured nutrition facts (calories, protein, sodium, etc.), verified cooking/prep times, servings, or ratings. Whenever the user asks about details or attributes (e.g., calories, cook time, ingredients, steps) of a specific recipe from the session, ALWAYS call `get_recipe` first so you cite its exact `recipe_id` and explicitly report what is missing from the dataset instead of fabricating numbers. If you offer a rough general estimate, label it clearly as an external estimate, not dataset data.
4. MULTI-TURN CONTEXT: Always inspect the `[CURRENT SESSION STATE]` block below. Follow-up restrictions (e.g., "I cannot have dairy", "Exclude eggs too", "Make it under 5 steps") MUST refine the active search/pantry query by setting `merge_with_active=true` unless the user clearly starts a brand-new topic.
5. EXACT STATS (Task 1): For counts, percentages, averages, medians, or step rankings, report the exact numbers returned by `compute_stats`. Always state:
   - the active filters,
   - the exact denominator / number of recipes analyzed,
   - the calculation/tie-breaker used, and
   - the ingredient normalization or step-counting rule (especially when exact canonical vs. broad family counts differ).
6. STRICT HARD CONSTRAINTS & PANTRY MATCHING (Task 2):
   - NEVER invent new recipes for Task 2.
   - Show matched ingredients (including quantity sufficiency OR explicitly noting "name-level match only; quantity not verified"), extra required ingredients, optional ingredients, step count, and why each recipe ranked where it did.
   - NEVER silently relax a hard constraint. If `eligible_found` is `false`, clearly state FIRST that no eligible recipe matched all hard constraints, and only list `closest_violating_alternatives` in a separate section explicitly labeled as violating constraints (naming each violation).
7. EVIDENCE-BASED SUBSTITUTIONS (Task 3):
   - ALWAYS call `get_recipe` (if adapting a specific recipe) and `find_substitution_evidence` on the LOCAL dataset FIRST.
   - Only call `web_search_substitution` as a secondary fallback if local `evidence_strength` is `weak_indirect` or `weak_or_none` AND the user did NOT restrict you to local dataset ingredients / local evidence only. Clearly separate local dataset evidence from external web links.
   - If local evidence is `weak_or_none` and external search is disallowed (or a reliable adaptation cannot be supported without destroying the dish's core chemistry), say so honestly instead of fabricating a guaranteed recipe!
   - When an adaptation IS supported, ALWAYS include ALL 5 sections concisely and completely (never stop early):
     1. **Culinary Role** of the removed ingredient (binding, moisture, leavening, richness/fat, thickening, flavor).
     2. **Local Dataset Evidence** citing exact supporting `recipe_id`s, relevant preparation methods observed in those local recipes, and stating whether the evidence is direct or indirect.
     3. **Updated Ingredient List** (complete list for the adapted recipe, keeping all restrictions strictly obeyed).
     4. **Revised Step-by-Step Instructions** (complete adapted cooking steps, incorporating the substitute's preparation method).
     5. **Expected Taste, Texture & Prep Trade-offs** (explicitly distinguishing **predictions** from **verified dataset facts**) + an explicit note that recipe text alone does **not** guarantee medical/allergy safety.
"""

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_recipes",
            "description": (
                "Search existing recipes in the local SQLite database by dish name/keywords, "
                "required ingredients, excluded ingredients/allergens, and step/ingredient limits. "
                "Set merge_with_active=true for follow-up refinements like 'I cannot have dairy'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Dish name or search keywords (e.g. 'penne pasta', 'banana bread')."},
                    "include_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ingredients that MUST be present.",
                    },
                    "exclude_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Hard exclusions / restrictions (e.g. 'dairy', 'peanut', 'egg', 'gluten', 'garlic').",
                    },
                    "max_steps": {"type": "integer", "description": "Maximum allowed instruction steps."},
                    "max_ingredients": {"type": "integer", "description": "Maximum allowed ingredient count."},
                    "limit": {"type": "integer", "description": "Number of recipes to return (default 5, max 10)."},
                    "include_steps": {"type": "boolean", "description": "Include full instruction steps in results."},
                    "merge_with_active": {
                        "type": "boolean",
                        "description": "If true, carry over the previous search query/filters and add new restrictions.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recipe",
            "description": (
                "Fetch the complete grounded record (exact ingredients, parsed quantities, full step-by-step instructions, "
                "and list of unavailable attributes like nutrition/cook time) for one exact recipe_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string", "description": "Exact stable recipe ID (e.g. 'ar:...')."},
                },
                "required": ["recipe_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_stats",
            "description": (
                "Compute exact, reproducible statistics programmatically across the ENTIRE filtered recipe dataset "
                "(counts & percentages, avg/median ingredient & step counts, top ingredients, fewest/most steps)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": [
                            "count_and_percentage",
                            "ingredient_count_stats",
                            "fewest_steps",
                            "most_steps",
                            "top_ingredients",
                            "dataset_overview",
                        ],
                        "description": "Which exact calculation to run over the full filtered subset.",
                    },
                    "include_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter subset to recipes containing ALL of these ingredients.",
                    },
                    "exclude_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exclude recipes containing ANY of these ingredients/groups.",
                    },
                    "query": {"type": "string", "description": "Optional FTS keyword filter for the subset."},
                    "match_mode": {
                        "type": "string",
                        "enum": ["canonical", "family"],
                        "description": "'canonical' for strict normalized ingredient name, 'family' for broader ingredient family.",
                    },
                    "limit": {"type": "integer", "description": "How many ranked items to return (default 5)."},
                },
                "required": ["metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "match_pantry",
            "description": (
                "Task 2: Given ingredients the user currently has (optionally with quantities), hard constraints "
                "(exclude_ingredients, max_steps, max_missing), and a ranking preference, retrieve and rank the closest "
                "EXISTING recipes. Never relaxes hard constraints. Set merge_with_active=true on follow-ups like 'Exclude eggs too'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "available_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ingredients user has, optionally with quantities (e.g. ['2 large eggs', '1 cup milk', 'onions']).",
                    },
                    "exclude_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "HARD exclusions / forbidden ingredients (e.g. ['peanuts', 'eggs']).",
                    },
                    "max_steps": {"type": "integer", "description": "HARD maximum number of instruction steps."},
                    "max_missing": {"type": "integer", "description": "Optional HARD max number of missing/extra ingredients."},
                    "prefer": {
                        "type": "string",
                        "enum": ["fewest_missing", "most_matched", "fewest_steps"],
                        "description": "Soft ranking preference (default 'fewest_missing').",
                    },
                    "limit": {"type": "integer", "description": "Number of recipes to return (default 5)."},
                    "merge_with_active": {
                        "type": "boolean",
                        "description": "If true, keep previous pantry list & constraints and merge new exclusions/changes.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_substitution_evidence",
            "description": (
                "Task 3 (PRIMARY SOURCE): Search the LOCAL dataset for similar recipes that omit `remove_ingredient` "
                "and obey all `exclude_restrictions`. Returns enriched local substitutes with supporting recipe_ids, "
                "the full target recipe, and an honest evidence_strength rating."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "remove_ingredient": {
                        "type": "string",
                        "description": "Ingredient to replace or remove (e.g. 'egg', 'heavy cream', 'butter').",
                    },
                    "recipe_id": {
                        "type": "string",
                        "description": "Exact recipe_id of the target recipe being adapted (if known).",
                    },
                    "dish_query": {
                        "type": "string",
                        "description": "Dish name/type to search local variants for (e.g. 'banana bread', 'creamy pasta').",
                    },
                    "exclude_restrictions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional hard restrictions/allergies (e.g. ['dairy', 'peanut']).",
                    },
                },
                "required": ["remove_ingredient"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search_substitution",
            "description": (
                "Task 3 (OPTIONAL SECONDARY SOURCE ONLY): Use ONLY if `find_substitution_evidence` returned "
                "`weak_indirect` or `weak_or_none` AND the user did NOT restrict you to local dataset ingredients only. "
                "Returns external web snippets with URLs that must be clearly separated from local recipe IDs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short culinary substitution query."},
                },
                "required": ["query"],
            },
        },
    },
]


@dataclass
class SessionState:
    """Explicit Python-managed conversation state so follow-up filters never get lost."""

    mode: Optional[str] = None
    active_query: Optional[str] = None
    active_includes: list[str] = field(default_factory=list)
    active_excludes: list[str] = field(default_factory=list)
    active_max_steps: Optional[int] = None
    active_max_ingredients: Optional[int] = None
    active_pantry: list[str] = field(default_factory=list)
    active_prefer: str = "fewest_missing"
    active_recipe_id: Optional[str] = None
    active_recipe_title: Optional[str] = None
    last_results: list[dict[str, str]] = field(default_factory=list)

    def reset(self) -> None:
        self.mode = None
        self.active_query = None
        self.active_includes.clear()
        self.active_excludes.clear()
        self.active_max_steps = None
        self.active_max_ingredients = None
        self.active_pantry.clear()
        self.active_prefer = "fewest_missing"
        self.active_recipe_id = None
        self.active_recipe_title = None
        self.last_results.clear()

    def to_prompt_summary(self) -> str:
        lines = [f"mode={self.mode}"]
        if self.active_query:
            lines.append(f"active_query={self.active_query!r}")
        if self.active_includes:
            lines.append(f"active_includes={self.active_includes}")
        if self.active_excludes:
            lines.append(f"active_excludes={self.active_excludes}")
        if self.active_max_steps is not None:
            lines.append(f"active_max_steps={self.active_max_steps}")
        if self.active_pantry:
            lines.append(f"active_pantry={self.active_pantry}")
        if self.active_recipe_id:
            lines.append(f"active_recipe={self.active_recipe_id} ({self.active_recipe_title})")
        if self.last_results:
            brief = [f"{i+1}. [{r['recipe_id']}] {r['title']}" for i, r in enumerate(self.last_results[:5])]
            lines.append("last_shown_recipes=" + "; ".join(brief))
        return "\n".join(lines)


def _duckduckgo_lite_search(query: str, max_results: int = 3) -> dict[str, Any]:
    """Lightweight secondary web fallback (DuckDuckGo HTML) with explicit source URLs."""
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RecipeBoxBot/1.0"},
        )
        html = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
        links = re.findall(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL
        )
        snippets = re.findall(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, flags=re.IGNORECASE | re.DOTALL
        )
        results = []
        for idx, (href, raw_title) in enumerate(links[:max_results]):
            # Unwrap DDG redirect URL if present
            if "uddg=" in href:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                href = parsed.get("uddg", [href])[0]
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            snippet = re.sub(r"<[^>]+>", "", snippets[idx]).strip() if idx < len(snippets) else ""
            results.append({"title": title, "url": href, "snippet": snippet})
        return {
            "tool": "web_search_substitution",
            "source_type": "external_web_secondary",
            "query": query,
            "results": results,
            "note": (
                "External web results (secondary source — clearly distinguish from local Recipe Box dataset)."
                if results
                else "External web search returned no snippets; rely on uncertainty statement."
            ),
        }
    except Exception as exc:
        return {
            "tool": "web_search_substitution",
            "source_type": "external_web_secondary",
            "query": query,
            "results": [],
            "note": f"External web search unavailable ({exc}); state local uncertainty instead.",
        }


class RecipeAgent:
    """Multi-turn tool-calling agent combining LLM reasoning with grounded SQL/Python tools."""

    def __init__(
        self,
        db: Optional[RecipeDB] = None,
        llm: Optional[LLMClient] = None,
        allow_web_fallback: bool = True,
        max_tool_rounds: int = 5,
    ) -> None:
        self.db = db or RecipeDB()
        self.llm = llm or LLMClient()
        self.allow_web_fallback = allow_web_fallback
        self.max_tool_rounds = max_tool_rounds
        self.state = SessionState()
        self.history: list[dict[str, Any]] = []
        self.last_turn_trace: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.state.reset()
        self.history.clear()
        self.last_turn_trace.clear()

    def close(self) -> None:
        self.db.close()

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_recipes":
            merge = bool(args.get("merge_with_active", False))
            query = args.get("query")
            inc = list(args.get("include_ingredients") or [])
            exc = list(args.get("exclude_ingredients") or [])
            max_steps = args.get("max_steps")
            max_ings = args.get("max_ingredients")
            limit = min(int(args.get("limit") or 5), 10)
            include_steps = bool(args.get("include_steps", False))

            if merge and self.state.mode == "search":
                if not query:
                    query = self.state.active_query
                inc = list(dict.fromkeys(self.state.active_includes + inc))
                exc = list(dict.fromkeys(self.state.active_excludes + exc))
                if max_steps is None:
                    max_steps = self.state.active_max_steps
                if max_ings is None:
                    max_ings = self.state.active_max_ingredients

            res = self.db.search_recipes(
                query=query,
                include_ingredients=inc,
                exclude_ingredients=exc,
                max_steps=max_steps,
                max_ingredients=max_ings,
                include_steps=include_steps,
                limit=limit,
            )
            # Update explicit Python session state
            self.state.mode = "search"
            self.state.active_query = query
            self.state.active_includes = inc
            self.state.active_excludes = exc
            self.state.active_max_steps = max_steps
            self.state.active_max_ingredients = max_ings
            self.state.last_results = [
                {"recipe_id": r["recipe_id"], "title": r["title"]} for r in res["results"]
            ]
            if len(res["results"]) == 1:
                self.state.active_recipe_id = res["results"][0]["recipe_id"]
                self.state.active_recipe_title = res["results"][0]["title"]
            return res

        if name == "get_recipe":
            rid = str(args.get("recipe_id") or "").strip()
            res = self.db.get_recipe(rid)
            if res.get("found"):
                self.state.active_recipe_id = res["recipe_id"]
                self.state.active_recipe_title = res["title"]
            return res

        if name == "compute_stats":
            return self.db.compute_stats(
                metric=str(args.get("metric", "count_and_percentage")),
                include_ingredients=list(args.get("include_ingredients") or []),
                exclude_ingredients=list(args.get("exclude_ingredients") or []),
                query=args.get("query"),
                match_mode=str(args.get("match_mode") or "canonical"),
                limit=min(int(args.get("limit") or 5), 15),
            )

        if name == "match_pantry":
            merge = bool(args.get("merge_with_active", False))
            avail = list(args.get("available_ingredients") or [])
            exc = list(args.get("exclude_ingredients") or [])
            max_steps = args.get("max_steps")
            max_missing = args.get("max_missing")
            prefer = str(args.get("prefer") or "fewest_missing")
            limit = min(int(args.get("limit") or 5), 10)

            if merge and self.state.active_pantry:
                if not avail:
                    avail = list(self.state.active_pantry)
                else:
                    avail = list(dict.fromkeys(self.state.active_pantry + avail))
                exc = list(dict.fromkeys(self.state.active_excludes + exc))
                if max_steps is None:
                    max_steps = self.state.active_max_steps
                if "prefer" not in args:
                    prefer = self.state.active_prefer

            # Remove any newly excluded ingredient from the available pantry list cleanly
            if exc:
                exc_low = {e.strip().lower().rstrip("s") for e in exc}
                avail = [a for a in avail if a.strip().lower().rstrip("s") not in exc_low]

            res = self.db.match_pantry(
                available_ingredients=avail,
                exclude_ingredients=exc,
                max_steps=max_steps,
                max_missing=max_missing,
                prefer=prefer,
                limit=limit,
            )
            self.state.mode = "pantry"
            self.state.active_pantry = avail
            self.state.active_excludes = exc
            self.state.active_max_steps = max_steps
            self.state.active_prefer = prefer
            self.state.last_results = [
                {"recipe_id": r["recipe_id"], "title": r["title"]} for r in res.get("results", [])
            ]
            return res

        if name == "find_substitution_evidence":
            rid = args.get("recipe_id") or self.state.active_recipe_id
            dish_q = args.get("dish_query")
            exc = list(args.get("exclude_restrictions") or [])
            # Carry forward any active session allergy/exclusions automatically
            if self.state.active_excludes:
                exc = list(dict.fromkeys(self.state.active_excludes + exc))

            res = self.db.find_substitution_evidence(
                remove_ingredient=str(args.get("remove_ingredient", "")),
                recipe_id=rid,
                dish_query=dish_q,
                exclude_restrictions=exc,
                limit_supporting=4,
            )
            self.state.mode = "substitution"
            if res.get("target_recipe"):
                self.state.active_recipe_id = res["target_recipe"]["recipe_id"]
                self.state.active_recipe_title = res["target_recipe"]["title"]
            return res

        if name == "web_search_substitution":
            if not self.allow_web_fallback:
                return {
                    "tool": "web_search_substitution",
                    "results": [],
                    "note": "External web search is disabled by configuration; rely only on local dataset evidence and state uncertainty.",
                }
            return _duckduckgo_lite_search(str(args.get("query", "")))

        return {"error": f"Unknown tool: {name}"}

    def ask(self, user_message: str) -> str:
        """Run one full user turn (may execute 1..N grounded tool calls before answering)."""
        self.last_turn_trace = []
        self.history.append({"role": "user", "content": user_message})

        # Keep conversation window compact (SessionState already preserves exact active filters & IDs)
        # Truncate older assistant replies in the LLM prompt so multi-turn chats never exceed free-tier 8k TPM
        recent_history: list[dict[str, Any]] = []
        for m in self.history[-6:]:
            if m["role"] == "assistant" and len(m["content"]) > 900:
                recent_history.append(
                    {"role": "assistant", "content": m["content"][:900] + "\n...[earlier reply summarized; see SESSION STATE for exact active IDs & filters]"}
                )
            else:
                recent_history.append(m)

        def build_messages() -> list[dict[str, Any]]:
            sys_content = (
                f"{SYSTEM_PROMPT}\n\n[CURRENT SESSION STATE]\n{self.state.to_prompt_summary()}"
            )
            return [{"role": "system", "content": sys_content}] + recent_history

        working_messages = build_messages()

        for _round in range(self.max_tool_rounds):
            assistant_msg = self.llm.chat_completion(
                messages=working_messages,
                tools=TOOL_DEFINITIONS,
                temperature=0.2,
                max_tokens=3000,
            )
            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                final_text = (assistant_msg.get("content") or "").strip()
                if not final_text:
                    final_text = "I could not generate a grounded response for that request. Please try rephrasing."
                self.history.append({"role": "assistant", "content": final_text})
                return final_text

            # Preserve standard tool-call fields (plus optional provider extra_content like Gemini thought_signature)
            asst_record: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_msg.get("content") or "",
                "tool_calls": tool_calls,
            }
            if "extra_content" in assistant_msg:
                asst_record["extra_content"] = assistant_msg["extra_content"]
            working_messages.append(asst_record)

            for tc in tool_calls:
                fn = tc.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {}

                result = self._execute_tool(tool_name, parsed_args)
                self.last_turn_trace.append(
                    {
                        "model": self.llm.last_model_used,
                        "tool": tool_name,
                        "args": parsed_args,
                    }
                )
                # Compact JSON (no extra whitespace) while keeping full supporting steps intact
                result_str = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                if len(result_str) > 9000:
                    result_str = result_str[:9000] + '...[truncated]"}'

                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", f"call_{_round}"),
                        "name": tool_name,
                        "content": result_str,
                    }
                )

            # Refresh system state block after tools updated SessionState
            working_messages[0]["content"] = (
                f"{SYSTEM_PROMPT}\n\n[CURRENT SESSION STATE]\n{self.state.to_prompt_summary()}"
            )

        # Safety fallback if tool loop hit max_tool_rounds
        final_msg = self.llm.chat_completion(
            messages=working_messages,
            tools=None,
            temperature=0.2,
            max_tokens=3000,
        )
        final_text = (final_msg.get("content") or "").strip()
        self.history.append({"role": "assistant", "content": final_text})
        return final_text
