from flask import Flask, request, jsonify, session, redirect
from flask_cors import CORS
from datetime import datetime, timezone
from my_supabase import (
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
    signup,
    login,
    change_password,
    delete_account,
    get_admin_client,
    supabase,
    USER_PROGRESS_TABLE,
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
)
import config as app_config
import os
import requests
import base64
import json
from urllib.parse import urlencode
import time


def _coerce_users_list(value):
    """Normalize supported users containers to a list."""
    if isinstance(value, list):
        # Some Supabase SDK versions return list_users() as a direct list of User objects.
        if not value:
            return value

        first = value[0]
        if isinstance(first, dict):
            if "id" in first or "user_id" in first or "email" in first:
                return value
        else:
            if hasattr(first, "id") or hasattr(first, "email"):
                return value
        return value
    if isinstance(value, tuple):
        return list(value)
    return None


def _extract_users_from_admin_response(response_obj):
    """Best-effort extractor for Supabase admin.list_users() responses across SDK versions."""
    if response_obj is None:
        return None

    if isinstance(response_obj, dict):
        users = _coerce_users_list(response_obj.get("users"))
        if users is not None:
            return users

        data = response_obj.get("data")
        if isinstance(data, dict):
            users = _coerce_users_list(data.get("users"))
            if users is not None:
                return users

        # Some SDK responses may expose payload under nested keys.
        for key in ("result", "body", "payload"):
            nested = response_obj.get(key)
            extracted = _extract_users_from_admin_response(nested)
            if extracted is not None:
                return extracted
        return None

    if isinstance(response_obj, (list, tuple)):
        direct_users = _coerce_users_list(response_obj)
        if direct_users is not None:
            return direct_users

        for item in response_obj:
            extracted = _extract_users_from_admin_response(item)
            if extracted is not None:
                return extracted
        return None

    model_dump_fn = getattr(response_obj, "model_dump", None)
    if callable(model_dump_fn):
        try:
            dumped = model_dump_fn()
            extracted = _extract_users_from_admin_response(dumped)
            if extracted is not None:
                return extracted
        except Exception:
            pass

    dict_fn = getattr(response_obj, "dict", None)
    if callable(dict_fn):
        try:
            dumped = dict_fn()
            extracted = _extract_users_from_admin_response(dumped)
            if extracted is not None:
                return extracted
        except Exception:
            pass

    users_attr = _coerce_users_list(getattr(response_obj, "users", None))
    if users_attr is not None:
        return users_attr

    data_attr = getattr(response_obj, "data", None)
    if isinstance(data_attr, dict):
        users = _coerce_users_list(data_attr.get("users"))
        if users is not None:
            return users

    # If .data is a custom object, recurse into it.
    extracted_from_data = _extract_users_from_admin_response(data_attr)
    if extracted_from_data is not None:
        return extracted_from_data

    nested_users = _coerce_users_list(getattr(data_attr, "users", None))
    if nested_users is not None:
        return nested_users

    # Final fallback: inspect public attributes for nested payload-like objects.
    attrs = getattr(response_obj, "__dict__", None)
    if isinstance(attrs, dict):
        extracted = _extract_users_from_admin_response(attrs)
        if extracted is not None:
            return extracted

    return None


def _count_auth_users() -> tuple[int | None, str | None]:
    """Return (total_auth_users, error_message)."""
    try:
        admin_api = get_admin_client().auth.admin
        page = 1
        per_page = 1000
        total = 0

        while True:
            try:
                response_obj = admin_api.list_users(page=page, per_page=per_page)
            except TypeError:
                response_obj = admin_api.list_users({"page": page, "per_page": per_page})

            users = _extract_users_from_admin_response(response_obj)
            if users is None:
                response_type = type(response_obj).__name__
                attrs = []
                try:
                    attrs = [k for k in dir(response_obj) if not k.startswith("_")][:30]
                except Exception:
                    attrs = []

                preview = ""
                try:
                    preview = repr(response_obj)
                except Exception:
                    preview = "<repr unavailable>"

                return None, (
                    "Could not parse users from admin.list_users response | "
                    f"type={response_type} | attrs={attrs} | preview={preview[:400]}"
                )

            total += len(users)
            if len(users) < per_page:
                break
            page += 1

        return total, None
    except Exception as exc:
        return None, str(exc)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
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
DISCORD_OAUTH_TOKEN = "https://discord.com/api/oauth2/token"
DISCORD_API_ME = "https://discord.com/api/users/@me"
DISCORD_APPEAL_URL = os.getenv("DISCORD_APPEAL_URL") or "https://discord.gg/rKgF9s32EV"


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
        "banned": True,
        "error": "This account is banned.",
        "ban_reason": info.get("reason") or "Account suspended for severe rule violations.",
        "ban_appeal_url": info.get("appeal_url") or DISCORD_APPEAL_URL,
        "ban_appeal_hint": "Appeal in our Discord server via a support ticket.",
        "ban_at": info.get("banned_at"),
    }


