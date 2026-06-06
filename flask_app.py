import os

from flask import Flask, request, jsonify, session, redirect
from flask_cors import CORS
from datetime import datetime, timezone
from my_database import (
    get_user_progress,
    save_user_progress,
    get_user_badges,
    save_user_badges,
    get_user_profile,
    update_user_profile,
    get_public_profile_by_username,
    report_profile_bio,
    list_bio_reports,
    ban_profile_bio,
    close_bio_report,
    list_all_profiles,
    get_account_ban_status,
    ban_user_account,
    unban_user_account,
    backfill_normalized_user_tables,
    delete_account_data,
    count_users,
    USER_PROGRESS_TABLE,
)
from my_clerk import (
    verify_clerk_token,
    get_last_verification_error,
    get_last_verification_debug,
    delete_clerk_user,
    update_clerk_user_password,
    count_clerk_users,
)
from config import (
    SECRET_KEY,
    TURNSTILE_KEY,
    TURNSTILE_SECRET,
    DISCORD_CLIENT_ID,
    DISCORD_CLIENT_SECRET,
    DISCORD_BOT_TOKEN,
    DISCORD_GUILD_ID,
    DISCORD_JOIN_REDIRECT,
    OPENAI_API_KEY,
)
import config as app_config
import os
import requests
import base64
import json
from urllib.parse import urlencode, urlparse
import time
                                                           
AI_ALLOWED_PATH_PREFIXES = [
    p.strip() for p in os.getenv("AI_ALLOWED_PATH_PREFIXES", "/").split(",") if p.strip()
]
AI_SYSTEM_PROMPTS = {
    "projects-assist": (
        "You are a helpful assistent that helps students out with their questions on the projects they are making. "
        "The student can ask you any questions, as long as they are related to the project, and the project's context you got. "
        "You never write a whole block of code, you help the student and tell them to use their own brain. "
        "It is important to always explain very easy, and support the student. The student is learning, so you should not "
        "give them the answer, but help them to find the answer themselves. You can ask the student questions to help them "
        "find the answer. Always explain very easy, and support the student. Never go off the topic of the projects, "
        "don't answer unrelated questions."
    )
}
AI_MAX_PROMPT_CHARS       = int(os.getenv("AI_MAX_PROMPT_CHARS", "40000"))
AI_MAX_MESSAGES           = int(os.getenv("AI_MAX_MESSAGES", "50"))
AI_TIMEOUT_SECONDS        = int(os.getenv("AI_TIMEOUT_SECONDS", "40"))
AI_RATE_LIMIT_WINDOW_SECONDS  = int(os.getenv("AI_RATE_LIMIT_WINDOW_SECONDS", "60"))
AI_RATE_LIMIT_MAX_REQUESTS    = int(os.getenv("AI_RATE_LIMIT_MAX_REQUESTS", "20"))
_ai_rate: dict = {}
def _get_user_or_ip():
    user_id = session.get("user_id")
    if user_id:
        return f"user:{user_id}"
    return f"ip:{request.headers.get('X-Forwarded-For', request.remote_addr)}"
