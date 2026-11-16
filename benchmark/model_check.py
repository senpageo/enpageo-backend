"""Which models on the language-model server are reachable and usable for the map chat?

Run on the VPS from /opt/enpageo-backend with its venv. Per model: one chat call through Ollama's native
API with the chat's real system prompt and tools (keep_alive 0 so each test model is unloaded again right
away -- the GPU is shared; qwen3:30b, the production model, is left loaded). Records load time, prompt and
generation time, token counts, whether tools are supported, and which tool/arguments were chosen.
Embedding models get an embedding call instead. Output: one JSON line per model (stdout).
"""
import json
import sys
import time

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv(".env")
import httpx

from app.agent.chat import ALL_TOOLS, OLLAMA_BASE_URL, OLLAMA_CLIENT_CERT, OLLAMA_CLIENT_KEY, _system_prompt
from app.agent.tools import to_openai_tools

PRODUCTION = "qwen3:30b"
QUESTION = "Wie viele Gebäude gibt es je Baualtersklasse in Mitte?"
EXPECTED = ("aggregate_buildings", {"group_by": ["construction_period"], "districts": ["Mitte"], "metric": "building_count"})

c = httpx.Client(cert=(OLLAMA_CLIENT_CERT, OLLAMA_CLIENT_KEY), timeout=420.0)
models = [m["name"] for m in c.get(f"{OLLAMA_BASE_URL}/api/tags").json()["models"]]
tools = to_openai_tools(ALL_TOOLS)
print(json.dumps({"models": models}), flush=True)


def secs(ns):
    return round((ns or 0) / 1e9, 1)


for name in models:
    row = {"model": name}
    keep = {} if name == PRODUCTION else {"keep_alive": 0}
    t0 = time.time()
    try:
        if "embed" in name:
            r = c.post(f"{OLLAMA_BASE_URL}/api/embed", json={"model": name, "input": QUESTION, **keep})
            row.update(status=r.status_code, kind="embedding", seconds=round(time.time() - t0, 1))
            if r.status_code == 200:
                row["dims"] = len(r.json()["embeddings"][0])
            else:
                row["error"] = r.text[:200]
            print(json.dumps(row, ensure_ascii=False), flush=True)
            continue
        body = {"model": name, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": _system_prompt("de")}, {"role": "user", "content": QUESTION}],
                "tools": tools, **keep}
        r = c.post(f"{OLLAMA_BASE_URL}/api/chat", json=body)
        row["status"] = r.status_code
        if r.status_code != 200:
            row["error"] = r.text[:200]
            row["tools"] = False
            # without tools: is the model usable at all?
            body.pop("tools")
            body["messages"] = [{"role": "user", "content": "Antworte nur mit: OK"}]
            t1 = time.time()
            r = c.post(f"{OLLAMA_BASE_URL}/api/chat", json=body)
            row["plain_status"] = r.status_code
            if r.status_code == 200:
                d = r.json()
                row.update(plain_answer=(d["message"].get("content") or "")[:60], load_s=secs(d.get("load_duration")),
                           plain_seconds=round(time.time() - t1, 1))
            print(json.dumps(row, ensure_ascii=False), flush=True)
            continue
        d = r.json()
        msg = d["message"]
        calls = msg.get("tool_calls") or []
        row.update(
            tools=True, seconds=round(time.time() - t0, 1), load_s=secs(d.get("load_duration")),
            prompt_s=secs(d.get("prompt_eval_duration")), gen_s=secs(d.get("eval_duration")),
            prompt_tokens=d.get("prompt_eval_count"), out_tokens=d.get("eval_count"),
            thinking_chars=len(msg.get("thinking") or ""),
            tool_call=[{"name": tc["function"]["name"], "args": tc["function"].get("arguments")} for tc in calls],
            answer=(msg.get("content") or "")[:120],
        )
        if calls:
            f = calls[0]["function"]
            a = f.get("arguments") or {}
            row["correct_tool"] = f["name"] == EXPECTED[0]
            row["correct_args"] = row["correct_tool"] and all(a.get(k) == v for k, v in EXPECTED[1].items())
        else:
            row["correct_tool"] = row["correct_args"] = False
    except Exception as e:  # noqa: BLE001 - one broken model must not stop the survey
        row.update(error=f"{type(e).__name__}: {e}"[:200], seconds=round(time.time() - t0, 1))
    print(json.dumps(row, ensure_ascii=False), flush=True)

c.close()
