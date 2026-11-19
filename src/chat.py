"""Interactive CLI interface for the grounded Recipe Box chatbot.

Usage:
    python -m src.chat
    python -m src.chat --debug      # also prints tool calls & model used each turn
    python -m src.chat --no-web     # strictly disable optional external web fallback
"""

from __future__ import annotations

import argparse
import sys
import time

from src.agent import RecipeAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="Recipe Box Grounded Chatbot CLI")
    parser.add_argument("--debug", action="store_true", help="Print tool calls and model info each turn")
    parser.add_argument("--no-web", action="store_true", help="Disable optional web fallback for Task 3")
    args = parser.parse_args()

    debug = args.debug
    try:
        agent = RecipeAgent(allow_web_fallback=not args.no_web)
    except Exception as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        sys.exit(1)

    print("=" * 70)
    print("Recipe Box Grounded Chatbot (SQLite + OpenRouter Tool Calling)")
    print(f"Model pecking order: {' -> '.join(agent.llm.models)}")
    print("Commands: /state | /reset | /debug | /quit")
    print("=" * 70)

    try:
        while True:
            try:
                user_text = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                break

            if not user_text:
                continue
            low = user_text.lower()
            if low in ("/quit", "/exit", "quit", "exit"):
                print("Bye!")
                break
            if low == "/reset":
                agent.reset()
                print("[Session state and history cleared.]")
                continue
            if low == "/state":
                print("[CURRENT SESSION STATE]")
                print(agent.state.to_prompt_summary())
                continue
            if low == "/debug":
                debug = not debug
                print(f"[Debug trace {'ON' if debug else 'OFF'}]")
                continue

            t0 = time.perf_counter()
            try:
                reply = agent.ask(user_text)
            except Exception as exc:
                print(f"\n[Error] {exc}")
                continue
            dt = time.perf_counter() - t0

            if debug:
                print(f"\n[Debug | {dt:.2f}s | model={agent.llm.last_model_used}]")
                for step in agent.last_turn_trace:
                    print(f"  tool -> {step['tool']}({step['args']})")

            print(f"\nAssistant:\n{reply}")
    finally:
        agent.close()


if __name__ == "__main__":
    main()
