from flask import Flask, request, jsonify, session, redirect
from flask_cors import CORS
from my_supabase import get_user_progress, save_user_progress, signup, login, supabase, USER_PROGRESS_TABLE
from dotenv import load_dotenv
import requests
import os
import time
from urllib.parse import urlparse

load_dotenv()

SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
)

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
        return jsonify({"error": str(e)}), 500

# ------------------- CORS -------------------
CORS(app, resources={r"/api/*": {"origins": [
    "https://itlearn.be",
    "https://it-learn.pages.dev"
]}}, supports_credentials=True)

# ------------------- AI access control -------------------
AI_ALLOWED_PATH_PREFIXES = [
    p.strip() for p in os.getenv("AI_ALLOWED_PATH_PREFIXES", "/").split(",") if p.strip()
]
# Also allow only these OpenAI-compatible system prompt keys.
# Add/modify keys as needed.
AI_SYSTEM_PROMPTS = {
    # Example:
    # "default": "You are a helpful IT learning assistant.",
    "projects-assist":"You are a helpful assistent that helps students out with their questions on the projects they are making. The student can ask you any questions, as long as they are related to the project, and the project's context you got. You never write a whole block of code, you help the student and tell them to use their own brain. It is important to always explain very easy, and support the student. The student is learning, so you should not give them the answer, but help them to find the answer themselves. You can ask the student questions to help them find the answer. Always explain very easy, and support the student. Never go off the topic of the projects, don't answer unrelated questions."
}

# Basic limits (tune via env if you want)
AI_MAX_PROMPT_CHARS = int(os.getenv("AI_MAX_PROMPT_CHARS", "40000"))
AI_MAX_MESSAGES = int(os.getenv("AI_MAX_MESSAGES", "50"))
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "40"))

# Very lightweight in-memory rate limiting (per-process)
# { "<user_or_ip>": [timestamps...] }
AI_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("AI_RATE_LIMIT_WINDOW_SECONDS", "60"))
AI_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("AI_RATE_LIMIT_MAX_REQUESTS", "20"))
_ai_rate = {}

def _get_user_or_ip():
    user_id = session.get("user_id")
    if user_id:
        return f"user:{user_id}"
    # Flask request.remote_addr is best effort (may be proxied)
    return f"ip:{request.headers.get('X-Forwarded-For', request.remote_addr)}"

def _rate_limit_check():
    key = _get_user_or_ip()
    now = time.time()
    bucket = _ai_rate.get(key, [])
    bucket = [t for t in bucket if now - t <= AI_RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= AI_RATE_LIMIT_MAX_REQUESTS:
        return False
    bucket.append(now)
    _ai_rate[key] = bucket
    return True

def _is_request_from_allowed_page():
    # We restrict by Referer path. This is not foolproof (clients can forge headers),
    # but it meaningfully reduces casual misuse and works with your requirement.
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")

    # Origin must be one of the allowed origins (CORS already restricts, but we double-check)
    if origin not in {"https://itlearn.be", "https://it-learn.pages.dev"}:
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
    # Supported shapes:
    # 1) { "messages": [ {role, content}, ... ] }
    # 2) { "content": "..."}  -> converts to user message
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
        role = m.get("role")
        content = m.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("Invalid message role")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Each message 'content' must be a non-empty string")
        normalized.append({"role": role, "content": content})
    return normalized



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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

# ------------------- AUTH -------------------
@app.route("/api/signup", methods=["POST", "OPTIONS"])
def api_signup():
    if request.method == "OPTIONS":
        return "", 200
    try:
        data = request.get_json() or {}
        email = data.get("email")
        password = data.get("password")
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
        return jsonify({"error": str(e)}), 500

@app.route("/api/login", methods=["POST", "OPTIONS"])
def api_login():
    if request.method == "OPTIONS":
        return "", 200
    try:
        data = request.get_json() or {}
        email = data.get("email")
        password = data.get("password")
        if not email or not password:
            return jsonify({"error": "Missing email or password"}), 400

        if not captcha_token:
            return jsonify({"error": "CAPTCHA token missing"}), 400

        # Attempt login
        result = login(email, password, captcha_token=captcha_token)
        print(f"[LOGIN] Result: {result}")
        
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
        print(f"[LOGIN] Exception: {str(e)}")
        return jsonify({"error": str(e)}), 500


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
        print(f"[OAUTH][{normalized}] Start error: {e}")
        return jsonify({"error": str(e)}), 500


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
                print(f"[OAUTH][{normalized}] Code exchange failed: {exc}")
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
        print(f"[OAUTH][{normalized}] Complete error: {e}")
        return jsonify({"error": str(e)}), 500


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
        print(f"[DISCORD JOIN] Token exchange - redirect_uri: {DISCORD_JOIN_REDIRECT}")
        print(f"[DISCORD JOIN] Code (first 20 chars): {code[:20]}...")
        
        token_res = requests.post(
            DISCORD_OAUTH_TOKEN,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        
        if not token_res.ok:
            error_detail = token_res.text
            print(f"[DISCORD JOIN] Token exchange failed: {token_res.status_code} - {error_detail}")
        
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
        import traceback
        error_msg = f"{type(exc).__name__}: {str(exc)}"
        traceback.print_exc()
        print(f"[DISCORD JOIN] Exception: {error_msg}")
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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        
        print(f"[TRIAL] Successfully linked trial progress for user {user_id}, course {course_id}")
        return jsonify({
            "success": True,
            "message": "Trial progress linked successfully",
            "chapters_linked": len(chapters)
        })
    
    except Exception as e:
        print(f"[TRIAL] Error linking trial progress: {str(e)}")
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)})

# ------------------ AI PROXY ---------------------------
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
    system_prompt = AI_SYSTEM_PROMPTS.get(system_prompt_key)
    if not system_prompt:
        return jsonify({"error": "Unknown system prompt key"}), 400

    try:
        payload = request.get_json(force=True) or {}
        messages = _extract_messages(payload)

        # Hard stop on size
        total_chars = sum(len(m.get("content", "")) for m in messages)
        if total_chars > AI_MAX_PROMPT_CHARS:
            return jsonify({"error": f"Request too large (max {AI_MAX_PROMPT_CHARS} chars)"}), 413

        model = payload.get("model", "google/gemini-3.1-flash-lite")
        temperature = payload.get("temperature", 0.2)

        # Build OpenAI messages: system prompt first, then client messages
        openai_messages = [{"role": "system", "content": system_prompt}] + messages

        openai_payload = {
            "model": model,
            "messages": openai_messages,
            "temperature": temperature,
        }

        # If frontend wants streaming later, we can add it.
        # For now: non-streaming response.
        resp = requests.post(
            "https://ai.hackclub.com/proxy/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=openai_payload,
            timeout=AI_TIMEOUT_SECONDS,
        )

        if resp.status_code >= 400:
            # Don’t leak upstream body too much; still return useful error
            try:
                data = resp.json()
            except Exception:
                data = None

            if isinstance(data, dict) and data:
                return jsonify(
                    {"error": data.get("error", data), "status": resp.status_code}
                ), resp.status_code

            # Fallback: include truncated text
            text = (resp.text or "").strip()
            if len(text) > 2000:
                text = text[:2000] + "...(truncated)"
            return jsonify({"error": "OpenAI request failed", "status": resp.status_code, "body": text}), resp.status_code

        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "content": content, "model": model})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
