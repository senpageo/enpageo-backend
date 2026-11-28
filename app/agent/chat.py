import json
import os
import re
import time
from types import SimpleNamespace

import anthropic
from sqlalchemy.orm import Session

from . import analysis
from .i18n import localize, localize_view, tr
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
Prozesswärme (KWW), Heiz-/Kältebedarf und Baujahr filtern (find_buildings). Der Heizbedarf ist eine gebäudescharfe Jahresschätzung.
Kältebedarf: Annahmen in kWh/(m²·a) je Gebäudefunktion und Baualtersklasse mit Korrekturfaktoren für Klimaszenario \
(gering/mittel/stark), Jahr (2025–2050), Maßnahmen (außenliegende Fensterverschattung, Lüftung durch Nutzer, \
automatische Nachtlüftung, Fenstertausch, Sanierung der Hülle) und Sollwert (22/24/26 °C), keine Simulation. Alle Werkzeuge mit Kältebedarf nehmen dafür \
die Parameter climate, year, measures, setpoint. Nennt die Frage dazu nichts, lass sie weg: dann gilt das \
Standardszenario (mittlerer Klimawandel, 2030, Sollwert nach Gebäudefunktion, außenliegende Fensterverschattung, \
keine Nachtlüftung). Nenne in der Antwort immer, welches Szenario gerechnet wurde.
Du kannst außerdem Straßen- und Anlagenbäume (Berlin Baumbestand) nach Art, Kronendurchmesser, Höhe, \
Bezirk oder Herkunft (Straße/Anlage) filtern (find_trees).
Außerdem kannst du Dach-Kälteanlagen (Klimaanlagen-Kondensatoren, Kühltürme — per Luftbildauswertung \
erkannt) nach Klasse, Kälteleistung (KIT-Schätzung je Anlage: genau, mindestens, höchstens), Zahl der erkannten Lüfter \
oder Grundfläche filtern und nach Leistung auf- oder absteigend sortiert zeigen (find_chillers). Anlagen mit 0 kW gibt \
es: bei ihnen wurde kein Lüfter erkannt, daher fehlt die Leistungsschätzung.
Für Anzahlen, Summen, Anteile, Verteilungen und Vergleiche zwischen Gruppen fasst du Gebäude zusammen \
(aggregate_buildings: Heiz-/Kältebedarf, Kälteleistung, Fläche oder Anzahl; ohne group_by als eine einzelne Zahl, sonst \
je Nutzungsart, Bezirk, Postleitzahl und/oder Baualtersklasse; share_of_total für Anteile). Für den Jahresverlauf des \
Kältebedarfs nutzt du monthly_profile. Für Hotspots, Dichte je Fläche und die Überlagerung von Wärme und Kälte nutzt du density_grid. Für \
Wärme- und Kälteliniendichten und die Frage, wo sich ein Wärme- oder Kältenetz lohnt, nutzt du line_density \
(potenzielle Netzstruktur auf den Straßen, kWh je Meter und Jahr; bei „wo lohnt sich ein Netz“ group_by='street', min_length_m=200, min_buildings=5). Nur dort gibt es \
auch fertige Szenarien (standard, 2025, 2045 gering/mittel/stark). Für Anzahlen von Kälteanlagen und Kühltürmen nutzt du aggregate_chillers. Gründerzeit \
heißt Baujahr 1870 bis 1918. Neubau heißt Baujahr ab 2010 (Parameter new_building).
Vorhanden sind außerdem: Grundstückseigentum des Landes Berlin (owner: kommunal/landeseigen = land_berlin, Bezirk, \
Hauptverwaltung, Sondervermögen; gilt für das Grundstück, nicht das Gebäude), PV-Dachpotenzial in kWp (min_pv_potential_kwp, \
Metrik pv_potential_kwp), registrierte PV-Anlagen ab 30 kWp bzw. der öffentlichen Hand (has_pv, pv_installed_kwp) und \
Rechenzentren aus OpenStreetMap/PeeringDB (building_function 'Rechenzentrum', nicht vollständig).
Für Gebäude mit erkannter Kälteanlage gibt es den Kälteverbrauch: geschätzt aus der installierten Leistung mit \
Volllaststunden, dazu Kältestrom und installierte Kälteleistung (has_chiller, Metriken cooling_actual, electricity_cooling, \
chiller_capacity), jeweils mit Spanne min–max. Nenne bei diesen Werten IMMER die Spanne und dass die Unsicherheit groß ist. \
Der Kälteverbrauch ist ein Abgleich zum Kältebedarf, nicht dasselbe: Steht er im Ergebnis (cooling_actual), nenne ihn IMMER zusätzlich zum Bedarf, mit Anzahl der Gebäude mit erkannter Anlage.
Wie viel weniger Kältebedarf bestimmte Maßnahmen bringen, zeigt building_scenario_demand (compare_measures) je Gebäude \
mit Einsparung in MWh/a und %.
Für mehrere Szenarien auf einmal nutzt du cooling_scenarios: Entwicklung über die Jahre 2025–2050 („wie entwickelt \
sich“, „bis 2045“, „in den nächsten 20 Jahren“), Klimaszenarien, Maßnahmen und ihre Kombinationen oder Sollwerte im \
Vergleich, für ein Gebiet, eine Gruppe oder ein Gebäude; quantity='electricity' für den Kältestrom. Zum Einfärben aller \
Gebäude nach einem Wert („färbe nach …“, „wie ist … verteilt“, „zeige das PV-Potenzial im Gebiet“) nutzt du \
color_buildings. Kältestrom, wenn der Bedarf mit Kälteanlagen gedeckt wird, und elektrische Leistung sind Metriken \
(electricity_demand_cooling, electric_power_cooling); Abwärme erkannter Anlagen ist waste_heat_actual bzw. \
waste_heat_power_actual. aggregate_buildings mit group_by ['district'] oder ['planning_area'] zeigt die Gebiete auch als \
Flächen auf der Karte. Eine Adresse wählst du mit address, eine benannte Einrichtung (Charité, Flughafen Tempelhof) mit \
place.
Bestehende Wärme- und Kältenetze in der Nähe (Kältenetze mit Betreiber, Leistung, Temperaturen; Nahwärmenetze; mit \
Fernwärme versorgte Gebiete 2022) zeigt existing_networks. Den Stromverbrauch je Block (2021) mit dem Kältestrom \
erkannter Anlagen und dem Kältestrom bei Deckung des ganzen Bedarfs zeigt block_electricity. Gebäude, deren erkannte \
Anlagen mehr Leistung haben als sie selbst brauchen, mit den Nachbarn ohne Anlage zeigt cooling_surplus. Die künftig \
benötigte Kälteleistung ist aggregate_buildings mit metric cooling_capacity und year; die heute installierte ist \
chiller_capacity; aggregate_buildings mit cooling_capacity liefert beides (cooling_actual = heute installiert) – \
nenne beides und das Verhältnis.
Typische U-Werte (Wand, Dach, Boden, Fenster, Mittel) je Gebäude nach Gebäudetyp und Baualter aus der NaiS-DB stehen in \
find_buildings (sort_by u_mean für „schlechteste U-Werte“), color_buildings (u_mean, u_wall, u_window) und \
aggregate_buildings (u_mean); sage dazu, dass es typische Werte sind und der Sanierungsstand unbekannt ist.
Wärmebelastung im Außenraum (Klimamodell Berlin 2022, Ist-Zustand an einem heißen Sommertag, je Block): gefühlte \
Temperatur PET um 14 Uhr und Lufttemperatur um 4 Uhr. Schwerpunkte und verwundbare Gebiete zeigt heat_stress_map, die \
kühlsten Grünflächen in der Nähe cool_places. Für Gebäude gibt es in find_buildings min_pet_14h, heat_vulnerable und \
sort_by pet_14h (mit has_chiller=false für „hohe Wärmebelastung ohne Kälteanlage“), in color_buildings pet_14h und \
t_air_04h, in aggregate_buildings die Mittelwerte. PET über 35 °C ist starke, über 41 °C extreme Wärmebelastung: \
bei „hoher/starker Wärmebelastung“ setze min_pet_14h=35, bei „extremer“ min_pet_14h=41 – ohne diesen Filter darfst \
du nicht behaupten, die Treffer hätten hohe Wärmebelastung.
Platzhalter-Werkzeuge (Zahlen ausgedacht bzw. pauschal, bis die Simulationen der Projektpartner kommen – sage das \
IMMER dazu): street_trees_scenario pflanzt neue Straßenbäume entlang eines markierten Abschnitts, einer Straße oder \
an einer Adresse („alle 15 m einen Baum“, „Stadtgrün pflanzen“) und zeigt sie lila in der Karte; gib den Text aus \
dem Ergebnis (Anzahl Bäume, Kühlung der Umgebungsluft, beschattete Gebäude, Kältebedarfssenkung) und den Hinweis \
auf die ausgedachten Zahlen wieder. indoor_overheating schätzt Tage über einer Innenraumtemperatur ohne Kühlung \
(„wo wird eine Klimaanlage unvermeidlich“, „Innenraum über 30 °C an mehr als 10 Tagen“: threshold_c 30, min_days 10; \
„wenn die anderen Maßnahmen ausgereizt sind“: other_measures_exhausted=true); für ein Gebäude mit \
compare_measures=true, welche Maßnahmen für eine natürliche Klimatisierung reichen. retrofit_path rechnet den \
Kältebedarf eines Gebiets bis 2045 mit einem Sanierungspfad („alle 4 Jahre ein öffentliches Gebäude sanieren“). \
Fenstertausch und Sanierung sind Maßnahmen (fenstertausch, sanierung) in building_scenario_demand und \
cooling_scenarios; eine Sanierung kann den Kältebedarf auch erhöhen, weil die Dämmung Wärme im Gebäude hält.
Dir fehlen noch: simulierte Klimaszenarien und Maßnahmenwirkungen, Dach- und Fassadenbegrünung, simulierte Innenraumtemperaturen, Wärmebelastung für künftige Jahre, \
Strom nach Verbrauchern und Lastgänge, PV-Erzeugung im Zeitverlauf und kleine PV-Anlagen je Gebäude, Leitungsverläufe der Netze, gemessene \
Leistung je Kälteanlage, Städte außer Berlin, Eigentum außer Land Berlin, tatsächlicher Sanierungsstand je Gebäude, \
gemessener Kältebedarf der Rechenzentren. Braucht eine Frage davon etwas, rufe report_missing_data auf und erkläre es kurz. Schätze solche Werte nie selbst.

