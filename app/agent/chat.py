import json
import os

import anthropic
from sqlalchemy.orm import Session

from .tools import TOOLS, execute_tool, to_openai_tools

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.42.25:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:30b")
# mTLS client cert for reaching Ollama through the ai.coolingmap.de reverse proxy (Alex's server
# only accepts connections presenting a certificate signed by our own private CA).
OLLAMA_CLIENT_CERT = os.getenv("OLLAMA_CLIENT_CERT")
OLLAMA_CLIENT_KEY = os.getenv("OLLAMA_CLIENT_KEY")
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """Du bist ein Assistent für eine interaktive Karte von Berliner Gebäuden und Bäumen (Enpageo).
Du kannst Gebäude im aktuell sichtbaren Kartenausschnitt nach Kriterien wie Nutzungstyp, Heiz-/Kältebedarf \
und Baujahr filtern (find_buildings). Der Heizbedarf ist eine gebäudescharfe Jahresschätzung, der \
Kältebedarf ein grober Schätzwert je Nutzungstyp (keine Simulation).
Du kannst außerdem Straßen- und Anlagenbäume (Berlin Baumbestand) nach Art, Kronendurchmesser, Höhe, \
Bezirk oder Herkunft (Straße/Anlage) filtern (find_trees).
Außerdem kannst du Dach-Kühlanlagen (Klimaanlagen-Kondensatoren, Kühltürme — per Luftbildauswertung \
erkannt) nach Klasse, Erkennungssicherheit oder Grundfläche filtern (find_chillers).

Regeln:
- Antworte auf Deutsch, knapp und konkret.
- Nenne in deiner Antwort immer die echten Zahlen aus dem Tool-Ergebnis (Anzahl, Durchschnittswerte) — \
erfinde niemals Werte.
- Wenn "truncated" im Tool-Ergebnis true ist, weise darauf hin, dass es noch mehr Treffer gibt, als angezeigt werden.
- Die Tools durchsuchen entweder eine von der Nutzerin gezeichnete Fläche/Punkt/Linie (falls vorhanden) oder \
sonst den aktuell sichtbaren Kartenausschnitt — keine benannten Bezirke/Stadtteile (außer beim Baum-Attribut \
"Bezirk" selbst); falls danach gefragt wird, erkläre das kurz statt zu raten.
- Wurde ein Punkt oder eine Linie gezeichnet und die Nutzerin nennt IRGENDEINEN Abstand (z.B. "im Abstand von \
100m", "im Radius von 10m", "im Umkreis von 50 Metern", "in 1km Entfernung"), MUSST du diesen Wert immer als \
buffer_distance_m mitgeben — das ist nicht optional. Rechne dabei immer in Meter um (1km = 1000, 0,5km = 500). \
Bei einem gezeichneten Polygon oder ganz ohne Zeichnung lass buffer_distance_m weg.
- Gib bei usage_zone_type immer eine der exakten Kategorie-Teilzeichenketten aus der Tool-Beschreibung an, nie \
eine eigene Umschreibung — im Zweifel wähle die nächstliegende exakte Kategorie statt zu raten.
- Enthält das Tool-Ergebnis "search_area_m2", nenne die berechnete Fläche in der Antwort (in m², bei großen \
Flächen gerne gerundet in ha).
- Wenn keine Filterkriterien aus der Frage hervorgehen, frage kurz nach, statt ein Tool ohne Filter aufzurufen.
- Rufe pro Antwort nur eines der drei Tools auf, je nachdem ob nach Gebäuden, Bäumen oder Kühlanlagen gefragt wird.
"""


def _geometry_context_note(polygon: dict | None) -> str:
    """A one-line, unambiguous statement of what (if anything) is currently drawn on the map,
    so the model never has to guess whether a shape exists before deciding on buffer_distance_m."""
    if not polygon:
        return "\n\nAktuell ist nichts auf der Karte gezeichnet — die Suche nutzt den sichtbaren Kartenausschnitt."
    geom_type = polygon.get("type")
    if geom_type == "Point":
        return (
            "\n\nAktuell ist ein PUNKT auf der Karte gezeichnet. Nennt die Nutzerin einen Abstand/Radius, "
            "MUSST du ihn als buffer_distance_m an das Tool übergeben (in Metern umgerechnet), sonst frage "
            "kurz danach."
        )
    if geom_type == "LineString":
        return (
            "\n\nAktuell ist eine LINIE auf der Karte gezeichnet. Nennt die Nutzerin einen Abstand, MUSST du "
            "ihn als buffer_distance_m an das Tool übergeben (in Metern umgerechnet), sonst frage kurz danach."
        )
    if geom_type == "Polygon":
        return "\n\nAktuell ist eine FLÄCHE (Polygon) auf der Karte gezeichnet — buffer_distance_m ist hier nicht nötig."
    return ""


def _run_chat_anthropic(db: Session, message: str, bbox: str, polygon: dict | None) -> dict:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system_prompt = SYSTEM_PROMPT + _geometry_context_note(polygon)

    messages = [{"role": "user", "content": message}]
    geojson_result = None
    layer_result = None

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            temperature=0,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        tool_uses = [block for block in response.content if block.type == "tool_use"]

        if not tool_uses:
            text_blocks = [block.text for block in response.content if block.type == "text"]
            return {
                "reply": "\n".join(text_blocks).strip(),
                "geojson": geojson_result,
                "layer": layer_result,
            }

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            result = execute_tool(tool_use.name, tool_use.input, db, bbox, polygon)
            if "geojson" in result:
                geojson_result = result["geojson"]
                layer_result = result.get("layer")
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
        "layer": layer_result,
    }


def _run_chat_ollama(db: Session, message: str, bbox: str, polygon: dict | None) -> dict:
    import httpx
    from openai import OpenAI

    http_client = None
    if OLLAMA_CLIENT_CERT and OLLAMA_CLIENT_KEY:
        # httpx defaults to a 5s timeout when a custom client is supplied (the openai SDK's own
        # generous default only applies to its own internal client) — qwen3:30b routinely takes
        # well over that, especially a cold model load (~45s alone) plus tool-calling round trips.
        http_client = httpx.Client(cert=(OLLAMA_CLIENT_CERT, OLLAMA_CLIENT_KEY), timeout=170.0)

    # max_retries=0: the SDK's default retry-on-timeout re-issues the whole request and waits
    # out the full timeout again each time, so a single slow call can silently balloon to 2-3x
    # the configured timeout — worse than just failing once and letting the caller retry.
    # 170s (nginx's proxy_read_timeout is 200s): a cold model load alone measured at ~45-48s via
    # a bare API call, and the full system prompt used here evidently pushes it noticeably higher.
    client = OpenAI(
        base_url=f"{OLLAMA_BASE_URL}/v1",
        api_key="ollama",
        http_client=http_client,
        timeout=170.0,
        max_retries=0,
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + _geometry_context_note(polygon)},
        {"role": "user", "content": message},
    ]
    geojson_result = None
    layer_result = None

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=OLLAMA_MODEL,
            messages=messages,
            tools=to_openai_tools(),
            temperature=0,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return {"reply": (msg.content or "").strip(), "geojson": geojson_result, "layer": layer_result}

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
                layer_result = result.get("layer")
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
        "layer": layer_result,
    }


def run_chat(db: Session, message: str, bbox: str, polygon: dict | None = None) -> dict:
    provider = os.getenv("CHAT_PROVIDER", "ollama").lower()
    if provider == "anthropic":
        return _run_chat_anthropic(db, message, bbox, polygon)
    return _run_chat_ollama(db, message, bbox, polygon)