SITE_STATE_PATH = os.path.join(os.path.dirname(__file__), "site_state.json")
DEFAULT_MAINTENANCE_MESSAGE = "The website is currently under maintenance and temporarily unavailable."
MAINTENANCE_DISCORD_URL = "https://discord.gg/rKgF9s32EV"


def _default_site_state() -> dict:
    return {
        "maintenance_enabled": False,
        "maintenance_message": DEFAULT_MAINTENANCE_MESSAGE,
        "maintenance_discord_url": MAINTENANCE_DISCORD_URL,
        "updated_at": None,
        "updated_by": None,
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
        "maintenance_enabled": bool(state.get("maintenance_enabled")),
        "maintenance_message": state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE,
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

    open_paths = {
        "/api/site/status",
        "/api/session",
        "/api/login",
        "/api/signup",
        "/api/logout",
    }
    if request.path in open_paths or request.path.startswith("/api/discord/join/") or request.path.startswith("/api/admin/"):
        return None

    state = _load_site_state()
    if not state.get("maintenance_enabled"):
        return None

    user_id = session.get("user_id")
    if _is_bio_moderation_admin(user_id):
        return None

    return jsonify({
        "error": state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE,
        "maintenance_enabled": True,
    }), 503


@app.route("/api/admin/site/maintenance", methods=["POST", "OPTIONS"])
def api_admin_site_maintenance():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        enabled = _parse_bool(data.get("enabled"))
        message = (data.get("message") or DEFAULT_MAINTENANCE_MESSAGE).strip() or DEFAULT_MAINTENANCE_MESSAGE

        current_state = _load_site_state()
        next_state = {
            "maintenance_enabled": enabled,
            "maintenance_message": message,
            "maintenance_discord_url": MAINTENANCE_DISCORD_URL,
            "updated_by": user_id,
        }
        next_state = _save_site_state({**current_state, **next_state})

        return jsonify({
            "success": True,
            "maintenance_enabled": bool(next_state.get("maintenance_enabled")),
            "maintenance_message": next_state.get("maintenance_message") or DEFAULT_MAINTENANCE_MESSAGE,
            "maintenance_discord_url": MAINTENANCE_DISCORD_URL,
            "updated_at": next_state.get("updated_at"),
            "updated_by": next_state.get("updated_by"),
        })
    except Exception as e:
        return jsonify({"error": "Signup failed"}), 500

# ------------------- CORS -------------------
# Allow the trusted frontends to call the API with credentials.
CORS(
    app,
    resources={r"/api/*": {"origins": sorted(ALLOWED_CORS_ORIGINS)}},
    supports_credentials=True,
)


@app.route("/api/site/status", methods=["GET", "OPTIONS"])
def api_site_status():
    if request.method == "OPTIONS":
        return "", 200
    return jsonify(_site_status_payload())


@app.route("/api/admin/system-health", methods=["GET", "OPTIONS"])
def api_admin_system_health():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    scope = (request.args.get("scope") or "full").strip().lower()
    allowed_scopes = {"full", "api", "db", "auth"}
    if scope not in allowed_scopes:
        scope = "full"

    checked_at = datetime.now(timezone.utc).isoformat()

    api_started = time.perf_counter()
    api_info = {
        "online": True,
        "status_code": 200,
        "response_time_ms": 0,
    }

    db_info = {
        "online": None,
        "latency_ms": None,
        "error": None,
        "url": getattr(app_config, "SUPABASE_URL", ""),
        "table": USER_PROGRESS_TABLE,
        "total_profiles": None,
    }

    auth_info = {
        "online": None,
        "latency_ms": None,
        "error": None,
        "authenticated_users": None,
    }

    reports_info = {
        "pending_open_reports": None,
        "sample_limit": 500,
        "error": None,
    }

    if scope in {"full", "db"}:
        db_started = time.perf_counter()
        try:
            db_resp = get_admin_client().table(USER_PROGRESS_TABLE).select("user_id", count="exact").limit(1).execute()
            counted = getattr(db_resp, "count", None)
            if not isinstance(counted, int):
                counted = len(getattr(db_resp, "data", []) or [])

            db_info["online"] = True
            db_info["total_profiles"] = int(counted)
        except Exception as exc:
            db_info["online"] = False
            db_info["error"] = str(exc)
        finally:
            db_info["latency_ms"] = int((time.perf_counter() - db_started) * 1000)

    if scope in {"full", "auth"}:
        auth_started = time.perf_counter()
        try:
            total_auth_users, auth_error = _count_auth_users()
            auth_info["authenticated_users"] = total_auth_users
            auth_info["error"] = auth_error
            auth_info["online"] = total_auth_users is not None and not auth_error
        except Exception as exc:
            auth_info["online"] = False
            auth_info["error"] = str(exc)
        finally:
            auth_info["latency_ms"] = int((time.perf_counter() - auth_started) * 1000)

    if scope == "full":
        try:
            open_reports = list_bio_reports(status="open", limit=reports_info["sample_limit"])
            if isinstance(open_reports, dict) and "error" not in open_reports:
                reports_info["pending_open_reports"] = len(open_reports.get("reports") or [])
            else:
                reports_info["error"] = (open_reports or {}).get("error", "Unknown error")
        except Exception as exc:
            reports_info["error"] = str(exc)

    api_info["response_time_ms"] = int((time.perf_counter() - api_started) * 1000)

    checks = []
    if scope in {"full", "db"}:
        checks.append(bool(db_info.get("online")))
    if scope in {"full", "auth"}:
        checks.append(bool(auth_info.get("online")))

    overall_online = all(checks) if checks else True

    avg_report_load = None
    avg_profile_load = None
    if scope == "full":
        avg_report_load = db_info.get("latency_ms")
        avg_profile_load = auth_info.get("latency_ms")

    return jsonify({
        "success": True,
        "scope": scope,
        "checked_at": checked_at,
        "overall_status": "online" if overall_online else "degraded",
        "api": api_info,
        "database": db_info,
        "auth": auth_info,
        "reports": reports_info,
        "metrics": {
            "avg_report_load_ms": avg_report_load,
            "avg_profile_load_ms": avg_profile_load,
            "uptime_today_pct": None,
        },
    })

# ------------------- Turnstile Validation -------------------
def verify_turnstile(token: str, remote_ip: str | None = None) -> tuple[bool, str | None]:
    """Verify Cloudflare Turnstile CAPTCHA and return a diagnostic error when available."""
    if not token:
        return False, "Missing CAPTCHA token"

    if not TURNSTILE_SECRET:
        return False, "Server CAPTCHA secret is not configured"

    payload = {"secret": TURNSTILE_SECRET, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            timeout=10,
        )
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


def ensure_user_progress_exists(user_id: str):
    """Guarantee a progress row exists for OAuth sign-ins."""
    default_progress = {
        "progress": {},
        "xp": 0,
        "streak": 0,
        "last_active": None,
        "missions": {},
        "mistakes": []
    }

    existing = get_user_progress(user_id)

    if isinstance(existing, dict) and "error" in existing:
        save_user_progress(user_id, default_progress)
        return

    # When the row does not yet exist, ensure we upsert a baseline entry.
    if not existing or not existing.get("progress"):
        payload = default_progress.copy()
        payload.update({k: v for k, v in existing.items() if k not in payload})
        save_user_progress(user_id, payload)

# ------------------- PROGRESS -------------------
@app.route("/api/progress/load", methods=["POST", "OPTIONS"])
def api_load_progress():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        progress_data = get_user_progress(user_id)
        return jsonify(progress_data)
    except Exception as e:
        return jsonify({"error": "Failed to load progress"}), 500

@app.route("/api/progress/save", methods=["POST", "OPTIONS"])
def api_save_progress():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data = request.get_json() or {}
        progress_data = data.get("progress_data", {})
        if not isinstance(progress_data, dict):
            progress_data = {}

        result = save_user_progress(user_id, progress_data)
        if "error" in result:
            error_text = str(result["error"])
            if result.get("cheat_detected"):
                return jsonify({"error": "Cheat detected", "cheat_detected": True}), 403
            if "row-level security" in error_text.lower() or "42501" in error_text:
                return jsonify({"error": "Progress save blocked by database policy."}), 403
            return jsonify({"error": error_text}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to save progress"}), 500

@app.route("/api/badges/load", methods=["POST", "OPTIONS"])
def api_load_badges():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        badges_data = get_user_badges(user_id)
        if isinstance(badges_data, dict) and "error" in badges_data:
            return jsonify({"error": badges_data["error"]}), 500
        return jsonify({"badges": badges_data})
    except Exception as e:
        return jsonify({"error": "Failed to load badges"}), 500

@app.route("/api/badges/save", methods=["POST", "OPTIONS"])
def api_save_badges():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data = request.get_json() or {}
        badges_data = data.get("badges", {})
        result = save_user_badges(user_id, badges_data)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to save badges"}), 500

# ------------------- AUTH -------------------
@app.route("/api/signup", methods=["POST", "OPTIONS"])
def api_signup():
    if request.method == "OPTIONS":
        return "", 200
    try:
        data = request.get_json() or {}
        email = data.get("email")
        password = data.get("password")
        captcha_token = data.get("captcha_token")

        if not email or not password:
            return jsonify({"error": "Missing email or password"}), 400

        if not captcha_token:
            return jsonify({"error": "CAPTCHA token missing"}), 400

        result = signup(email, password, captcha_token=captcha_token)
        if "error" in result:
            error_message = str(result["error"])
            status_code = 400 if "captcha" in error_message.lower() else 500
            return jsonify({"error": error_message}), status_code

        user_id = result["user_id"]
        save_user_progress(user_id, {
            "progress": {},
            "xp": 0,
            "streak": 0,
            "last_active": None,
            "missions": {},
            "mistakes": []
        })

        session["user_id"] = user_id
        return jsonify({"success": True, "user_id": user_id})

    except Exception as e:
        return jsonify({"error": "Signup failed"}), 500

@app.route("/api/login", methods=["POST", "OPTIONS"])
def api_login():
    if request.method == "OPTIONS":
        return "", 200
    try:
        data = request.get_json() or {}
        email = data.get("email")
        password = data.get("password")
        captcha_token = data.get("captcha_token")

        if not email or not password:
            return jsonify({"error": "Missing email or password"}), 400

        if not captcha_token:
            return jsonify({"error": "CAPTCHA token missing"}), 400

        # Attempt login
        result = login(email, password, captcha_token=captcha_token)
        print(f"[LOGIN] Result: {'success' if 'success' in result else 'error'}")
        
        if "error" in result:
            error_message = str(result["error"])
            status_code = 400 if "captcha" in error_message.lower() else 401
            return jsonify({"error": error_message}), status_code

        user_id = result["user_id"]
        ban_status = get_account_ban_status(user_id)
        if isinstance(ban_status, dict) and not ban_status.get("error") and ban_status.get("banned"):
            return jsonify(_ban_payload(ban_status)), 403

        session["user_id"] = user_id
        return jsonify({"success": True, "user_id": user_id})
    except Exception as e:
        print("[LOGIN] Exception during login")
        return jsonify({"error": "Login failed"}), 500


# ------------------- OAUTH -------------------
def _validate_provider(provider: str) -> str:
    normalized = (provider or "").lower()
    if normalized not in ALLOWED_OAUTH_PROVIDERS:
        return ""
    return normalized


@app.route("/api/oauth/<provider>/start", methods=["POST", "OPTIONS"])
def api_oauth_start(provider: str):
    if request.method == "OPTIONS":
        return "", 200

    normalized = _validate_provider(provider)
    if not normalized:
        return jsonify({"error": "Unsupported provider"}), 400

    try:
        data = request.get_json() or {}
        redirect_to = data.get("redirect_to")
        if not redirect_to:
            return jsonify({"error": "Missing redirect_to"}), 400

        # Force implicit flow so access tokens return in the redirect fragment.
        query_params = {"flow_type": "implicit"}

        # Ensure Google grants refresh capability and consent prompt.
        if normalized == "google":
            query_params.update({"access_type": "offline", "prompt": "consent"})

        oauth_res = supabase.auth.sign_in_with_oauth({
            "provider": normalized,
            "options": {
                "redirect_to": redirect_to,
                "query_params": query_params
            }
        })

        # supabase-py returns an object with a .url attribute
        url = getattr(oauth_res, "url", None)
        if not url and isinstance(oauth_res, dict):
            url = oauth_res.get("url")

        if not url:
            return jsonify({"error": "Unable to start OAuth flow"}), 500

        return jsonify({"url": url})
    except Exception as e:
        print(f"[OAUTH][{normalized}] Start error")
        return jsonify({"error": "Failed to start OAuth flow"}), 500


@app.route("/api/oauth/<provider>/complete", methods=["POST", "OPTIONS"])
def api_oauth_complete(provider: str):
    if request.method == "OPTIONS":
        return "", 200

    normalized = _validate_provider(provider)
    if not normalized:
        return jsonify({"error": "Unsupported provider"}), 400

    try:
        data = request.get_json() or {}
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        auth_code = data.get("auth_code")

        # Allow exchanging an auth code (PKCE) when no tokens are returned to the client.
        if not access_token and auth_code:
            try:
                exchange = supabase.auth.exchange_code_for_session({"auth_code": auth_code})
                session_obj = getattr(exchange, "session", None)
                access_token = getattr(session_obj, "access_token", None)
                refresh_token = getattr(session_obj, "refresh_token", None)
            except Exception as exc:
                print(f"[OAUTH][{normalized}] Code exchange failed")
                return jsonify({"error": "Failed to exchange auth code"}), 400

        # GitHub does not always return a refresh token; accept access token alone.
        if not access_token:
            return jsonify({"error": "Missing access token from provider"}), 400

        # Validate token and get user info from Supabase
        user_resp = supabase.auth.get_user(access_token)
        user_obj = getattr(user_resp, "user", None)
        user_id = getattr(user_obj, "id", None) if user_obj else None

        if not user_id:
            return jsonify({"error": "Could not fetch user from provider"}), 401

        ensure_user_progress_exists(user_id)

        ban_status = get_account_ban_status(user_id)
        if isinstance(ban_status, dict) and not ban_status.get("error") and ban_status.get("banned"):
            return jsonify(_ban_payload(ban_status)), 403

        # Persist session and progress
        session["user_id"] = user_id

        return jsonify({"success": True, "user_id": user_id, "provider": normalized})
    except Exception as e:
        print(f"[OAUTH][{normalized}] Complete error")
        return jsonify({"error": "OAuth completion failed"}), 500


# ------------------- DISCORD GUILD JOIN -------------------
def _discord_is_configured():
    return all([
        DISCORD_CLIENT_ID,
        DISCORD_CLIENT_SECRET,
        DISCORD_BOT_TOKEN,
        DISCORD_GUILD_ID,
        DISCORD_JOIN_REDIRECT,
    ])


@app.route("/api/discord/join/start", methods=["GET"])
def discord_join_start():
    """Begin a Discord OAuth flow to add the user to the guild using guilds.join."""
    if not _discord_is_configured():
        return jsonify({"error": "Discord join not configured"}), 400

    next_url = request.args.get("next", "/learn/index.html")
    state_obj = {"next": next_url}
    state = base64.urlsafe_b64encode(json.dumps(state_obj).encode()).decode()

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": DISCORD_JOIN_REDIRECT,
        "scope": "identify email guilds.join",
        "state": state,
    }

    url = DISCORD_OAUTH_AUTHORIZE + "?" + urlencode(params)
    return jsonify({"url": url})


@app.route("/api/discord/join/callback", methods=["GET"])
def discord_join_callback():
    """Handle Discord OAuth redirect, exchange code, and add the user to the guild."""
    if not _discord_is_configured():
        return jsonify({"error": "Discord join not configured"}), 400

    error = request.args.get("error")
    if error:
        return _discord_join_redirect("Discord authorization was cancelled.")

    code = request.args.get("code")
    if not code:
        return _discord_join_redirect("Missing Discord authorization code.")

    state_raw = request.args.get("state", "")
    next_url = "/learn/index.html"
    try:
        decoded = json.loads(base64.urlsafe_b64decode(state_raw + "==").decode()) if state_raw else {}
        next_url = decoded.get("next", next_url)
    except Exception:
        pass

    try:
        token_data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_JOIN_REDIRECT,
        }
        print("[DISCORD JOIN] Token exchange started")
        
        token_res = requests.post(
            DISCORD_OAUTH_TOKEN,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        
        if not token_res.ok:
            print(f"[DISCORD JOIN] Token exchange failed: {token_res.status_code}")
        
        token_res.raise_for_status()
        tokens = token_res.json()
        user_access_token = tokens.get("access_token")
        token_type = tokens.get("token_type", "Bearer")

        if not user_access_token:
            return _discord_join_redirect("No access token returned by Discord.")

        user_res = requests.get(
            DISCORD_API_ME,
            headers={"Authorization": f"{token_type} {user_access_token}"},
            timeout=10,
        )
        user_res.raise_for_status()
        user_obj = user_res.json() or {}
        user_id = user_obj.get("id")

        if not user_id:
            return _discord_join_redirect("Could not fetch Discord user.")

        add_res = requests.put(
            f"https://discord.com/api/guilds/{DISCORD_GUILD_ID}/members/{user_id}",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"access_token": user_access_token},
            timeout=10,
        )

        if add_res.status_code not in (200, 201, 204):
            msg = _safe_discord_error(add_res)
            return _discord_join_redirect(f"Failed to add you to the Discord server: {msg}")

        return _discord_join_redirect(None, next_url)
    except Exception as exc:
        error_msg = type(exc).__name__
        print("[DISCORD JOIN] Exception during Discord join")
        return _discord_join_redirect(f"Discord join error: {error_msg}")