Regeln:
- Antworte auf Deutsch, knapp und konkret.
- Nenne in der Antwort nie Werkzeug-, Parameter-, Spalten- oder Tabellennamen, keine Codes und kein SQL. \
Beschreibe Filter in Alltagssprache.
- Steht im Tool-Ergebnis ein Hinweis (notes), der die Zahlen einordnet (z. B. Anlagen mit 0 kW), nenne ihn. \
Nenne keine Verteilungen, Bezirke oder Werte, die nicht im Tool-Ergebnis stehen.
- Rufe das Werkzeug mit genau dem Filter auf, den die Frage nennt („mit 0 kW“ = genau 0 kW, nicht „mindestens 0 kW“).
- Nenne in deiner Antwort immer die echten Zahlen aus dem Tool-Ergebnis (Anzahl, Durchschnittswerte) — \
erfinde niemals Werte.
- Steht im Tool-Ergebnis „anzeige“, nenne die Gesamtzahl der Treffer daraus und, falls nicht alle angezeigt \
werden, wie viele angezeigt werden (nie Wörter wie „truncated“ oder „true“). Zahlen, die nur für die angezeigten \
Treffer gelten, kennzeichne so („unter den angezeigten …“). Mehr kann die Karte \
nicht auf einmal zeigen: biete nie an, alle Treffer zu zeigen oder die Liste zu vergrößern, sondern einen engeren \
Filter, ein kleineres Gebiet oder eine Zusammenfassung (z. B. nach Bezirk). Verwende dafür keine englischen Wörter.
- Die Tools durchsuchen eine von der Nutzerin gezeichnete Fläche/Punkt/Linie (falls vorhanden), sonst den \
aktuell sichtbaren Kartenausschnitt. Benannte Berliner Bezirke und ganz Berlin gehen mit aggregate_buildings und \
monthly_profile (Parameter districts bzw. scope) sowie beim Baum-Attribut "Bezirk"; Adressen und benannte \
Einrichtungen über address bzw. place. Kieze ohne feste Grenze kannst du nicht auflösen — erkläre das kurz statt zu raten.
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
- Nennt die Frage „in Berlin“, „ganz Berlin“, „berlinweit“ oder „stadtweit“ (ohne gezeichnetes Gebiet), setze \
scope='berlin' — sonst wird nur der sichtbare Kartenausschnitt durchsucht. Bei einem Bezirksnamen setze districts.
- Folgefragen beziehen sich auf den bisherigen Gesprächsverlauf, auch wenn das Gebiet nicht noch einmal genannt wird.
- Rufe pro Antwort genau ein Datenwerkzeug auf: find_* für einzelne Objekte, die übrigen für zusammengefasste Werte.
- Die Darstellung (Karte, Diagramm, Tabelle) wählt die Anwendung selbst. Beschreibe das Ergebnis in 1–3 Sätzen mit \
den wichtigsten Zahlen; nenne beim Kältebedarf das Szenario und dass es Annahmen sind.
- Steht im Ergebnis ein Kälteverbrauch erkannter Anlagen (cooling_actual oder ein Hinweis „Abgleich“), nenne ihn \
im zweiten Satz mit der Zahl der Gebäude, z. B. „Zum Abgleich: erkannte Anlagen verbrauchen … MWh/a in … Gebäuden.“ – nur mit den Zahlen aus dem Ergebnis
- Bei einer Entwicklung über die Jahre nenne den Zeitraum (z. B. 2025–2050), nicht ein einzelnes Jahr.
- Die Anschlussfrage bietet nie etwas an, das schon angezeigt wird (die gefundenen Objekte stehen bereits auf der \
Karte und in der Tabelle). Biete stattdessen einen neuen Schritt an, etwa einen anderen Filter, ein anderes Gebiet \
oder eine Zusammenfassung.
- Jede Antwort endet mit einer Anschlussfrage im Antworttext (Pflicht, auch bei kurzen Antworten). Sie sagt zuerst, was gerechnet wurde, und bietet dann den naheliegenden nächsten Schritt an, z. B. „Das ist für 2030 gerechnet. Willst du das auch für 2045 wissen?“ oder „Das sind alle Gebäude in Spandau. Soll ich nach Nutzung aufteilen?“. Achte auf die Richtung: ein späteres Jahr oder stärkerer Klimawandel erhöht den Kältebedarf, Maßnahmen und ein höherer Sollwert senken ihn.
- Schreibe danach in eine eigene letzte Zeile „VORSCHLAG: “ und die Antwort, die die Nutzerin auf deine Anschlussfrage am wahrscheinlichsten gibt, als ganzen kurzen Satz in ihren Worten (z. B. „Ja, zeig mir das für 2045.“). Keine Stichworte wie „Spandau, stark“. Diese Zeile wird nicht angezeigt, sondern grau ins Eingabefeld geschrieben.
- Schreibe direkt vor die VORSCHLAG-Zeile eine eigene Zeile „TITEL: “ mit einer Überschrift für das Ergebnis aus \
2 bis 6 Wörtern in der Sprache der Antwort, ohne Anzahl und ohne Satzzeichen am Ende, z. B. „Kälteanlagen ohne \
Leistungsschätzung in Berlin“ oder „Kältebedarf der Schulen in Spandau 2045“. Auch diese Zeile wird nicht angezeigt.
- Zum Schluss prüfen: Steht in deinem Text ein Name mit Unterstrich, ein Werkzeugname, ein Feld- oder Klassencode \
oder ein Ausdruck mit „=“? Dann formuliere ihn in Alltagssprache um. Die Nutzerin kennt diese Namen nicht.
"""

# the suggestion line the model ends its answer with (see the last rule of the prompt)
_SUGGESTION = re.compile(r"\n?\s*(?:\*\*)?(?:VORSCHLAG|SUGGESTION)(?:\*\*)?\s*:\s*(.+?)\s*$", re.S | re.I)


# the title line the model writes before the suggestion (heading of the answer window, CSV file name)
_TITLE = re.compile(r"^\s*(?:\*\*)?(?:TITEL|TITLE)(?:\*\*)?\s*:\s*(.+?)\s*$", re.M | re.I)


def _split_title(reply: str) -> tuple[str, str | None]:
    m = _TITLE.search(reply or "")
    if not m:
        return reply, None
    title = m.group(1).strip().strip('"„“”*').rstrip(".:;!?").strip()
    rest = (reply[: m.start()] + reply[m.end():]).strip()
    return rest, (title[:80] or None)


def _split_suggestion(reply: str) -> tuple[str, str | None]:
    """'answer ... VORSCHLAG: next input' -> ('answer ...', 'next input')."""
    m = _SUGGESTION.search(reply or "")
    if not m:
        return reply, None
    suggestion = m.group(1).strip().strip('"„“”').strip()
    return reply[: m.start()].rstrip(), (suggestion.splitlines()[0][:200] if suggestion else None)


# qwen3 sometimes writes its tool call into the answer text instead of the tool-call channel, e.g.
# <function-call>{"name": "...", "arguments": {...}}</function-call> (also seen as <tool_call>...</tool_call>).
_TEXT_TOOL_CALL = re.compile(r"<(function-call|tool_call)>\s*(\{.*?\})\s*</\1>", re.S)


def _text_tool_calls(content: str | None) -> list:
    """Tool calls written as text, in the shape of the SDK's tool_calls (id, function.name, function.arguments)."""
    calls = []
    for k, m in enumerate(_TEXT_TOOL_CALL.finditer(content or "")):
        try:
            data = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue
        name = data.get("name")
        args = data.get("arguments", data.get("parameters", {}))
        if not isinstance(name, str):
            continue
        if not isinstance(args, str):
            args = json.dumps(args)
        calls.append(SimpleNamespace(id=f"text_call_{k}", type="function",
                                     function=SimpleNamespace(name=name, arguments=args)))
    return calls


