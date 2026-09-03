import json
import os

import anthropic
from sqlalchemy.orm import Session

from .tools import TOOLS, execute_tool, to_openai_tools

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.42.25:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:30b")
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """Du bist ein Assistent für eine interaktive Karte von Berliner Gebäuden (Enpageo).
Du kannst Gebäude im aktuell sichtbaren Kartenausschnitt nach Kriterien wie Nutzungstyp, Heiz-/Kältebedarf \
und Baujahr filtern (find_buildings). Die Werte stammen aus dem Szenario 2040 / RCP 4.5 / mittlere Sanierung.

Regeln:
- Antworte auf Deutsch, knapp und konkret.
- Nenne in deiner Antwort immer die echten Zahlen aus dem Tool-Ergebnis (Anzahl, Durchschnittswerte) — \
erfinde niemals Werte.
- Wenn "truncated" im Tool-Ergebnis true ist, weise darauf hin, dass es noch mehr Treffer gibt, als angezeigt werden.
- Das Tool durchsucht entweder ein von der Nutzerin gezeichnetes Polygon (falls vorhanden) oder sonst den \
aktuell sichtbaren Kartenausschnitt — keine benannten Bezirke/Stadtteile; falls danach gefragt wird, \
erkläre das kurz statt zu raten.
- Wenn keine Filterkriterien aus der Frage hervorgehen, frage kurz nach, statt find_buildings ohne Filter aufzurufen.
"""


def _run_chat_anthropic(db: Session, message: str, bbox: str, polygon: dict | None) -> dict:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    messages = [{"role": "user", "content": message}]
    geojson_result = None

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [block for block in response.content if block.type == "tool_use"]

        if not tool_uses:
            text_blocks = [block.text for block in response.content if block.type == "text"]
            return {
                "reply": "\n".join(text_blocks).strip(),
                "geojson": geojson_result,
            }

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            result = execute_tool(tool_use.name, tool_use.input, db, bbox, polygon)
            if "geojson" in result:
                geojson_result = result["geojson"]
                result_for_llm = result["summary_for_llm"]
            else:
                result_for_llm = result
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": str(result_for_llm),
                }
            )

        messages.append({"role": "user", "content": tool_results})

    return {
        "reply": "Entschuldigung, das hat zu viele Schritte gebraucht. Kannst du die Frage konkreter stellen?",
        "geojson": geojson_result,
    }


def _run_chat_ollama(db: Session, message: str, bbox: str, polygon: dict | None) -> dict:
    from openai import OpenAI

    client = OpenAI(base_url=f"{OLLAMA_BASE_URL}/v1", api_key="ollama")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    geojson_result = None

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=to_openai_tools(),
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return {"reply": (msg.content or "").strip(), "geojson": geojson_result}

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )

        for tool_call in msg.tool_calls:
            tool_input = json.loads(tool_call.function.arguments or "{}")
            result = execute_tool(tool_call.function.name, tool_input, db, bbox, polygon)
            if "geojson" in result:
                geojson_result = result["geojson"]
                result_for_llm = result["summary_for_llm"]
            else:
                result_for_llm = result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result_for_llm),
                }
            )

    return {
        "reply": "Entschuldigung, das hat zu viele Schritte gebraucht. Kannst du die Frage konkreter stellen?",
        "geojson": geojson_result,
    }


def run_chat(db: Session, message: str, bbox: str, polygon: dict | None = None) -> dict:
    provider = os.getenv("CHAT_PROVIDER", "ollama").lower()
    if provider == "anthropic":
        return _run_chat_anthropic(db, message, bbox, polygon)
    return _run_chat_ollama(db, message, bbox, polygon)