def _discord_join_redirect(message: str | None, next_url: str = "/learn/index.html"):
    """Redirect back to the app with optional error message."""
    target = next_url
    if message:
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}discord_join_error={requests.utils.quote(message)}"
    return redirect(target, code=302)


def _safe_discord_error(response: requests.Response) -> str:
    try:
        data = response.json()
        return data.get("message") or response.text
    except Exception:
        return response.text

# ------------------- SESSION -------------------
@app.route("/api/session", methods=["GET", "OPTIONS"])
def api_session():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if user_id:
        ban_status = get_account_ban_status(user_id)
        if isinstance(ban_status, dict) and not ban_status.get("error") and ban_status.get("banned"):
            session.pop("user_id", None)
            payload = _ban_payload(ban_status)
            payload.update({
                "logged_in": False,
                "user_id": None,
                "is_bio_admin": False,
            })
            return jsonify(payload)

    return jsonify({
        "logged_in": bool(user_id),
        "user_id": user_id,
        "is_bio_admin": _is_bio_moderation_admin(user_id),
    })

@app.route("/api/profile/me", methods=["GET", "OPTIONS"])
def api_profile_me():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        profile = get_user_profile(user_id)
        if "error" in profile:
            return jsonify({"error": profile["error"]}), 500
        return jsonify(profile)
    except Exception as e:
        return jsonify({"error": "Failed to load profile"}), 500

