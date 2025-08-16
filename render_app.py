import os, json, re
from flask import Flask, request, jsonify, Response
import requests

# CORS
from flask_cors import CORS

try:
    import redis
except ImportError:
    redis = None

app = Flask(__name__)

# === Config desde variables de entorno ===
CLIENT_API_KEY   = os.getenv("PUBLIC_API_KEY", "")      # clave que enviarán los clientes al proxy (Render)
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")    # la que espera TU Flask (x-api-key)
ADMIN_TOKEN      = os.getenv("ADMIN_TOKEN", "")         # token para /admin/register
REDIS_URL        = os.getenv("REDIS_URL", "")           # opcional (si no hay, se usa memoria)
DEBUG_HEADERS    = os.getenv("DEBUG_HEADERS", "0") == "1"  # <--- NUEVO: logs opcionales

# === Orígenes permitidos para CORS ===
origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
if origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in origins_env.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = [
        "https://www.consorcio-chilca.build-ness.com",
        "http://localhost:5173",
        "http://localhost:3000",
    ]

# Habilitar CORS solo en rutas de uso público (webhook/health)
CORS(
    app,
    resources={
        r"/webhook/*": {"origins": ALLOWED_ORIGINS},
        r"/health": {"origins": "*"},  # útil para pruebas
    },
    methods=["GET", "POST", "OPTIONS"],
    # ✨ SUMA: permitir también x-optimus-model y x-optimus-alias
    allow_headers=["Content-Type", "x-client-key", "x-optimus-model", "x-optimus-alias"],
    supports_credentials=False,
)

# === Storage del destino (URL del túnel) ===
TARGET_KEY = "current_tunnel_url"
_mem = {"url": None}
rds = redis.from_url(REDIS_URL, decode_responses=True) if (REDIS_URL and redis) else None

def set_target(url: str):
    if rds:
        rds.set(TARGET_KEY, url)
    else:
        _mem["url"] = url

def get_target() -> str | None:
    if rds:
        return rds.get(TARGET_KEY)
    return _mem.get("url")

TRYCLOUD_PAT = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$", re.I)  # opcional

@app.get("/health")
def health():
    return {"ok": True, "target": get_target()}, 200

@app.post("/admin/register")
def register():
    # Admin no expone CORS (no lo listamos arriba). Debe llamarse desde backend/Postman.
    if ADMIN_TOKEN and request.headers.get("admin-token") != ADMIN_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip().rstrip("/")
    if not url.startswith("http"):
        return jsonify({"error": "invalid url"}), 400
    # Para obligar quick tunnel, descomenta:
    # if not TRYCLOUD_PAT.match(url): return jsonify({"error":"url not allowed"}), 400

    set_target(url)
    return {"ok": True, "target": url}, 200

@app.route("/webhook/optimus", methods=["POST", "OPTIONS"])
def proxy_optimus():
    # Preflight CORS (OPTIONS)
    if request.method == "OPTIONS":
        return ("", 204)

    # Auth cliente → Render (opcional pero recomendado)
    if CLIENT_API_KEY and request.headers.get("x-client-key") != CLIENT_API_KEY:
        return jsonify({"error": "forbidden"}), 403

    upstream_base = get_target()
    if not upstream_base:
        return jsonify({"error": "no upstream registered"}), 503

    upstream_url = f"{upstream_base}/webhook/optimus"

    # ✨ SUMA: reenviar headers útiles tal cual, + añadir x-api-key para el backend
    forward_headers = {}
    for k, v in request.headers.items():
        lk = k.lower()
        # No pasamos estos hop-by-hop ni la clave pública al backend
        if lk in ("host", "content-length", "content-encoding", "transfer-encoding", "connection"):
            continue
        if lk == "x-client-key":
            continue
        forward_headers[k] = v
    # Forzamos Content-Type si no viene y añadimos la firma interna
    forward_headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
    forward_headers["x-api-key"] = UPSTREAM_API_KEY

    if DEBUG_HEADERS:
        try:
            print(">> proxy → backend headers:", json.dumps({k: forward_headers[k] for k in sorted(forward_headers)}, ensure_ascii=False))
        except Exception:
            pass

    # reenviamos el JSON tal cual llega
    raw = request.get_data()
    try:
        # si tu IA tarda, puedes subir el timeout
        resp = requests.post(upstream_url, data=raw, headers=forward_headers, timeout=120)
        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
        )
    except Exception as e:
        return jsonify({"error": f"upstream error: {str(e)}"}), 502