def _for_llm(result: dict, lang: str = "de") -> dict:
    """What the model sees of a tool result: the compact summary of find_* tools, or a result frame cut to
    MAX_ROWS_FOR_LLM rows (enough to describe it, without flooding the context)."""
    if "summary_for_llm" in result:
        s = dict(result["summary_for_llm"])
        # fertige Saetze, damit das Modell die Gesamtzahl nennt und nicht "truncated" o. ae. zitiert
        total, shown = s.get("total_matches"), s.get("returned")
        en = (lang or "de").lower().startswith("en")
        if s.pop("truncated", None):
            s["anzeige"] = (f"{total} results found (give this number). {shown} of them are shown on the map and in the "
                            f"table; counts, sums and means in this result refer only to these {shown}." if en else
                            f"Gefunden: {total} Treffer (diese Zahl nennen). Auf der Karte und in der Tabelle stehen "
                            f"{shown} davon; Anzahlen, Summen und Mittelwerte in diesem Ergebnis gelten nur für diese {shown}.")
        elif total is not None:
            s["anzeige"] = f"{total} results found, all shown." if en else f"Gefunden: {total} Treffer, alle angezeigt."
        if s.get("notes"):
            s["notes"] = [tr(n) for n in s["notes"]] if not (lang or "de").lower().startswith("de") else s["notes"]
        return s
    if "kind" not in result and isinstance(result.get("frame"), dict):  # frame + map layer, no own summary
        result = result["frame"]
    if "kind" in result:
        # on the English page the model must see English category values and notes, or it quotes them in German
        result = localize(result, lang)
        return {
            "kind": result["kind"],
            "fields": [f["name"] for f in result["fields"]],
            "rows": result["rows"][:MAX_ROWS_FOR_LLM],
            "total_rows": result["total"],
            **({"zeilen": f"Du siehst nur die ersten {MAX_ROWS_FOR_LLM} von {result['total']} Zeilen; die Anwendung "
                          f"zeigt alle {result['total']}. Nenne {result['total']} als Anzahl."}
               if result["total"] > MAX_ROWS_FOR_LLM else {}),
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
            tool_calls = msg.tool_calls or _text_tool_calls(msg.content)

            if not tool_calls:
                return {"reply": (msg.content or "").strip(), "geojson": geojson_result, "layer": layer_result,
                        **_presentation(frame_result, lang, chooser)}

            messages.append(
                {
                    "role": "assistant",
                    "content": None if not msg.tool_calls else msg.content,
                    "tool_calls": [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                   for tc in tool_calls],
                }
            )

            for tool_call in tool_calls:
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
    res["reply"], title = _split_title(res.get("reply") or "")
    res["reply"], res["suggestion"] = _split_suggestion(res["reply"])
    view, fr = res.get("view"), res.get("frame") or {}
    if title and isinstance(view, dict) and view.get("type") != "text":
        total = fr.get("total")
        res["view"] = {**view, "title": f"{title} ({total})" if fr.get("kind") == "features" and total else title}
    res["meta"] = {"provider": provider, "model": used, "chooser": chooser, "seconds": round(time.time() - t0, 2)}
    return res