def _rate_limit_check():
    key  = _get_user_or_ip()
    now  = time.time()
    bucket = [t for t in _ai_rate.get(key, []) if now - t <= AI_RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= AI_RATE_LIMIT_MAX_REQUESTS:
        return False
    bucket.append(now)
    _ai_rate[key] = bucket
    return True
def _is_request_from_allowed_page():
    origin  = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    if origin not in {"https://itlearn.be", "https://it-learn.pages.dev", "https://beta.itlearn.be", "https://it-learn-beta.pages.dev"}:
        return False
    if not referer:
        return False
    try:
        parsed = urlparse(referer)
        if parsed.netloc not in {"itlearn.be", "it-learn.pages.dev"}:
            return False
        path = parsed.path or "/"
        return any(path.startswith(prefix) for prefix in AI_ALLOWED_PATH_PREFIXES)
    except Exception:
        return False
def _extract_messages(payload: dict):
    messages = payload.get("messages")
    if messages is None:
        content = payload.get("content")
        if content is None:
            raise ValueError("Missing 'messages' or 'content'")
        messages = [{"role": "user", "content": content}]
    if not isinstance(messages, list):
        raise ValueError("'messages' must be an array")
    if len(messages) > AI_MAX_MESSAGES:
        raise ValueError(f"Too many messages (max {AI_MAX_MESSAGES})")
    normalized = []
    for m in messages:
        if not isinstance(m, dict):
            raise ValueError("Each message must be an object")
        role    = m.get("role")
        content = m.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("Invalid message role")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Each message 'content' must be a non-empty string")
        normalized.append({"role": role, "content": content})
    return normalized
                                                                             
           
                                                                             
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
)
ALLOWED_CORS_ORIGINS = {
    origin.strip()
    for origin in (
        os.getenv("CORS_ALLOWED_ORIGINS")
        or "https://itlearn.be,https://it-learn.pages.dev,http://localhost:5500,http://127.0.0.1:5500"
    ).split(",")
    if origin.strip()
}
ALLOWED_OAUTH_PROVIDERS = {"google", "github", "discord"}
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_OAUTH_TOKEN     = "https://discord.com/api/oauth2/token"
DISCORD_API_ME          = "https://discord.com/api/users/@me"
DISCORD_APPEAL_URL      = os.getenv("DISCORD_APPEAL_URL") or "https://discord.gg/rKgF9s32EV"
def _admin_user_ids() -> set[str]:
    configured = os.getenv("BIO_MOD_ADMIN_USER_IDS") or getattr(app_config, "BIO_MOD_ADMIN_USER_IDS", "")
    if isinstance(configured, (list, tuple, set)):
        return {str(v).strip() for v in configured if str(v).strip()}
    return {v.strip() for v in str(configured).split(",") if v.strip()}
def _is_bio_moderation_admin(user_id: str | None) -> bool:
    if not user_id:
        return False
    return user_id in _admin_user_ids()
def _ban_payload(ban_status: dict | None = None) -> dict:
    info = ban_status if isinstance(ban_status, dict) else {}
    return {
        "banned":          True,
        "error":           "This account is banned.",
        "ban_reason":      info.get("reason") or "Account suspended for severe rule violations.",
        "ban_appeal_url":  info.get("appeal_url") or DISCORD_APPEAL_URL,
        "ban_appeal_hint": "Appeal in our Discord server via a support ticket.",
        "ban_at":          info.get("banned_at"),
    }
                                                                             
                                                 
                                                                             
