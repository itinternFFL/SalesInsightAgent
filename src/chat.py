"""CLI entrypoint for the sales insight RAG agent.

Run with:  python -m src.chat
"""

import ollama

from src.agent import MODEL, answer
from src.index import get_index


def _ollama_is_ready() -> bool:
    try:
        models = {m["model"] for m in ollama.list()["models"]}
    except Exception:
        return False
    return MODEL in models or f"{MODEL}:latest" in models or any(
        m.startswith(MODEL) for m in models
    )


def main():
    if not _ollama_is_ready():
        print(
            f"Ollama isn't reachable, or the '{MODEL}' model isn't pulled yet.\n"
            "Make sure the Ollama app is running, then run:\n"
            f"    ollama pull {MODEL}\n"
        )
        return

    index_df = get_index()

    print("\nSales Insight Agent — ask a question about the FMO sales reports.")
    print("Type 'exit' or 'quit' to stop.\n")

    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break

        response = answer(question, index_df)
        print(f"\nAgent: {response}\n")


if __name__ == "__main__":
    main()