@app.route("/api/profile/update", methods=["POST", "OPTIONS"])
def api_profile_update():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    try:
        data = request.get_json() or {}
        username = data.get("username")
        bio = data.get("bio")
        avatar_url = data.get("avatar_url")
        profile_tagline = data.get("profile_tagline")
        updated = update_user_profile(
            user_id,
            username=username,
            bio=bio,
            avatar_url=avatar_url,
            profile_tagline=profile_tagline,
        )
        if "error" in updated:
            return jsonify({"error": updated["error"]}), 400
        return jsonify({"success": True, "profile": updated})
    except Exception as e:
        return jsonify({"error": "Failed to update profile"}), 500


@app.route("/api/profile/report-bio", methods=["POST", "OPTIONS"])
def api_profile_report_bio():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    try:
        data = request.get_json() or {}
        username = data.get("username")
        reason = data.get("reason")
        details = data.get("details")

        if not username:
            return jsonify({"error": "Missing username"}), 400

        result = report_profile_bio(username, reporter_user_id=user_id, reason=reason, details=details)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to report profile"}), 500


@app.route("/api/admin/bio-reports", methods=["GET", "OPTIONS"])
def api_admin_bio_reports():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        status = (request.args.get("status") or "open").strip().lower()
        limit_raw = request.args.get("limit") or "100"
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 100

        result = list_bio_reports(status=status, limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"reports": result.get("reports", [])})
    except Exception as e:
        return jsonify({"error": "Failed to fetch reports"}), 500


