import json
import os
import time

import anthropic
from sqlalchemy.orm import Session

from . import analysis
from .i18n import localize, localize_view
from .presentation import choose
from .tools import TOOLS, execute_tool, to_openai_tools

ALL_TOOLS = TOOLS + analysis.TOOLS
MAX_ROWS_FOR_LLM = 40

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.42.25:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:30b")
# mTLS client cert for reaching Ollama through the ai.coolingmap.de reverse proxy (Alex's server
# only accepts connections presenting a certificate signed by our own private CA).
OLLAMA_CLIENT_CERT = os.getenv("OLLAMA_CLIENT_CERT")
OLLAMA_CLIENT_KEY = os.getenv("OLLAMA_CLIENT_KEY")
MAX_TOOL_ROUNDS = 4

SYSTEM_PROMPT = """Du bist ein Assistent für eine interaktive Karte von Berliner Gebäuden und Bäumen (Enpageo).
Du kannst Gebäude im aktuell sichtbaren Kartenausschnitt nach Kriterien wie Nutzungstyp, Gebäudefunktion (ALKIS), \
Prozesswärme (KWW), Heiz-/Kältebedarf und Baujahr filtern (find_buildings). Der Heizbedarf ist eine gebäudescharfe Jahresschätzung, der \
Kältebedarf ein grober Schätzwert je Nutzungstyp (keine Simulation).
Du kannst außerdem Straßen- und Anlagenbäume (Berlin Baumbestand) nach Art, Kronendurchmesser, Höhe, \
Bezirk oder Herkunft (Straße/Anlage) filtern (find_trees).
Außerdem kannst du Dach-Kühlanlagen (Klimaanlagen-Kondensatoren, Kühltürme — per Luftbildauswertung \
erkannt) nach Klasse, Erkennungssicherheit oder Grundfläche filtern (find_chillers).
Für Anzahlen, Summen, Anteile, Verteilungen und Vergleiche zwischen Gruppen fasst du Gebäude zusammen \
(aggregate_buildings: Heiz-/Kältebedarf, Kälteleistung, Fläche oder Anzahl; ohne group_by als eine einzelne Zahl, sonst \
je Nutzungsart, Bezirk, Postleitzahl und/oder Baualtersklasse; share_of_total für Anteile). Für den Jahresverlauf des \
Kältebedarfs nutzt du monthly_profile. Für Hotspots, Dichte, die Eignung von Kältenetzen und die Überlagerung von Wärme \
und Kälte nutzt du density_grid. Für Anzahlen von Kühlanlagen und Kühltürmen nutzt du aggregate_chillers. Gründerzeit \
heißt Baujahr 1870 bis 1918. Neubau heißt Baujahr ab 2010 (Parameter new_building).
Vorhanden sind außerdem: Grundstückseigentum des Landes Berlin (owner: kommunal/landeseigen = land_berlin, Bezirk, \
Hauptverwaltung, Sondervermögen; gilt für das Grundstück, nicht das Gebäude), PV-Dachpotenzial in kWp (min_pv_potential_kwp, \
Metrik pv_potential_kwp), registrierte PV-Anlagen ab 30 kWp bzw. der öffentlichen Hand (has_pv, pv_installed_kwp) und \
Rechenzentren aus OpenStreetMap/PeeringDB (building_function 'Rechenzentrum', nicht vollständig).
Dir fehlen noch: Klimaszenarien und Zukunftsjahre, Wirkung von Maßnahmen, Innenraumtemperaturen, Wärmebelastung, Strom, \
PV-Erzeugung im Zeitverlauf und kleine PV-Anlagen je Gebäude, Kältenetze, Leistung je Kühlanlage, Städte außer Berlin, \
Eigentum außer Land Berlin, U-Werte und Sanierungsstand, Kältebedarf der Rechenzentren. Braucht eine Frage davon etwas, rufe \
report_missing_data auf und erkläre es kurz. Schätze solche Werte nie selbst.

Regeln:
- Antworte auf Deutsch, knapp und konkret.
- Nenne in deiner Antwort immer die echten Zahlen aus dem Tool-Ergebnis (Anzahl, Durchschnittswerte) — \
erfinde niemals Werte.
- Wenn "truncated" im Tool-Ergebnis true ist, weise darauf hin, dass es noch mehr Treffer gibt, als angezeigt werden.
- Die Tools durchsuchen eine von der Nutzerin gezeichnete Fläche/Punkt/Linie (falls vorhanden), sonst den \
aktuell sichtbaren Kartenausschnitt. Benannte Berliner Bezirke und ganz Berlin gehen mit aggregate_buildings und \
monthly_profile (Parameter districts bzw. scope) sowie beim Baum-Attribut "Bezirk"; andere Ortsnamen (Kieze, \
Straßen) kannst du nicht auflösen — erkläre das kurz statt zu raten.
- Wurde ein Punkt oder eine Linie gezeichnet und die Nutzerin nennt IRGENDEINEN Abstand (z.B. "im Abstand von \
100m", "im Radius von 10m", "im Umkreis von 50 Metern", "in 1km Entfernung"), MUSST du diesen Wert immer als \
buffer_distance_m mitgeben — das ist nicht optional. Rechne dabei immer in Meter um (1km = 1000, 0,5km = 500). \
Bei einem gezeichneten Polygon oder ganz ohne Zeichnung lass buffer_distance_m weg.
- Nennt die Nutzerin eine konkrete Gebäudeart (Schule, Kita, Schwimmbad, Krankenhaus, Pflegeheim, Sporthalle, \
Kirche, Museum, Hotel, Fabrik …), nimm building_function (amtliche ALKIS-Gebäudefunktion) und NICHT usage_zone_type: \
„Schulen“ sind dann nur Schulen, ohne Kitas und Betreuungsgebäude. usage_zone_type nur für die groben Klassen \
(Wohngebäude, Büro, Handel, Produktion, sensibel).
- Gib bei usage_zone_type immer eine der exakten Kategorie-Teilzeichenketten aus der Tool-Beschreibung an, nie \
eine eigene Umschreibung — im Zweifel wähle die nächstliegende exakte Kategorie statt zu raten.
- Wärmebedarf nach KWW (kww_heat_demand) ist ein Kennwert-Schätzwert (Bruttogeschossfläche × Kennwert aus dem \
KWW-Technikkatalog, Heizung + Warmwasser). Prozesswärme (process_heat) ist nur eine Ja/Nein-Einstufung je \
Gebäudefunktion, keine Menge — nenne nie kWh für Prozesswärme.
- Enthält das Tool-Ergebnis "search_area_m2", nenne die berechnete Fläche in der Antwort (in m², bei großen \
Flächen gerne gerundet in ha).
- Wenn keine Filterkriterien aus der Frage hervorgehen, frage kurz nach, statt ein Tool ohne Filter aufzurufen. \
Ebenso bei offenen Angaben wie „Gebäude X“ oder „Quartier XY“: nachfragen statt raten.
- Folgefragen beziehen sich auf den bisherigen Gesprächsverlauf, auch wenn das Gebiet nicht noch einmal genannt wird.
- Rufe pro Antwort genau ein Datenwerkzeug auf: find_* für einzelne Objekte, die übrigen für zusammengefasste Werte.
- Die Darstellung (Karte, Diagramm, Tabelle) wählt die Anwendung selbst. Beschreibe das Ergebnis in 1–3 Sätzen mit \
den wichtigsten Zahlen; nenne beim Kältebedarf, dass es ein grober Platzhalter-Schätzwert ist.
"""


