import os, json
from typing import Dict, Optional
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

PUBLIC_API_KEY = os.getenv("PUBLIC_API_KEY", "")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
PORT = int(os.getenv("PORT", "10000"))

try:
    import redis
except Exception:
    redis = None

class AliasStore:
    def __init__(self):
        self._mem: Dict[str, str] = {}
        self._default: Optional[str] = None
        self._target_legacy: Optional[str] = None
        self._r = None
        if REDIS_URL and redis:
            try:
                self._r = redis.from_url(REDIS_URL, decode_responses=True)
            except Exception:
                self._r = None

    def set_target(self, url: str):
        if self._r: self._r.set("optimus:target", url)
        self._target_legacy = url

    def get_target(self) -> Optional[str]:
        if self._r:
            val = self._r.get("optimus:target")
            if val: return val
        return self._target_legacy

    def register_alias(self, alias: str, url: str, default: bool = False):
        a = alias.lower()
        if self._r: self._r.hset("optimus:aliases", a, url)
        self._mem[a] = url
        if default: self.set_default_alias(a)

    def set_default_alias(self, alias: Optional[str]):
        if self._r:
            if alias: self._r.set("optimus:alias_default", alias)
            else: self._r.delete("optimus:alias_default")
        self._default = alias

    def get_default_alias(self) -> Optional[str]:
        if self._r:
            val = self._r.get("optimus:alias_default")
            if val: return val
        return self._default

    def list_aliases(self) -> Dict[str, str]:
        out = dict(self._mem)
        if self._r:
            try: out.update(self._r.hgetall("optimus:aliases") or {})
            except Exception: pass
        return out

    def delete_alias(self, alias: str):
        a = alias.lower()
        if self._r: self._r.hdel("optimus:aliases", a)
        self._mem.pop(a, None)
        if self.get_default_alias() == a:
            self.set_default_alias(None)

    def resolve(self, alias: Optional[str]) -> Optional[str]:
        if alias:
            a = alias.lower()
            all_ = self.list_aliases()
            if a in all_: return all_[a]
        da = self.get_default_alias()
        if da:
            all_ = self.list_aliases()
            if da in all_: return all_[da]
        return self.get_target()

store = AliasStore()

app = Flask(__name__)
origins = ALLOWED_ORIGINS if ALLOWED_ORIGINS else "*"
CORS(app, resources={r"/*": {"origins": origins}},
     supports_credentials=False,
     allow_headers=["Content-Type", "x-client-key", "x-optimus-alias", "x-optimus-model"],
     methods=["GET", "POST", "DELETE", "OPTIONS"])

def require_admin():
    tok = request.headers.get("admin-token")
    return bool(tok and tok == ADMIN_TOKEN)

@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "target": store.get_target(),
        "default_alias": store.get_default_alias(),
        "aliases": store.list_aliases(),
    }), 200

@app.post("/admin/register")
def admin_register():
    if not require_admin(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url: return jsonify({"error": "url requerido"}), 400
    store.set_target(url)
    return jsonify({"ok": True, "target": url}), 200

@app.post("/admin/alias/register")
def admin_alias_register():
    if not require_admin(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    alias = data.get("alias"); url = data.get("url"); default = bool(data.get("default", False))
    if not alias or not url: return jsonify({"error": "alias y url requeridos"}), 400
    store.register_alias(alias, url, default=default)
    return jsonify({"ok": True, "aliases": store.list_aliases(), "default": store.get_default_alias()}), 200

@app.post("/admin/alias/set-default")
def admin_alias_set_default():
    if not require_admin(): return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    alias = data.get("alias")
    if not alias: return jsonify({"error": "alias requerido"}), 400
    store.set_default_alias(alias)
    return jsonify({"ok": True, "default": store.get_default_alias()}), 200

@app.get("/admin/alias/list")
def admin_alias_list():
    if not require_admin(): return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"ok": True, "aliases": store.list_aliases(), "default": store.get_default_alias()}), 200

@app.delete("/admin/alias/<alias>")
def admin_alias_delete(alias):
    if not require_admin(): return jsonify({"error": "Unauthorized"}), 401
    store.delete_alias(alias)
    return jsonify({"ok": True, "aliases": store.list_aliases(), "default": store.get_default_alias()}), 200

@app.route("/webhook/optimus", methods=["POST", "OPTIONS"])
def proxy_webhook():
    if request.method == "OPTIONS":
        return ("", 204)

    if PUBLIC_API_KEY:
        ck = request.headers.get("x-client-key")
        if ck != PUBLIC_API_KEY:
            return jsonify({"error": "Unauthorized client"}), 401

    alias = request.headers.get("x-optimus-alias")
    target = store.resolve(alias)
    if not target:
        return jsonify({"error": "No hay backend registrado"}), 503

    try:
        body = request.get_json(silent=True)
    except Exception:
        body = None

    headers = {"Content-Type": "application/json", "x-api-key": UPSTREAM_API_KEY}
    if request.headers.get("x-optimus-model"):
        headers["x-optimus-model"] = request.headers.get("x-optimus-model")

    try:
        r = requests.post(f"{target.rstrip('/')}/webhook/optimus", json=body, headers=headers, timeout=130)
    except requests.Timeout:
        return jsonify({"error": "timeout upstream"}), 504
    except requests.RequestException as e:
        return jsonify({"error": f"upstream error: {e}"}), 502

    return Response(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "application/json"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