@app.route("/api/admin/bio-reports/close", methods=["POST", "OPTIONS"])
def api_admin_close_bio_report():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        report_id = data.get("report_id")
        close_note = data.get("close_note")
        if not report_id:
            return jsonify({"error": "Missing report_id"}), 400

        result = close_bio_report(report_id=report_id, admin_user_id=user_id, close_note=close_note)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "report": result})
    except Exception as e:
        return jsonify({"error": "Failed to close report"}), 500


@app.route("/api/admin/profiles", methods=["GET", "OPTIONS"])
def api_admin_profiles():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        limit_raw = request.args.get("limit") or "500"
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 500

        result = list_all_profiles(limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify({"profiles": result.get("profiles", [])})
    except Exception as e:
        return jsonify({"error": "Failed to list profiles"}), 500


@app.route("/api/admin/profile/ban-bio", methods=["POST", "OPTIONS"])
def api_admin_ban_profile_bio():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        username = data.get("username")
        replacement_bio = data.get("replacement_bio")
        if not username:
            return jsonify({"error": "Missing username"}), 400

        result = ban_profile_bio(username, admin_user_id=user_id, replacement_bio=replacement_bio)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception as e:
        return jsonify({"error": "Failed to ban profile"}), 500


@app.route("/api/admin/profile/ban-account", methods=["POST", "OPTIONS"])
def api_admin_ban_account():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        target_user_id = str(data.get("user_id") or "").strip()
        reason = data.get("reason")

        if not target_user_id:
            return jsonify({"error": "Missing user_id"}), 400

        result = ban_user_account(
            target_user_id,
            admin_user_id=user_id,
            reason=reason,
            appeal_url=DISCORD_APPEAL_URL,
        )
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception as e:
        return jsonify({"error": "Failed to ban account"}), 500


@app.route("/api/admin/profile/unban-account", methods=["POST", "OPTIONS"])
def api_admin_unban_account():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        target_user_id = str(data.get("user_id") or "").strip()

        if not target_user_id:
            return jsonify({"error": "Missing user_id"}), 400

        result = unban_user_account(target_user_id, admin_user_id=user_id)
        if "error" in result:
            return jsonify({"error": result["error"]}), 400
        return jsonify({"success": True, "profile": result})
    except Exception as e:
        return jsonify({"error": "Failed to unban account"}), 500


@app.route("/api/admin/backfill-normalized-tables", methods=["POST", "OPTIONS"])
def api_admin_backfill_normalized_tables():
    if request.method == "OPTIONS":
        return "", 200

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    if not _is_bio_moderation_admin(user_id):
        return jsonify({"error": "Forbidden"}), 403

    try:
        data = request.get_json() or {}
        limit_raw = data.get("limit", 0)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 0

        result = backfill_normalized_user_tables(limit=limit)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": "Backfill failed"}), 500

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
    except Exception as e:
        return jsonify({"error": "Failed to load public profile"}), 500

@app.route("/@<username>", methods=["GET"])
def public_profile_redirect(username: str):
    return redirect(f"/profile.html?u={username}", code=302)

@app.route("/api/logout", methods=["POST", "OPTIONS"])
def api_logout():
    if request.method == "OPTIONS":
        return "", 200
    session.pop("user_id", None)
    return jsonify({"success": True})

# ------------------ USER COUNT ---------------------------
@app.route("/api/user-count", methods=["GET"])
def api_user_count():
    try:
        total_users, _ = _count_auth_users()

        # Fallback for environments without admin auth permissions.
        if total_users is None:
            resp = get_admin_client().table(USER_PROGRESS_TABLE).select("user_id", count="exact").execute()
            counted = getattr(resp, "count", None)
            total_users = counted if isinstance(counted, int) else (len(resp.data) if resp.data else 0)

        return jsonify({"totalUsers": int(total_users)})
    except Exception as e:
        return jsonify({"error": "Failed to count users"}), 500

# ------------------- TRIAL MODE -------------------
@app.route("/api/trial/link", methods=["POST", "OPTIONS"])
def api_trial_link():
    """Link trial progress to newly created user account"""
    if request.method == "OPTIONS":
        return "", 200
    
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    
    try:
        data = request.get_json()
        
        # Validate required fields
        if not data:
            return jsonify({"error": "No trial data provided"}), 400
        
        session_id = data.get("sessionId")
        course_id = data.get("courseId")
        chapters = data.get("chapters", [])
        
        if not session_id or not course_id:
            return jsonify({"error": "Missing required trial data"}), 400
        
        # Get existing user progress
        existing_progress = get_user_progress(user_id)
        if "error" in existing_progress:
            existing_progress = {
                "progress": {},
                "xp": 0,
                "streak": 0,
                "last_active": None,
                "missions": {},
                "mistakes": []
            }
        
        # Initialize progress structure if needed
        if "progress" not in existing_progress:
            existing_progress["progress"] = {}
        
        # Add trial course to progress if it doesn't exist
        if course_id not in existing_progress["progress"]:
            existing_progress["progress"][course_id] = {
                "started": True,
                "chapters": {}
            }
        
        # Link trial chapters to user progress
        if "chapters" not in existing_progress["progress"][course_id]:
            existing_progress["progress"][course_id]["chapters"] = {}
        
        for chapter in chapters:
            chapter_id = chapter.get("chapterId")
            chapter_data = chapter.get("data", {})
            
            if chapter_id:
                # Merge chapter data, preserving any existing data
                if chapter_id not in existing_progress["progress"][course_id]["chapters"]:
                    existing_progress["progress"][course_id]["chapters"][chapter_id] = {}
                
                existing_progress["progress"][course_id]["chapters"][chapter_id].update(chapter_data)
        
        # Save updated progress
        result = save_user_progress(user_id, existing_progress)
        
        if "error" in result:
            return jsonify({"error": "Failed to save progress: " + result["error"]}), 500
        
        print("[TRIAL] Successfully linked trial progress")
        return jsonify({
            "success": True,
            "message": "Trial progress linked successfully",
            "chapters_linked": len(chapters)
        })
    
    except Exception as e:
        print("[TRIAL] Error linking trial progress")
        return jsonify({"error": "Failed to link trial progress"}), 500

# ------------------- ACCOUNT MANAGEMENT -------------------
@app.route("/api/change-password", methods=["POST", "OPTIONS"])
def api_change_password():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        data = request.get_json()
        new_password = data.get("new_password")
        
        if not new_password:
            return jsonify({"error": "Missing new password"}), 400
        
        if len(new_password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        
        result = change_password(user_id, None, new_password)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Password change failed"}), 500

@app.route("/api/delete-account", methods=["POST", "OPTIONS"])
def api_delete_account():
    if request.method == "OPTIONS":
        return "", 200
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401
    try:
        result = delete_account(user_id)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        
        # Clear session
        session.pop("user_id", None)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Account deletion failed"}), 500