def _for_llm(result: dict, lang: str = "de") -> dict:
    """What the model sees of a tool result: the compact summary of find_* tools, or a result frame cut to
    MAX_ROWS_FOR_LLM rows (enough to describe it, without flooding the context)."""
    if "summary_for_llm" in result:
        return result["summary_for_llm"]
    if "kind" in result:
        # on the English page the model must see English category values and notes, or it quotes them in German
        result = localize(result, lang)
        return {
            "kind": result["kind"],
            "fields": [f["name"] for f in result["fields"]],
            "rows": result["rows"][:MAX_ROWS_FOR_LLM],
            "total_rows": result["total"],
            "truncated": result["total"] > MAX_ROWS_FOR_LLM,
            "notes": result["notes"],
        }
    return result



def _system_prompt(lang: str) -> str:
    """The prompt stays German (the tools and category names are German); only the answer language changes."""
    if (lang or "de").lower().startswith("de"):
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT.replace(
        "- Antworte auf Deutsch, knapp und konkret.",
        "- Antworte auf Englisch (die Nutzerin ist auf der englischen Seite), knapp und konkret. Bezirks- und "
        "Straßennamen bleiben unverändert. Tool-Parameter (z. B. Nutzungsarten) gibst du weiterhin auf Deutsch an.")


def _presentation(frame_result: dict | None, lang: str = "de", chooser: str = "rule") -> dict:
    """Result frame plus the chosen view for the frontend (chooser: presentation.CHOOSERS)."""
    if not frame_result:
        return {}
    # the view is chosen on the German frame (labels don't matter to it), then the frame is translated
    return {"frame": localize(frame_result, lang), "view": localize_view(choose(frame_result, chooser), lang)}


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


MAX_HISTORY_TURNS = 6


