import os, json, re
from flask import Flask, request, jsonify, Response
import requests

try:
    import redis
except ImportError:
    redis = None

app = Flask(__name__)

# === Config desde variables de entorno ===
CLIENT_API_KEY  = os.getenv("PUBLIC_API_KEY", "")      # clave que enviarán tus clientes al proxy (Render)
UPSTREAM_API_KEY= os.getenv("UPSTREAM_API_KEY", "")    # la que espera TU Flask (x-api-key)
ADMIN_TOKEN     = os.getenv("ADMIN_TOKEN", "")         # token para /admin/register
REDIS_URL       = os.getenv("REDIS_URL", "")           # opcional (si no hay, se usa memoria)

# === Storage del destino (URL del túnel) ===
TARGET_KEY = "current_tunnel_url"
_mem = {"url": None}
rds = redis.from_url(REDIS_URL, decode_responses=True) if (REDIS_URL and redis) else None

def set_target(url: str):
    if rds: rds.set(TARGET_KEY, url)
    else:   _mem["url"] = url

def get_target() -> str | None:
    if rds: return rds.get(TARGET_KEY)
    return _mem.get("url")

TRYCLOUD_PAT = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$", re.I)  # opcional

@app.get("/health")
def health():
    return {"ok": True, "target": get_target()}, 200

@app.post("/admin/register")
def register():
    if ADMIN_TOKEN and request.headers.get("admin-token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if not url.startswith("http"):
        return jsonify({"error": "invalid url"}), 400
    # Para obligar que sea quick tunnel, descomenta:
    # if not TRYCLOUD_PAT.match(url): return jsonify({"error":"url not allowed"}), 400
    set_target(url)
    return {"ok": True, "target": url}, 200

@app.post("/webhook/optimus")
def proxy_optimus():
    # Auth cliente → Render (opcional pero recomendado)
    if CLIENT_API_KEY and request.headers.get("x-client-key") != CLIENT_API_KEY:
        return jsonify({"error": "forbidden"}), 403

    upstream_base = get_target()
    if not upstream_base:
        return jsonify({"error": "no upstream registered"}), 503

    upstream_url = f"{upstream_base}/webhook/optimus"

    # reenviamos el JSON exactamente como llega (sin reparsear)
    raw = request.get_data()
    headers = {
        "Content-Type": request.headers.get("Content-Type", "application/json"),
        "x-api-key": UPSTREAM_API_KEY,  # la que valida tu Flask local
    }

    try:
        resp = requests.post(upstream_url, data=raw, headers=headers, timeout=60)
        return Response(resp.content, status=resp.status_code,
                        content_type=resp.headers.get("Content-Type", "application/json"))
    except Exception as e:
        return jsonify({"error": f"upstream error: {str(e)}"}), 502