def _get_current_user_id() -> str | None:
    """
    Return the current user's Clerk user ID.
    Checks Authorization header (Clerk session token) first, then Flask session.
    This lets API clients send Bearer tokens while the browser uses cookies.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token   = auth_header[7:]
        user_id = verify_clerk_token(token)
        if user_id:
            return user_id
    return session.get("user_id")
def _ensure_progress_exists(user_id: str) -> None:
    """Create a baseline progress row for a brand-new Clerk user."""
    existing = get_user_progress(user_id)
    if isinstance(existing, dict) and "error" in existing:
        save_user_progress(user_id, {
            "progress": {}, "xp": 0, "streak": 0,
            "last_active": None, "missions": {}, "mistakes": [],
        })
        return
    if not existing or not existing.get("progress"):
        payload = {"progress": {}, "xp": 0, "streak": 0, "last_active": None, "missions": {}, "mistakes": []}
        payload.update({k: v for k, v in existing.items() if k not in payload})
        save_user_progress(user_id, payload)
                                                                             
                        
                                                                             
SITE_STATE_PATH             = os.path.join(os.path.dirname(__file__), "site_state.json")
DEFAULT_MAINTENANCE_MESSAGE = "The website is currently under maintenance and temporarily unavailable."
MAINTENANCE_DISCORD_URL     = "https://discord.gg/rKgF9s32EV"
def _default_site_state() -> dict:
    return {
        "maintenance_enabled":   False,
        "maintenance_message":   DEFAULT_MAINTENANCE_MESSAGE,
        "maintenance_discord_url": MAINTENANCE_DISCORD_URL,
        "updated_at":  None,
        "updated_by":  None,
    }
def _load_site_state() -> dict:
    state = _default_site_state()
    try:
        with open(SITE_STATE_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            state.update({key: loaded.get(key, value) for key, value in state.items()})
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[SITE STATE] Failed to load state: {exc}")
    return state
def _save_site_state(state: dict) -> dict:
    payload = _default_site_state()
    if isinstance(state, dict):
        payload.update(state)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(SITE_STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload
def _site_status_payload() -> dict:
    state = _load_site_state()
    return {
        "maintenance_enabled":   bool(state.get("maintenance_enabled")),
        "maintenance_message":   state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE,
        "maintenance_discord_url": MAINTENANCE_DISCORD_URL,
        "updated_at": state.get("updated_at"),
        "updated_by": state.get("updated_by"),
    }
def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False
                                                                             
                  
                                                                             
@app.before_request
def _maintenance_gate():
    if not request.path.startswith("/api/"):
        return None
    if request.method == "OPTIONS":
        return None
    open_paths = {"/api/site/status", "/api/session", "/api/auth/sync", "/api/logout"}
    if request.path in open_paths or request.path.startswith("/api/discord/join/") or request.path.startswith("/api/admin/"):
        return None
    state = _load_site_state()
    if not state.get("maintenance_enabled"):
        return None
    user_id = _get_current_user_id()
    if _is_bio_moderation_admin(user_id):
        return None
    return jsonify({"error": state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE, "maintenance_enabled": True}), 503
      
CORS(app, resources={r"/api/*": {"origins": sorted(ALLOWED_CORS_ORIGINS)}}, supports_credentials=True)
                                                                             
                                 
                                                                             
@app.route("/api/site/status", methods=["GET", "OPTIONS"])
def api_site_status():
    if request.method == "OPTIONS":
        return "", 200
    return jsonify(_site_status_payload())
@app.route("/api/admin/site/maintenance", methods=["POST", "OPTIONS"])
def api_admin_site_maintenance():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data    = request.get_json() or {}
        enabled = _parse_bool(data.get("enabled"))
        message = (data.get("message") or DEFAULT_MAINTENANCE_MESSAGE).strip() or DEFAULT_MAINTENANCE_MESSAGE
        current_state = _load_site_state()
        next_state    = _save_site_state({**current_state, "maintenance_enabled": enabled, "maintenance_message": message, "maintenance_discord_url": MAINTENANCE_DISCORD_URL, "updated_by": user_id})
        return jsonify({"success": True, "maintenance_enabled": bool(next_state.get("maintenance_enabled")), "maintenance_message": next_state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE, "maintenance_discord_url": MAINTENANCE_DISCORD_URL, "updated_at": next_state.get("updated_at"), "updated_by": next_state.get("updated_by")})
    except Exception:
        return jsonify({"error": "Failed to update maintenance state"}), 500
@app.route("/api/admin/system-health", methods=["GET", "OPTIONS"])
def api_admin_system_health():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    scope = (request.args.get("scope") or "full").strip().lower()
    if scope not in {"full", "api", "db", "auth"}:
        scope = "full"
    checked_at = datetime.now(timezone.utc).isoformat()
    api_started = time.perf_counter()
    db_info   = {"online": None, "latency_ms": None, "error": None, "table": USER_PROGRESS_TABLE, "total_profiles": None}
    auth_info = {"online": None, "latency_ms": None, "error": None, "authenticated_users": None}
    reports_info = {"pending_open_reports": None, "sample_limit": 500, "error": None}
    if scope in {"full", "db"}:
        t = time.perf_counter()
        try:
            db_info["total_profiles"] = count_users()
            db_info["online"]         = True
        except Exception as exc:
            db_info["online"] = False
            db_info["error"]  = str(exc)
        finally:
            db_info["latency_ms"] = int((time.perf_counter() - t) * 1000)
    if scope in {"full", "auth"}:
        t = time.perf_counter()
        try:
            total = count_clerk_users()
            auth_info["authenticated_users"] = total
            auth_info["online"]              = True
        except Exception as exc:
            auth_info["online"] = False
            auth_info["error"]  = str(exc)
        finally:
            auth_info["latency_ms"] = int((time.perf_counter() - t) * 1000)
    if scope == "full":
        try:
            open_reports = list_bio_reports(status="open", limit=reports_info["sample_limit"])
            if isinstance(open_reports, dict) and "error" not in open_reports:
                reports_info["pending_open_reports"] = len(open_reports.get("reports") or [])
            else:
                reports_info["error"] = (open_reports or {}).get("error", "Unknown error")
        except Exception as exc:
            reports_info["error"] = str(exc)
    checks        = []
    if scope in {"full", "db"}:   checks.append(bool(db_info.get("online")))
    if scope in {"full", "auth"}: checks.append(bool(auth_info.get("online")))
    overall_online = all(checks) if checks else True
    return jsonify({
        "success": True, "scope": scope, "checked_at": checked_at,
        "overall_status": "online" if overall_online else "degraded",
        "api": {"online": True, "status_code": 200, "response_time_ms": int((time.perf_counter() - api_started) * 1000)},
        "database": db_info, "auth": auth_info, "reports": reports_info,
        "metrics": {"avg_report_load_ms": db_info.get("latency_ms"), "avg_profile_load_ms": auth_info.get("latency_ms"), "uptime_today_pct": None},
    })
                                                                             
                       
                                                                             
def verify_turnstile(token: str, remote_ip: str | None = None) -> tuple[bool, str | None]:
    if not token:
        return False, "Missing CAPTCHA token"
    if not TURNSTILE_SECRET:
        return False, "Server CAPTCHA secret is not configured"
    payload = {"secret": TURNSTILE_SECRET, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        response = requests.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        return False, f"CAPTCHA verification request failed: {exc}"
    except ValueError:
        return False, "CAPTCHA verification returned invalid JSON"
    if result.get("success"):
        return True, None
    error_codes = result.get("error-codes") or []
    if isinstance(error_codes, list) and error_codes:
        return False, f"CAPTCHA rejected token ({', '.join(str(c) for c in error_codes)})"
    return False, "CAPTCHA rejected token"
                                                                             
                    
                                                                             
@app.route("/api/auth/sync", methods=["POST", "OPTIONS"])
def api_auth_sync():
    """
    Called by the frontend after Clerk authentication.
    Accepts a Clerk session token, verifies it, and sets a Flask session.
    Also bootstraps a progress row for first-time users.
    Body: { "token": "<clerk_session_jwt>" }
    """
    if request.method == "OPTIONS":
        return "", 200
    try:
        data  = request.get_json() or {}
        body_token = (
            data.get("token")
            or data.get("sessionToken")
            or data.get("session_token")
        )
        auth_header = request.headers.get("Authorization", "")
        header_token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None

        # Prefer body token, fall back to Authorization header
        token = body_token or header_token

        # Diagnostic: log what we received
        if token:
            preview = token[:20] + "..." if len(token) > 20 else token
            source = "body" if body_token else "Authorization header"
            print(f"[AUTH SYNC] Received token from {source} (len={len(token)}, preview='{preview}')")
        else:
            print(f"[AUTH SYNC] No token in body or Authorization header — body keys: {list(data.keys())}, auth header present: {bool(auth_header)}")

        user_id = verify_clerk_token(token) if token else None
        if not user_id:
            debug_info = {}
            if not token:
                debug_info = {
                    "reason": "no token provided",
                    "body_keys": list(data.keys()),
                    "auth_header_present": bool(auth_header),
                }
            else:
                verification_error = get_last_verification_error() or "unknown"
                verification_debug = get_last_verification_debug()
                source = "body" if body_token else "Authorization header"
                debug_info = {
                    "reason": f"token from {source} did not verify",
                    "verification_error": verification_error,
                    "jwks_url": verification_debug.get("jwks_url"),
                    "issuer": verification_debug.get("issuer"),
                    "token_len": len(token),
                    "token_preview": token[:30] + "..." if len(token) > 30 else token,
                }
            return jsonify({"error": "Invalid or expired session token", "debug": debug_info}), 401
        ban_status = get_account_ban_status(user_id)
        if isinstance(ban_status, dict) and not ban_status.get("error") and ban_status.get("banned"):
            return jsonify(_ban_payload(ban_status)), 403
        _ensure_progress_exists(user_id)
        session["user_id"] = user_id
        return jsonify({
            "success": True,
            "user_id": user_id,
            "is_bio_admin": _is_bio_moderation_admin(user_id),
        })
    except Exception:
        return jsonify({"error": "Auth sync failed"}), 500
@app.route("/api/session", methods=["GET", "OPTIONS"])
def api_session():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if user_id:
        ban_status = get_account_ban_status(user_id)
        if isinstance(ban_status, dict) and not ban_status.get("error") and ban_status.get("banned"):
            session.pop("user_id", None)
            payload = _ban_payload(ban_status)
            payload.update({"logged_in": False, "user_id": None, "is_bio_admin": False})
            return jsonify(payload)
    return jsonify({"logged_in": bool(user_id), "user_id": user_id, "is_bio_admin": _is_bio_moderation_admin(user_id)})
@app.route("/api/logout", methods=["POST", "OPTIONS"])
def api_logout():
    if request.method == "OPTIONS":
        return "", 200
    session.pop("user_id", None)
    return jsonify({"success": True})
                                                                             
                                         
                                                                             
@app.route("/api/change-password", methods=["POST", "OPTIONS"])
def api_change_password():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data         = request.get_json() or {}
        new_password = data.get("new_password")
        if not new_password:
            return jsonify({"error": "Missing new password"}), 400
        if len(new_password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400
        ok = update_clerk_user_password(user_id, new_password)
        if not ok:
            return jsonify({"error": "Password change failed"}), 500
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Password change failed"}), 500
@app.route("/api/delete-account", methods=["POST", "OPTIONS"])
def api_delete_account():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
                                       
        result = delete_account_data(user_id)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
                           
        delete_clerk_user(user_id)
        session.pop("user_id", None)
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Account deletion failed"}), 500
                                                                             
                                                       
                                                                             
@app.route("/api/progress/load", methods=["POST", "OPTIONS"])
def api_load_progress():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        return jsonify(get_user_progress(user_id))
    except Exception:
        return jsonify({"error": "Failed to load progress"}), 500
@app.route("/api/progress/save", methods=["POST", "OPTIONS"])
def api_save_progress():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data          = request.get_json() or {}
        progress_data = data.get("progress_data", {})
        if not isinstance(progress_data, dict):
            progress_data = {}
        result = save_user_progress(user_id, progress_data)
        if "error" in result:
            error_text = str(result["error"])
            if result.get("cheat_detected"):
                return jsonify({"error": "Cheat detected", "cheat_detected": True}), 403
            return jsonify({"error": error_text}), 500
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Failed to save progress"}), 500
@app.route("/api/badges/load", methods=["POST", "OPTIONS"])
def api_load_badges():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        badges_data = get_user_badges(user_id)
        if isinstance(badges_data, dict) and "error" in badges_data:
            return jsonify({"error": badges_data["error"]}), 500
        return jsonify({"badges": badges_data})
    except Exception:
        return jsonify({"error": "Failed to load badges"}), 500
@app.route("/api/badges/save", methods=["POST", "OPTIONS"])
def api_save_badges():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data        = request.get_json() or {}
        badges_data = data.get("badges", {})
        result      = save_user_badges(user_id, badges_data)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Failed to save badges"}), 500
                                                                             
                            
                                                                             
@app.route("/api/profile/me", methods=["GET", "OPTIONS"])
def api_profile_me():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        profile = get_user_profile(user_id)
        if "error" in profile:
            return jsonify({"error": profile["error"]}), 500
        return jsonify(profile)
    except Exception:
        return jsonify({"error": "Failed to load profile"}), 500
@app.route("/api/profile/update", methods=["POST", "OPTIONS"])
def api_profile_update():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data    = request.get_json() or {}
        updated = update_user_profile(user_id, username=data.get("username"), bio=data.get("bio"), avatar_url=data.get("avatar_url"), profile_tagline=data.get("profile_tagline"))
        if "error" in updated:
            return jsonify({"error": updated["error"]}), 400
        return jsonify({"success": True, "profile": updated})
    except Exception:
        return jsonify({"error": "Failed to update profile"}), 500
@app.route("/api/profile/report-bio", methods=["POST", "OPTIONS"])
def api_profile_report_bio():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data     = request.get_json() or {}
        username = data.get("username")
        if not username:
            return jsonify({"error": "Missing username"}), 400
        result = report_profile_bio(username, reporter_user_id=user_id, reason=data.get("reason"), details=data.get("details"))
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Failed to report profile"}), 500
@app.route("/api/profile/<username>", methods=["GET", "OPTIONS"])
def api_profile_public(username: str):
    if request.method == "OPTIONS":
        return "", 200
    try:
        profile = get_public_profile_by_username(username)
        if "error" in profile:
            status = 404 if profile["error"] == "Profile not found" else 400
            return jsonify({"error": profile["error"]}), status
        return jsonify(profile)
    except Exception:
        return jsonify({"error": "Failed to load public profile"}), 500
@app.route("/@<username>", methods=["GET"])
def public_profile_redirect(username: str):
    return redirect(f"/profile.html?u={username}", code=302)
                                                                             
                                     
                                                                             
@app.route("/api/admin/bio-reports", methods=["GET", "OPTIONS"])
def api_admin_bio_reports():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        status = (request.args.get("status") or "open").strip().lower()
        try:
            limit = int(request.args.get("limit") or "100")
        except ValueError:
            limit = 100
        result = list_bio_reports(status=status, limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"reports": result.get("reports", [])})
    except Exception:
        return jsonify({"error": "Failed to fetch reports"}), 500
@app.route("/api/admin/bio-reports/close", methods=["POST", "OPTIONS"])
def api_admin_close_bio_report():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data      = request.get_json() or {}
        report_id = data.get("report_id")
        if not report_id:
            return jsonify({"error": "Missing report_id"}), 400
        result = close_bio_report(report_id=report_id, admin_user_id=user_id, close_note=data.get("close_note"))
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "report": result})
    except Exception:
        return jsonify({"error": "Failed to close report"}), 500
@app.route("/api/admin/profiles", methods=["GET", "OPTIONS"])
def api_admin_profiles():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        try:
            limit = int(request.args.get("limit") or "500")
        except ValueError:
            limit = 500
        result = list_all_profiles(limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"profiles": result.get("profiles", [])})
    except Exception:
        return jsonify({"error": "Failed to list profiles"}), 500
@app.route("/api/admin/profile/ban-bio", methods=["POST", "OPTIONS"])
def api_admin_ban_profile_bio():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data     = request.get_json() or {}
        username = data.get("username")
        if not username:
            return jsonify({"error": "Missing username"}), 400
        result = ban_profile_bio(username, admin_user_id=user_id, replacement_bio=data.get("replacement_bio"))
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception:
        return jsonify({"error": "Failed to ban profile"}), 500
@app.route("/api/admin/profile/ban-account", methods=["POST", "OPTIONS"])
def api_admin_ban_account():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data           = request.get_json() or {}
        target_user_id = str(data.get("user_id") or "").strip()
        if not target_user_id:
            return jsonify({"error": "Missing user_id"}), 400
        result = ban_user_account(target_user_id, admin_user_id=user_id, reason=data.get("reason"), appeal_url=DISCORD_APPEAL_URL)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception:
        return jsonify({"error": "Failed to ban account"}), 500
@app.route("/api/admin/profile/unban-account", methods=["POST", "OPTIONS"])
def api_admin_unban_account():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data           = request.get_json() or {}
        target_user_id = str(data.get("user_id") or "").strip()
        if not target_user_id:
            return jsonify({"error": "Missing user_id"}), 400
        result = unban_user_account(target_user_id, admin_user_id=user_id)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception:
        return jsonify({"error": "Failed to unban account"}), 500
@app.route("/api/admin/backfill-normalized-tables", methods=["POST", "OPTIONS"])
def api_admin_backfill_normalized_tables():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403
    try:
        data = request.get_json() or {}
        try:
            limit = int(data.get("limit", 0))
        except (TypeError, ValueError):
            limit = 0
        result = backfill_normalized_user_tables(limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify(result)
    except Exception:
        return jsonify({"error": "Backfill failed"}), 500
                                                                             
            
                                                                             
@app.route("/api/user-count", methods=["GET"])
def api_user_count():
    try:
        return jsonify({"totalUsers": count_users()})
    except Exception:
        return jsonify({"error": "Failed to count users"}), 500
                                                                             
                        
                                                                             
@app.route("/api/trial/link", methods=["POST", "OPTIONS"])
def api_trial_link():
    if request.method == "OPTIONS":
        return "", 200
    user_id = _get_current_user_id()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data       = request.get_json()
        if not data:
            return jsonify({"error": "No trial data provided"}), 400
        session_id = data.get("sessionId")
        course_id  = data.get("courseId")
        chapters   = data.get("chapters", [])
        if not session_id or not course_id:
            return jsonify({"error": "Missing required trial data"}), 400
        existing_progress = get_user_progress(user_id)
        if "error" in existing_progress:
            existing_progress = {"progress": {}, "xp": 0, "streak": 0, "last_active": None, "missions": {}, "mistakes": []}
        if "progress" not in existing_progress:
            existing_progress["progress"] = {}
        if course_id not in existing_progress["progress"]:
            existing_progress["progress"][course_id] = {"started": True, "chapters": {}}
        if "chapters" not in existing_progress["progress"][course_id]:
            existing_progress["progress"][course_id]["chapters"] = {}
        for chapter in chapters:
            chapter_id   = chapter.get("chapterId")
            chapter_data = chapter.get("data", {})
            if chapter_id:
                if chapter_id not in existing_progress["progress"][course_id]["chapters"]:
                    existing_progress["progress"][course_id]["chapters"][chapter_id] = {}
                existing_progress["progress"][course_id]["chapters"][chapter_id].update(chapter_data)
        result = save_user_progress(user_id, existing_progress)
        if "error" in result:
            return jsonify({"error": "Failed to save progress: " + result["error"]}), 500
        return jsonify({"success": True, "message": "Trial progress linked successfully", "chapters_linked": len(chapters)})
    except Exception:
        return jsonify({"error": "Failed to link trial progress"}), 500
                                                                             
                                
                                                                             
def _discord_is_configured():
    return all([DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DISCORD_JOIN_REDIRECT])
@app.route("/api/discord/join/start", methods=["GET"])
def discord_join_start():
    if not _discord_is_configured():
        return jsonify({"error": "Discord join not configured"}), 400
    next_url  = request.args.get("next", "/learn/index.html")
    state     = base64.urlsafe_b64encode(json.dumps({"next": next_url}).encode()).decode()
    params    = {"client_id": DISCORD_CLIENT_ID, "response_type": "code", "redirect_uri": DISCORD_JOIN_REDIRECT, "scope": "identify email guilds.join", "state": state}
    return jsonify({"url": DISCORD_OAUTH_AUTHORIZE + "?" + urlencode(params)})
@app.route("/api/discord/join/callback", methods=["GET"])
def discord_join_callback():
    if not _discord_is_configured():
        return jsonify({"error": "Discord join not configured"}), 400
    error = request.args.get("error")
    if error:
        return _discord_join_redirect("Discord authorization was cancelled.")
    code = request.args.get("code")
    if not code:
        return _discord_join_redirect("Missing Discord authorization code.")
    state_raw = request.args.get("state", "")
    next_url  = "/learn/index.html"
    try:
        decoded  = json.loads(base64.urlsafe_b64decode(state_raw + "==").decode()) if state_raw else {}
        next_url = decoded.get("next", next_url)
    except Exception:
        pass
    try:
        token_res = requests.post(DISCORD_OAUTH_TOKEN, data={"client_id": DISCORD_CLIENT_ID, "client_secret": DISCORD_CLIENT_SECRET, "grant_type": "authorization_code", "code": code, "redirect_uri": DISCORD_JOIN_REDIRECT}, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=10)
        token_res.raise_for_status()
        tokens           = token_res.json()
        user_access_token = tokens.get("access_token")
        token_type        = tokens.get("token_type", "Bearer")
        if not user_access_token:
            return _discord_join_redirect("No access token returned by Discord.")
        user_res = requests.get(DISCORD_API_ME, headers={"Authorization": f"{token_type} {user_access_token}"}, timeout=10)
        user_res.raise_for_status()
        user_obj = user_res.json() or {}
        user_id  = user_obj.get("id")
        if not user_id:
            return _discord_join_redirect("Could not fetch Discord user.")
        add_res = requests.put(f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{user_id}", headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}, json={"access_token": user_access_token}, timeout=10)
        if add_res.status_code not in (200, 201, 204):
            return _discord_join_redirect(f"Failed to add you to the Discord server.")
        return _discord_join_redirect(None, next_url)
    except Exception as exc:
        return _discord_join_redirect(f"Discord join error: {type(exc).__name__}")
def _discord_join_redirect(message: str | None, next_url: str = "/learn/index.html"):
    target = next_url
    if message:
        sep    = "&" if "?" in target else "?"
        target = f"{target}{sep}discord_join_error={requests.utils.quote(message)}"
    return redirect(target, code=302)
                                                                             
                      
                                                                             
@app.route("/api/ai", methods=["POST", "OPTIONS"])
def api_ai():
    if request.method == "OPTIONS":
        return "", 200
    if not OPENAI_API_KEY:
        return jsonify({"error": "Server misconfigured: missing OPENAI_API_KEY"}), 500
    if not _is_request_from_allowed_page():
        return jsonify({"error": "Forbidden"}), 403
    if not _rate_limit_check():
        return jsonify({"error": "Rate limit exceeded"}), 429
    system_prompt_key = request.args.get("p", "default")
    system_prompt     = AI_SYSTEM_PROMPTS.get(system_prompt_key)
    if not system_prompt:
        return jsonify({"error": "Unknown system prompt key"}), 400
    try:
        payload  = request.get_json(force=True) or {}
        messages = _extract_messages(payload)
        if sum(len(m.get("content", "")) for m in messages) > AI_MAX_PROMPT_CHARS:
            return jsonify({"error": f"Request too large (max {AI_MAX_PROMPT_CHARS} chars)"}), 413
        model       = payload.get("model", "google/gemini-3.1-flash-lite")
        temperature = payload.get("temperature", 0.2)
        resp = requests.post(
            "https://ai.hackclub.com/proxy/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "system", "content": system_prompt}] + messages, "temperature": temperature},
            timeout=AI_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict) and data:
                return jsonify({"error": data.get("error", data), "status": resp.status_code}), resp.status_code
            text = (resp.text or "").strip()
            if len(text) > 2000:
                text = text[:2000] + "...(truncated)"
            return jsonify({"error": "OpenAI request failed", "status": resp.status_code, "body": text}), resp.status_code
        data    = resp.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "content": content, "model": model})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    def _parse_bool(value):
        return str(value).strip().lower() in {"1", "true"}

    debug_value = _parse_bool(os.getenv("FLASK_DEBUG"))
    host_value = os.getenv("FLASK_HOST", "127.0.0.1")
    port_value = int(os.getenv("FLASK_PORT", "5000"))

    app.run(debug=debug_value, host=host_value, port=port_value)