def _history_messages(history: list[dict] | None) -> list[dict]:
    """Earlier user/assistant texts of this conversation (no tool traffic), so follow-up questions work."""
    return [
        {"role": m["role"], "content": str(m.get("content", ""))[:2000]}
        for m in (history or [])[-MAX_HISTORY_TURNS:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]


def _run_chat_anthropic(db: Session, message: str, bbox: str, polygon: dict | None, history: list[dict] | None = None,
                        lang: str = "de", chooser: str = "rule") -> dict:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    system_prompt = _system_prompt(lang) + _geometry_context_note(polygon)

    messages = [*_history_messages(history), {"role": "user", "content": message}]
    geojson_result = None
    layer_result = None
    frame_result = None

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            temperature=0,
            system=system_prompt,
            tools=ALL_TOOLS,
            messages=messages,
        )

        tool_uses = [block for block in response.content if block.type == "tool_use"]

        if not tool_uses:
            text_blocks = [block.text for block in response.content if block.type == "text"]
            return {
                "reply": "\n".join(text_blocks).strip(),
                "geojson": geojson_result,
                "layer": layer_result,
                **_presentation(frame_result, lang, chooser),
            }

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool_use in tool_uses:
            result = execute_tool(tool_use.name, tool_use.input, db, bbox, polygon)
            if result.get("geojson"):
                geojson_result = result["geojson"]
                layer_result = result.get("layer")
            frame_result = result.get("frame") or (result if "kind" in result else frame_result)
            result_for_llm = _for_llm(result, lang)
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
        **_presentation(frame_result, lang, chooser),
    }


def _run_chat_ollama(db: Session, message: str, bbox: str, polygon: dict | None, history: list[dict] | None = None,
                     lang: str = "de", chooser: str = "rule", model: str | None = None) -> dict:
    import httpx
    from openai import OpenAI

    http_client = None
    if OLLAMA_CLIENT_CERT and OLLAMA_CLIENT_KEY:
        # httpx defaults to a 5s timeout when a custom client is supplied (the openai SDK's own
        # generous default only applies to its own internal client) — qwen3:30b routinely takes
        # well over that, especially a cold model load (~45s alone) plus tool-calling round trips.
        http_client = httpx.Client(cert=(OLLAMA_CLIENT_CERT, OLLAMA_CLIENT_KEY), timeout=170.0)

    try:
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
            {"role": "system", "content": _system_prompt(lang) + _geometry_context_note(polygon)},
            *_history_messages(history),
            {"role": "user", "content": message},
        ]
        geojson_result = None
        layer_result = None
        frame_result = None

        for _ in range(MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=model or OLLAMA_MODEL,
                messages=messages,
                tools=to_openai_tools(ALL_TOOLS),
                temperature=0,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                return {"reply": (msg.content or "").strip(), "geojson": geojson_result, "layer": layer_result,
                        **_presentation(frame_result, lang, chooser)}

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
                if result.get("geojson"):
                    geojson_result = result["geojson"]
                    layer_result = result.get("layer")
                frame_result = result.get("frame") or (result if "kind" in result else frame_result)
                result_for_llm = _for_llm(result, lang)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        # default=str: tool results carry Postgres NUMERIC columns (e.g. average
                        # heating/cooling demand) as Decimal, which json.dumps otherwise rejects.
                        "content": json.dumps(result_for_llm, default=str),
                    }
                )

        return {
            "reply": "Entschuldigung, das hat zu viele Schritte gebraucht. Kannst du die Frage konkreter stellen?",
            "geojson": geojson_result,
            "layer": layer_result,
            **_presentation(frame_result, lang, chooser),
        }
    finally:
        # The OpenAI SDK never closes a caller-supplied http_client, so without this every
        # chat request leaked its mTLS connection to Alex's server — enough of those piling up
        # over the process's lifetime silently exhausted his IP filter's connection limit and
        # made *new* requests hang on connect (not read), long after the leaking request itself
        # had finished.
        if http_client is not None:
            http_client.close()


def allowed_models() -> list[str]:
    """Models the chat may be asked to use (benchmark, B4). The API is public, so not just any model name on the
    shared language-model server: CHAT_MODELS in .env (comma-separated), else only the default model."""
    listed = [m.strip() for m in os.getenv("CHAT_MODELS", "").split(",") if m.strip()]
    return listed or [OLLAMA_MODEL]


def run_chat(db: Session, message: str, bbox: str, polygon: dict | None = None, history: list[dict] | None = None,
             lang: str = "de", chooser: str = "rule", model: str | None = None) -> dict:
    """model: one of allowed_models() (Ollama only; None = OLLAMA_MODEL). The answer carries meta (provider,
    model, chooser, seconds) so benchmark runs can be logged from the response alone."""
    provider = os.getenv("CHAT_PROVIDER", "ollama").lower()
    t0 = time.time()
    if provider == "anthropic":
        res = _run_chat_anthropic(db, message, bbox, polygon, history, lang, chooser)
        used = "anthropic"
    else:
        used = model or OLLAMA_MODEL
        res = _run_chat_ollama(db, message, bbox, polygon, history, lang, chooser, used)
    res["meta"] = {"provider": provider, "model": used, "chooser": chooser, "seconds": round(time.time() - t0, 2)}
    return res
