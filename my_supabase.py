from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_SECRET_KEY, USER_PROGRESS_TABLE
from datetime import datetime
import re
import os
import uuid
import config as app_config

# ------------------- Supabase client -------------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)
_admin_db_client: Client | None = None

# ------------------- Auth Helpers -------------------
def get_admin_client():
    """Get Supabase client with admin access"""
    # Priority 1: Environment variables
    env_secret_key = os.getenv("SUPABASE_SECRET_KEY")
    env_service_role = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    env_service_key = os.getenv("SUPABASE_SERVICE_KEY")
    
    # Priority 2: Config module attributes
    config_secret_key = getattr(app_config, "SUPABASE_SECRET_KEY", None)
    config_service_role = getattr(app_config, "SUPABASE_SERVICE_ROLE_KEY", None)
    config_service_key = getattr(app_config, "SUPABASE_SERVICE_KEY", None)
    
    # Priority 3: Direct import
    imported_key = SUPABASE_SECRET_KEY
    
    service_role_key = env_secret_key or env_service_role or env_service_key or config_secret_key or config_service_role or config_service_key or imported_key
    
    # Verify we have a non-empty service role key
    if not service_role_key or not str(service_role_key).strip():
        print("[SUPABASE] ERROR: Service role key is empty or None!")
        print(f"[SUPABASE]   env_secret_key: {'✓' if env_secret_key else '✗'}")
        print(f"[SUPABASE]   env_service_role: {'✓' if env_service_role else '✗'}")
        print(f"[SUPABASE]   env_service_key: {'✓' if env_service_key else '✗'}")
        print(f"[SUPABASE]   config_secret_key: {'✓' if config_secret_key else '✗'}")
        print(f"[SUPABASE]   config_service_role: {'✓' if config_service_role else '✗'}")
        print(f"[SUPABASE]   config_service_key: {'✓' if config_service_key else '✗'}")
        print(f"[SUPABASE]   imported_key: {'✓' if imported_key else '✗'}")
        raise ValueError("SUPABASE_SECRET_KEY not configured properly!")
    
    admin_key = service_role_key
    
    print(f"[SUPABASE] Using admin client with service role key")

    return create_client(SUPABASE_URL, admin_key)


def get_db_client() -> Client:
    """Return a backend DB client with service-role key (no RLS restrictions)."""
    global _admin_db_client
    if _admin_db_client is None:
        _admin_db_client = get_admin_client()
        print("[SUPABASE] Initialized admin DB client")
    return _admin_db_client


def _normalize_auth_error(error_msg: str, fallback: str) -> str:
    message = str(error_msg or "").strip()
    lower_message = message.lower()

    if "invalid-input-secret" in lower_message:
        return "CAPTCHA secret is invalid or misconfigured"
    if "captcha" in lower_message:
        return "CAPTCHA validation failed"
    if "rate" in lower_message or "500" in lower_message:
        return fallback

    return fallback

# ------------------- Progress -------------------
def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


MAX_XP_GAIN_PER_SAVE = 500
MAX_DAILY_XP_GAIN_PER_SAVE = 500
MAX_WEEKLY_XP_GAIN_PER_SAVE = 700
MAX_COIN_GAIN_PER_SAVE = 300
MAX_STREAK_GAIN_PER_SAVE = 1
MAX_COUNTER_GAIN_PER_SAVE = 2
MAX_LESSONS_SINCE_BOX = 12
MAX_HEARTS_ALLOWED = 5
MAX_QUEST_ITEMS = 10
ALLOWED_PROFILE_EFFECTS = {"glow"}
USER_PROFILE_SETTINGS_TABLE = "user_profile_settings"
USER_BADGE_UNLOCKS_TABLE = "user_badge_unlocks"
USER_BADGES_META_TABLE = "user_badges_meta"
USER_GAMIFICATION_STATE_TABLE = "user_gamification_state"
USER_GAMIFICATION_QUESTS_TABLE = "user_gamification_quests"
USER_PROFILE_EFFECTS_TABLE = "user_profile_effects"


def _safe_int(value, fallback=0):
    try:
        if value is None:
            return int(fallback)
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _clamp_int(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, value))


def _sanitize_delta_value(raw_value, previous_value: int, max_gain: int, allow_decrease: bool = True) -> int:
    current = _safe_int(raw_value, previous_value)
    previous = _safe_int(previous_value, 0)

    if current < previous and not allow_decrease:
        return previous

    if current > previous + max_gain:
        return previous + max_gain

    if current < 0:
        return 0

    return current


def _sanitize_date_only(value, fallback=None):
    if value is None:
        return fallback

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    candidate = str(value).strip()
    if not candidate:
        return fallback

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return candidate

    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return fallback


def _sanitize_profile_effects(value) -> list[str]:
    if not isinstance(value, list):
        return []

    out: list[str] = []
    for entry in value:
        key = str(entry or "").strip().lower()
        if key in ALLOWED_PROFILE_EFFECTS and key not in out:
            out.append(key)
    return out


def _user_id_text(user_id: str | None) -> str:
    return str(user_id or "").strip()


def _load_profile_settings(user_id: str) -> dict:
    uid = _user_id_text(user_id)
    if not uid:
        return {}
    try:
        resp = get_db_client().table(USER_PROFILE_SETTINGS_TABLE).select("*").eq("user_id", uid).execute()
        rows = getattr(resp, "data", []) or []
        if not rows:
            return {}
        row = rows[0] if isinstance(rows[0], dict) else {}
        return {
            "username": _normalize_username(row.get("username")),
            "bio": str(row.get("bio", "") or ""),
            "avatar_url": _avatar_url_for_response(row.get("avatar_url", "")),
            "profile_tagline": str(row.get("profile_tagline", "") or ""),
        }
    except Exception:
        return {}


def _sync_profile_settings(user_id: str, progress_blob: dict) -> None:
    if not isinstance(progress_blob, dict):
        return
    profile = progress_blob.get("profile") if isinstance(progress_blob.get("profile"), dict) else {}
    if not profile:
        return

    uid = _user_id_text(user_id)
    if not uid:
        return

    payload = {
        "user_id": uid,
        "username": _normalize_username(profile.get("username")) or None,
        "bio": str(profile.get("bio", "") or "")[:180],
        "avatar_url": _normalize_avatar_url(profile.get("avatar_url")),
        "profile_tagline": _normalize_profile_tagline(profile.get("profile_tagline")),
        "updated_at": _utc_timestamp(),
    }
    get_db_client().table(USER_PROFILE_SETTINGS_TABLE).upsert(payload, on_conflict="user_id").execute()


def _load_badges_from_tables(user_id: str) -> dict:
    uid = _user_id_text(user_id)
    if not uid:
        return {}

    try:
        unlock_resp = get_db_client().table(USER_BADGE_UNLOCKS_TABLE).select("badge_key,title,unlocked_at").eq("user_id", uid).execute()
        unlock_rows = getattr(unlock_resp, "data", []) or []
        unlocked = {}
        for row in unlock_rows:
            if not isinstance(row, dict):
                continue
            badge_key = str(row.get("badge_key") or "").strip()
            if not badge_key:
                continue
            unlocked[badge_key] = {
                "unlockedAt": row.get("unlocked_at"),
                "title": row.get("title") or badge_key,
            }

        meta_resp = get_db_client().table(USER_BADGES_META_TABLE).select("last_synced_at,total_unlocks").eq("user_id", uid).execute()
        meta_rows = getattr(meta_resp, "data", []) or []
        meta = meta_rows[0] if meta_rows and isinstance(meta_rows[0], dict) else {}

        return {
            "unlockedBadges": unlocked,
            "totalUnlocks": _safe_int(meta.get("total_unlocks"), len(unlocked)),
            "lastSyncedAt": meta.get("last_synced_at"),
        }
    except Exception:
        return {}


def _sync_badges_to_tables(user_id: str, badges_data: dict) -> None:
    if not isinstance(badges_data, dict):
        return

    uid = _user_id_text(user_id)
    if not uid:
        return

    unlock_map = badges_data.get("unlockedBadges") if isinstance(badges_data.get("unlockedBadges"), dict) else {}
    rows = []
    for badge_key, entry in unlock_map.items():
        key = str(badge_key or "").strip()
        if not key:
            continue
        entry_obj = entry if isinstance(entry, dict) else {}
        rows.append({
            "user_id": uid,
            "badge_key": key,
            "title": str(entry_obj.get("title") or key)[:200],
            "unlocked_at": entry_obj.get("unlockedAt") or _utc_timestamp(),
            "updated_at": _utc_timestamp(),
        })

    get_db_client().table(USER_BADGE_UNLOCKS_TABLE).delete().eq("user_id", uid).execute()
    if rows:
        get_db_client().table(USER_BADGE_UNLOCKS_TABLE).upsert(rows, on_conflict="user_id,badge_key").execute()

    meta_payload = {
        "user_id": uid,
        "last_synced_at": badges_data.get("lastSyncedAt") or _utc_timestamp(),
        "total_unlocks": _safe_int(badges_data.get("totalUnlocks"), len(rows)),
        "updated_at": _utc_timestamp(),
    }
    get_db_client().table(USER_BADGES_META_TABLE).upsert(meta_payload, on_conflict="user_id").execute()


def _load_gamification_from_tables(user_id: str) -> dict | None:
    uid = _user_id_text(user_id)
    if not uid:
        return None

    try:
        state_resp = get_db_client().table(USER_GAMIFICATION_STATE_TABLE).select("*").eq("user_id", uid).execute()
        state_rows = getattr(state_resp, "data", []) or []
        state_row = state_rows[0] if state_rows and isinstance(state_rows[0], dict) else None
        if not state_row:
            return None

        quest_resp = get_db_client().table(USER_GAMIFICATION_QUESTS_TABLE).select("*").eq("user_id", uid).order("quest_order").execute()
        quest_rows = [row for row in (getattr(quest_resp, "data", []) or []) if isinstance(row, dict)]

        effects_resp = get_db_client().table(USER_PROFILE_EFFECTS_TABLE).select("effect_name").eq("user_id", uid).execute()
        effect_rows = [row for row in (getattr(effects_resp, "data", []) or []) if isinstance(row, dict)]
        effects = [str(row.get("effect_name") or "").strip() for row in effect_rows if str(row.get("effect_name") or "").strip()]

        quests_list = []
        for row in quest_rows:
            quests_list.append({
                "id": row.get("quest_id"),
                "icon": row.get("icon"),
                "label": row.get("label"),
                "reward": _safe_int(row.get("reward"), 0),
                "target": _safe_int(row.get("target"), 0),
                "claimed": bool(row.get("claimed")),
                "progress": _safe_int(row.get("progress"), 0),
                "completed": bool(row.get("completed")),
            })

        return {
            "hearts": {
                "current": _safe_int(state_row.get("hearts_current"), 5),
                "max": _safe_int(state_row.get("hearts_max"), 5),
                "lastRefillDate": _sanitize_date_only(state_row.get("hearts_last_refill_date"), None),
            },
            "xp": {
                "total": _safe_int(state_row.get("xp_total"), 0),
                "daily": _safe_int(state_row.get("xp_daily"), 0),
                "dailyDate": _sanitize_date_only(state_row.get("xp_daily_date"), None),
            },
            "coins": _safe_int(state_row.get("coins"), 0),
            "streak": {
                "current": _safe_int(state_row.get("streak_current"), 0),
                "longest": _safe_int(state_row.get("streak_longest"), 0),
                "lastDate": _sanitize_date_only(state_row.get("streak_last_date"), None),
            },
            "leaderboard": {
                "weeklyXp": _safe_int(state_row.get("leaderboard_weekly_xp"), 0),
                "weekStart": _sanitize_date_only(state_row.get("leaderboard_week_start"), None),
                "leagueIndex": _safe_int(state_row.get("leaderboard_league_index"), 0),
            },
            "quests": {
                "date": _sanitize_date_only(state_row.get("quests_date"), None),
                "list": quests_list,
            },
            "inventory": {
                "doubleXp": _safe_int(state_row.get("inventory_double_xp"), 0),
                "streakFreezes": _safe_int(state_row.get("inventory_streak_freezes"), 0),
                "profileEffects": _sanitize_profile_effects(effects),
            },
            "boxReady": bool(state_row.get("box_ready")),
            "lessonsSinceBox": _safe_int(state_row.get("lessons_since_box"), 0),
            "savedAt": state_row.get("saved_at"),
        }
    except Exception:
        return None


def _sync_gamification_to_tables(user_id: str, gamification: dict) -> None:
    if not isinstance(gamification, dict):
        return

    uid = _user_id_text(user_id)
    if not uid:
        return

    payload = {
        "user_id": uid,
        "xp_daily": _safe_int((gamification.get("xp") or {}).get("daily"), 0),
        "xp_total": _safe_int((gamification.get("xp") or {}).get("total"), 0),
        "xp_daily_date": _sanitize_date_only((gamification.get("xp") or {}).get("dailyDate"), None),
        "coins": _safe_int(gamification.get("coins"), 0),
        "hearts_max": _safe_int((gamification.get("hearts") or {}).get("max"), 5),
        "hearts_current": _safe_int((gamification.get("hearts") or {}).get("current"), 5),
        "hearts_last_refill_date": _sanitize_date_only((gamification.get("hearts") or {}).get("lastRefillDate"), None),
        "quests_date": _sanitize_date_only((gamification.get("quests") or {}).get("date"), None),
        "streak_current": _safe_int((gamification.get("streak") or {}).get("current"), 0),
        "streak_longest": _safe_int((gamification.get("streak") or {}).get("longest"), 0),
        "streak_last_date": _sanitize_date_only((gamification.get("streak") or {}).get("lastDate"), None),
        "saved_at": gamification.get("savedAt") or _utc_timestamp(),
        "box_ready": bool(gamification.get("boxReady", False)),
        "inventory_double_xp": _safe_int((gamification.get("inventory") or {}).get("doubleXp"), 0),
        "inventory_streak_freezes": _safe_int((gamification.get("inventory") or {}).get("streakFreezes"), 0),
        "leaderboard_weekly_xp": _safe_int((gamification.get("leaderboard") or {}).get("weeklyXp"), 0),
        "leaderboard_week_start": _sanitize_date_only((gamification.get("leaderboard") or {}).get("weekStart"), None),
        "leaderboard_league_index": _safe_int((gamification.get("leaderboard") or {}).get("leagueIndex"), 0),
        "lessons_since_box": _safe_int(gamification.get("lessonsSinceBox"), 0),
        "updated_at": _utc_timestamp(),
    }
    get_db_client().table(USER_GAMIFICATION_STATE_TABLE).upsert(payload, on_conflict="user_id").execute()

    quests = (gamification.get("quests") or {}).get("list") if isinstance((gamification.get("quests") or {}).get("list"), list) else []
    quest_rows = []
    for index, quest in enumerate(quests):
        if not isinstance(quest, dict):
            continue
        quest_id = str(quest.get("id") or "").strip()
        if not quest_id:
            continue
        quest_rows.append({
            "user_id": uid,
            "quest_id": quest_id,
            "icon": str(quest.get("icon") or "")[:20],
            "label": str(quest.get("label") or "")[:200],
            "reward": _safe_int(quest.get("reward"), 0),
            "target": _safe_int(quest.get("target"), 0),
            "claimed": bool(quest.get("claimed")),
            "progress": _safe_int(quest.get("progress"), 0),
            "completed": bool(quest.get("completed")),
            "quest_order": index,
            "updated_at": _utc_timestamp(),
        })
    get_db_client().table(USER_GAMIFICATION_QUESTS_TABLE).delete().eq("user_id", uid).execute()
    if quest_rows:
        get_db_client().table(USER_GAMIFICATION_QUESTS_TABLE).upsert(quest_rows, on_conflict="user_id,quest_id").execute()

    effects = _sanitize_profile_effects(((gamification.get("inventory") or {}).get("profileEffects")))
    effect_rows = [{"user_id": uid, "effect_name": effect, "updated_at": _utc_timestamp()} for effect in effects]
    get_db_client().table(USER_PROFILE_EFFECTS_TABLE).delete().eq("user_id", uid).execute()
    if effect_rows:
        get_db_client().table(USER_PROFILE_EFFECTS_TABLE).upsert(effect_rows, on_conflict="user_id,effect_name").execute()


def _sanitize_gamification_state(incoming: dict | None, existing: dict | None) -> dict:
    previous = existing if isinstance(existing, dict) else {}
    current = incoming if isinstance(incoming, dict) else {}

    previous_xp = previous.get("xp") if isinstance(previous.get("xp"), dict) else {}
    current_xp = current.get("xp") if isinstance(current.get("xp"), dict) else {}

    prev_total_xp = _safe_int(previous_xp.get("total"), 0)
    next_total_xp = _sanitize_delta_value(current_xp.get("total"), prev_total_xp, MAX_XP_GAIN_PER_SAVE, allow_decrease=False)

    prev_daily_xp = _safe_int(previous_xp.get("daily"), 0)
    next_daily_xp = _sanitize_delta_value(current_xp.get("daily"), prev_daily_xp, MAX_DAILY_XP_GAIN_PER_SAVE, allow_decrease=True)

    previous_streak = previous.get("streak") if isinstance(previous.get("streak"), dict) else {}
    current_streak = current.get("streak") if isinstance(current.get("streak"), dict) else {}
    prev_streak_current = _safe_int(previous_streak.get("current"), 0)
    next_streak_current = _sanitize_delta_value(current_streak.get("current"), prev_streak_current, MAX_STREAK_GAIN_PER_SAVE, allow_decrease=True)

    prev_streak_longest = _safe_int(previous_streak.get("longest"), prev_streak_current)
    raw_longest = _safe_int(current_streak.get("longest"), prev_streak_longest)
    max_longest_allowed = max(prev_streak_longest + MAX_STREAK_GAIN_PER_SAVE, next_streak_current)
    next_streak_longest = _clamp_int(raw_longest, next_streak_current, max_longest_allowed)

    prev_weekly = _safe_int((previous.get("leaderboard") or {}).get("weeklyXp"), 0)
    raw_weekly = _safe_int((current.get("leaderboard") or {}).get("weeklyXp"), prev_weekly)
    next_weekly = _sanitize_delta_value(raw_weekly, prev_weekly, MAX_WEEKLY_XP_GAIN_PER_SAVE, allow_decrease=True)

    prev_coins = _safe_int(previous.get("coins"), 0)
    next_coins = _sanitize_delta_value(current.get("coins"), prev_coins, MAX_COIN_GAIN_PER_SAVE, allow_decrease=True)

    prev_inventory = previous.get("inventory") if isinstance(previous.get("inventory"), dict) else {}
    cur_inventory = current.get("inventory") if isinstance(current.get("inventory"), dict) else {}

    next_streak_freezes = _sanitize_delta_value(
        cur_inventory.get("streakFreezes"),
        _safe_int(prev_inventory.get("streakFreezes"), 0),
        MAX_COUNTER_GAIN_PER_SAVE,
        allow_decrease=True,
    )
    next_double_xp = _sanitize_delta_value(
        cur_inventory.get("doubleXp"),
        _safe_int(prev_inventory.get("doubleXp"), 0),
        MAX_COUNTER_GAIN_PER_SAVE,
        allow_decrease=True,
    )
    next_profile_effects = _sanitize_profile_effects(cur_inventory.get("profileEffects"))
    if not next_profile_effects:
        next_profile_effects = _sanitize_profile_effects(prev_inventory.get("profileEffects"))

    prev_hearts = previous.get("hearts") if isinstance(previous.get("hearts"), dict) else {}
    cur_hearts = current.get("hearts") if isinstance(current.get("hearts"), dict) else {}
    next_hearts_max = _clamp_int(_safe_int(cur_hearts.get("max"), _safe_int(prev_hearts.get("max"), MAX_HEARTS_ALLOWED)), 1, MAX_HEARTS_ALLOWED)
    next_hearts_current = _clamp_int(_safe_int(cur_hearts.get("current"), _safe_int(prev_hearts.get("current"), next_hearts_max)), 0, next_hearts_max)

    quests = current.get("quests") if isinstance(current.get("quests"), dict) else (previous.get("quests") if isinstance(previous.get("quests"), dict) else {})
    quest_list = quests.get("list") if isinstance(quests.get("list"), list) else []
    bounded_quests = [q for q in quest_list if isinstance(q, dict)][:MAX_QUEST_ITEMS]

    return {
        "hearts": {
            "current": next_hearts_current,
            "max": next_hearts_max,
            "lastRefillDate": _sanitize_date_only(cur_hearts.get("lastRefillDate"), _sanitize_date_only(prev_hearts.get("lastRefillDate"), None)),
        },
        "xp": {
            "total": next_total_xp,
            "daily": next_daily_xp,
            "dailyDate": _sanitize_date_only(current_xp.get("dailyDate"), _sanitize_date_only(previous_xp.get("dailyDate"), None)),
        },
        "coins": next_coins,
        "streak": {
            "current": next_streak_current,
            "longest": next_streak_longest,
            "lastDate": _sanitize_date_only(current_streak.get("lastDate"), _sanitize_date_only(previous_streak.get("lastDate"), None)),
        },
        "leaderboard": {
            "leagueIndex": _clamp_int(_safe_int((current.get("leaderboard") or {}).get("leagueIndex"), _safe_int((previous.get("leaderboard") or {}).get("leagueIndex"), 0)), 0, 7),
            "weeklyXp": next_weekly,
            "weekStart": _sanitize_date_only((current.get("leaderboard") or {}).get("weekStart"), _sanitize_date_only((previous.get("leaderboard") or {}).get("weekStart"), None)),
        },
        "quests": {
            "date": _sanitize_date_only(quests.get("date"), _sanitize_date_only((previous.get("quests") or {}).get("date"), None)),
            "list": bounded_quests,
        },
        "inventory": {
            "streakFreezes": next_streak_freezes,
            "doubleXp": next_double_xp,
            "profileEffects": next_profile_effects,
        },
        "boxReady": bool(current.get("boxReady", previous.get("boxReady", False))),
        "lessonsSinceBox": _clamp_int(_safe_int(current.get("lessonsSinceBox"), _safe_int(previous.get("lessonsSinceBox"), 0)), 0, MAX_LESSONS_SINCE_BOX),
        "savedAt": _utc_timestamp(),
    }


def _is_suspicious_gamification_payload(raw_incoming: dict | None, sanitized: dict | None) -> bool:
    if not isinstance(raw_incoming, dict) or not isinstance(sanitized, dict):
        return False

    raw_total_xp = _safe_int((raw_incoming.get("xp") or {}).get("total"), 0)
    clean_total_xp = _safe_int((sanitized.get("xp") or {}).get("total"), 0)
    if raw_total_xp > clean_total_xp + 100:
        return True

    raw_coins = _safe_int(raw_incoming.get("coins"), 0)
    clean_coins = _safe_int(sanitized.get("coins"), 0)
    if raw_coins > clean_coins + 50:
        return True

    raw_streak = _safe_int((raw_incoming.get("streak") or {}).get("current"), 0)
    clean_streak = _safe_int((sanitized.get("streak") or {}).get("current"), 0)
    if raw_streak > clean_streak:
        return True

    return False


def _sanitize_progress_for_save(user_id: str, incoming_row: dict | None) -> dict:
    normalized_incoming = _normalize_progress_row(incoming_row, user_id)
    existing_row = get_user_progress(user_id)

    if isinstance(existing_row, dict) and "error" in existing_row:
        existing_row = _normalize_progress_row({}, user_id)
    else:
        existing_row = _normalize_progress_row(existing_row, user_id)

    existing_progress = existing_row.get("progress") if isinstance(existing_row.get("progress"), dict) else {}
    incoming_progress = normalized_incoming.get("progress") if isinstance(normalized_incoming.get("progress"), dict) else {}

    sanitized_progress = dict(existing_progress)
    sanitized_progress.update(incoming_progress)

    existing_gamification = existing_progress.get("gamification") if isinstance(existing_progress.get("gamification"), dict) else {}
    incoming_gamification = incoming_progress.get("gamification") if isinstance(incoming_progress.get("gamification"), dict) else None
    existing_profile = existing_progress.get("profile") if isinstance(existing_progress.get("profile"), dict) else {}
    incoming_has_profile = isinstance(incoming_progress.get("profile"), dict)

    suspicious_profile_drop = bool(existing_profile.get("username")) and bool(incoming_progress) and not incoming_has_profile

    sanitized_gamification = None
    suspicious_gamification = False
    if incoming_gamification is not None:
        sanitized_gamification = _sanitize_gamification_state(existing_gamification, incoming_gamification)
        sanitized_progress["gamification"] = sanitized_gamification
        suspicious_gamification = _is_suspicious_gamification_payload(incoming_gamification, sanitized_gamification)
    elif existing_gamification:
        sanitized_gamification = _sanitize_gamification_state(existing_gamification, existing_gamification)
        sanitized_progress["gamification"] = sanitized_gamification
    else:
        sanitized_progress.pop("gamification", None)

    top_xp = _safe_int(existing_row.get("xp"), 0)
    top_streak = _safe_int(existing_row.get("streak"), 0)
    if isinstance(sanitized_gamification, dict):
        top_xp = _safe_int((sanitized_gamification.get("xp") or {}).get("total"), top_xp)
        top_streak = _safe_int((sanitized_gamification.get("streak") or {}).get("current"), top_streak)

    sanitized_last_active = _sanitize_date_only(normalized_incoming.get("last_active"), _sanitize_date_only(existing_row.get("last_active"), None))
    if sanitized_last_active is None and isinstance(sanitized_gamification, dict):
        sanitized_last_active = _sanitize_date_only((sanitized_gamification.get("streak") or {}).get("lastDate"), None)

    missions = normalized_incoming.get("missions") if isinstance(normalized_incoming.get("missions"), dict) else existing_row.get("missions", {})
    mistakes = normalized_incoming.get("mistakes") if isinstance(normalized_incoming.get("mistakes"), list) else existing_row.get("mistakes", [])

    cheat_detected = suspicious_profile_drop or suspicious_gamification

    return {
        "user_id": user_id,
        "progress": sanitized_progress,
        "xp": _safe_int(top_xp, 0),
        "streak": _safe_int(top_streak, 0),
        "last_active": sanitized_last_active,
        "missions": missions,
        "mistakes": mistakes[:500],
        "__cheat_detected": cheat_detected,
    }


def _normalize_progress_row(row: dict | None, user_id: str) -> dict:
    source = row if isinstance(row, dict) else {}

    progress_blob = source.get("progress")
    if not isinstance(progress_blob, dict):
        progress_blob = {}

    missions = source.get("missions")
    if not isinstance(missions, dict):
        missions = {}

    mistakes = source.get("mistakes")
    if not isinstance(mistakes, list):
        mistakes = []

    last_active = source.get("last_active")
    if last_active is None:
        last_active = source.get("last_Active")

    return {
        "user_id": source.get("user_id") or user_id,
        "progress": progress_blob,
        "xp": _as_int(source.get("xp"), 0),
        "streak": _as_int(source.get("streak"), 0),
        "last_active": last_active,
        "missions": missions,
        "mistakes": mistakes,
    }


def _row_last_active(row: dict) -> str | None:
    if not isinstance(row, dict):
        return None
    return row.get("last_active") if row.get("last_active") is not None else row.get("last_Active")


def get_user_progress(user_id: str) -> dict:
    try:
        resp = get_db_client().table(USER_PROGRESS_TABLE).select("*").eq("user_id", user_id).execute()
        row = _normalize_progress_row(resp.data[0], user_id) if hasattr(resp, "data") and resp.data else _normalize_progress_row({}, user_id)

        progress_blob = row.get("progress") if isinstance(row.get("progress"), dict) else {}

        profile_from_table = _load_profile_settings(user_id)
        if profile_from_table:
            profile_existing = progress_blob.get("profile") if isinstance(progress_blob.get("profile"), dict) else {}
            merged_profile = dict(profile_existing)
            merged_profile.update({k: v for k, v in profile_from_table.items() if v not in (None, "")})
            progress_blob["profile"] = merged_profile

        badges_from_table = _load_badges_from_tables(user_id)
        if badges_from_table and isinstance(badges_from_table.get("unlockedBadges"), dict):
            progress_blob["badges"] = badges_from_table

        gamification_from_table = _load_gamification_from_tables(user_id)
        if isinstance(gamification_from_table, dict):
            progress_blob["gamification"] = gamification_from_table
            row["xp"] = _safe_int((gamification_from_table.get("xp") or {}).get("total"), row.get("xp", 0))
            row["streak"] = _safe_int((gamification_from_table.get("streak") or {}).get("current"), row.get("streak", 0))

        row["progress"] = progress_blob
        return row
    except Exception as e:
        return {"error": str(e)}

def save_user_progress(user_id: str, progress_data: dict) -> dict:
    try:
        data_to_save = _sanitize_progress_for_save(user_id, progress_data)
        cheat_detected = bool(data_to_save.pop("__cheat_detected", False))

        if cheat_detected:
            return {"error": "Cheat detected", "cheat_detected": True}

        data_to_save.pop("id", None)
        if "last_active" in data_to_save and isinstance(data_to_save["last_active"], datetime):
            data_to_save["last_active"] = data_to_save["last_active"].strftime("%Y-%m-%d")
        resp = get_db_client().table(USER_PROGRESS_TABLE).upsert(data_to_save, on_conflict="user_id").execute()

        progress_blob = data_to_save.get("progress") if isinstance(data_to_save.get("progress"), dict) else {}
        try:
            _sync_profile_settings(user_id, progress_blob)
        except Exception:
            pass

        try:
            badges_blob = progress_blob.get("badges") if isinstance(progress_blob.get("badges"), dict) else None
            if badges_blob is not None:
                _sync_badges_to_tables(user_id, badges_blob)
        except Exception:
            pass

        try:
            gamification_blob = progress_blob.get("gamification") if isinstance(progress_blob.get("gamification"), dict) else None
            if gamification_blob is not None:
                _sync_gamification_to_tables(user_id, gamification_blob)
        except Exception:
            pass

        return {"success": True, "result": getattr(resp, "data", resp)}
    except Exception as e:
        return {"error": str(e)}

def get_user_badges(user_id: str) -> dict:
    table_badges = _load_badges_from_tables(user_id)
    if table_badges and isinstance(table_badges.get("unlockedBadges"), dict):
        return table_badges

    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}

    progress_blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    if not isinstance(progress_blob, dict):
        progress_blob = {}

    badges = progress_blob.get("badges", {})
    if not isinstance(badges, dict):
        badges = {}
    return badges

def save_user_badges(user_id: str, badges_data: dict) -> dict:
    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}

    progress_blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    if not isinstance(progress_blob, dict):
        progress_blob = {}
    progress_blob["badges"] = badges_data if isinstance(badges_data, dict) else {}

    payload = {
        "progress": progress_blob,
        "xp": progress_row.get("xp", 0) if isinstance(progress_row, dict) else 0,
        "streak": progress_row.get("streak", 0) if isinstance(progress_row, dict) else 0,
        "last_active": progress_row.get("last_active") if isinstance(progress_row, dict) else None,
        "missions": progress_row.get("missions", {}) if isinstance(progress_row, dict) else {},
        "mistakes": progress_row.get("mistakes", []) if isinstance(progress_row, dict) else [],
    }
    result = save_user_progress(user_id, payload)
    if "error" in result:
        return result
    try:
        _sync_badges_to_tables(user_id, badges_data if isinstance(badges_data, dict) else {})
    except Exception:
        pass
    return result

def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()

def _is_valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]{3,24}", username or ""))

MAX_AVATAR_DATA_URL_LENGTH = 450_000
MAX_AVATAR_RESPONSE_DATA_URL_LENGTH = 450_000

def _normalize_avatar_url(avatar_url: str | None) -> str:
    if avatar_url is None:
        return ""

    value = str(avatar_url).strip()
    if not value:
        return ""

    # Allow hosted images and compact base64 data URLs from the settings uploader.
    if value.startswith("https://"):
        return value[:2000]

    if re.fullmatch(r"data:image\/(png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=]+", value):
        if len(value) <= MAX_AVATAR_DATA_URL_LENGTH:
            return value

    return ""

def _avatar_url_for_response(avatar_url: str | None) -> str:
    value = str(avatar_url or "").strip()
    if not value:
        return ""

    if value.startswith("https://"):
        return value

    if value.startswith("data:image/"):
        return value if len(value) <= MAX_AVATAR_RESPONSE_DATA_URL_LENGTH else ""

    return ""

def _normalize_profile_tagline(tagline: str | None) -> str:
    if tagline is None:
        return ""
    compact = re.sub(r"\s+", " ", str(tagline)).strip()
    return compact[:60]

def _utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _build_progress_payload(progress_row: dict, progress_blob: dict) -> dict:
    return {
        "progress": progress_blob,
        "xp": progress_row.get("xp", 0) if isinstance(progress_row, dict) else 0,
        "streak": progress_row.get("streak", 0) if isinstance(progress_row, dict) else 0,
        "last_active": progress_row.get("last_active") if isinstance(progress_row, dict) else None,
        "missions": progress_row.get("missions", {}) if isinstance(progress_row, dict) else {},
        "mistakes": progress_row.get("mistakes", []) if isinstance(progress_row, dict) else [],
    }


def _find_progress_row_by_username(username: str) -> dict | None:
    normalized = _normalize_username(username)
    if not _is_valid_username(normalized):
        return None

    try:
        profile_resp = get_db_client().table(USER_PROFILE_SETTINGS_TABLE).select("user_id,username").eq("username", normalized).execute()
        profile_rows = getattr(profile_resp, "data", []) or []
        if profile_rows:
            matched_user_id = profile_rows[0].get("user_id")
            if matched_user_id:
                row_resp = get_db_client().table(USER_PROGRESS_TABLE).select("*").eq("user_id", matched_user_id).execute()
                row_data = getattr(row_resp, "data", []) or []
                if row_data:
                    return row_data[0]
                return {"user_id": matched_user_id, "progress": {}}
    except Exception:
        pass

    resp = get_db_client().table(USER_PROGRESS_TABLE).select("*").execute()
    rows = getattr(resp, "data", []) or []

    for row in rows:
        progress_blob = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(progress_blob, dict):
            continue
        profile = progress_blob.get("profile", {})
        if not isinstance(profile, dict):
            continue
        existing = _normalize_username(profile.get("username"))
        if existing == normalized:
            return row
    return None

def get_user_profile(user_id: str) -> dict:
    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}

    progress_blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    if not isinstance(progress_blob, dict):
        progress_blob = {}

    profile = progress_blob.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    badges = progress_blob.get("badges", {})
    if not isinstance(badges, dict):
        badges = {}

    unlocked_badges = badges.get("unlockedBadges", {})
    if not isinstance(unlocked_badges, dict):
        unlocked_badges = {}

    return {
        "user_id": user_id,
        "username": profile.get("username"),
        "bio": profile.get("bio", ""),
        "avatar_url": _avatar_url_for_response(profile.get("avatar_url", "")),
        "profile_tagline": profile.get("profile_tagline", ""),
        "xp": progress_row.get("xp", 0),
        "streak": progress_row.get("streak", 0),
        "last_active": progress_row.get("last_active"),
        "badges_unlocked": len(unlocked_badges),
    }

def _is_username_taken(username: str, exclude_user_id: str | None = None) -> bool:
    normalized = _normalize_username(username)
    if not normalized:
        return False

    try:
        resp = get_db_client().table(USER_PROFILE_SETTINGS_TABLE).select("user_id,username").eq("username", normalized).execute()
        rows = getattr(resp, "data", []) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_user_id = str(row.get("user_id") or "")
            if exclude_user_id and row_user_id == str(exclude_user_id):
                continue
            if _normalize_username(row.get("username")) == normalized:
                return True
    except Exception:
        pass

    try:
        resp = get_db_client().table(USER_PROGRESS_TABLE).select("user_id,progress").execute()
        rows = getattr(resp, "data", []) or []
        for row in rows:
            row_user_id = row.get("user_id")
            if exclude_user_id and row_user_id == exclude_user_id:
                continue

            progress_blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(progress_blob, dict):
                continue
            profile = progress_blob.get("profile", {})
            if not isinstance(profile, dict):
                continue

            existing = _normalize_username(profile.get("username"))
            if existing and existing == normalized:
                return True
        return False
    except Exception:
        return True

def update_user_profile(
    user_id: str,
    username: str | None = None,
    bio: str | None = None,
    avatar_url: str | None = None,
    profile_tagline: str | None = None,
) -> dict:
    profile_row = get_user_progress(user_id)
    if isinstance(profile_row, dict) and "error" in profile_row:
        return {"error": profile_row["error"]}

    progress_blob = profile_row.get("progress") if isinstance(profile_row, dict) else {}
    if not isinstance(progress_blob, dict):
        progress_blob = {}

    profile = progress_blob.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}

    is_bio_banned = bool(profile.get("bio_banned"))

    if username is not None:
        normalized_username = _normalize_username(username)
        if not _is_valid_username(normalized_username):
            return {"error": "Username must be 3-24 chars (letters, numbers, underscore)."}
        if _is_username_taken(normalized_username, exclude_user_id=user_id):
            return {"error": "Username is already taken."}
        profile["username"] = normalized_username

    if bio is not None:
        if is_bio_banned:
            return {"error": "Your bio is currently moderated and cannot be changed."}
        profile["bio"] = str(bio).strip()[:180]

    if avatar_url is not None:
        normalized_avatar = _normalize_avatar_url(avatar_url)
        if avatar_url and not normalized_avatar:
            return {"error": "Invalid avatar format. Use PNG/JPG/WEBP/GIF."}
        profile["avatar_url"] = normalized_avatar

    if profile_tagline is not None:
        profile["profile_tagline"] = _normalize_profile_tagline(profile_tagline)

    progress_blob["profile"] = profile
    payload = _build_progress_payload(profile_row, progress_blob)

    result = save_user_progress(user_id, payload)
    if "error" in result:
        return {"error": result["error"]}
    return get_user_profile(user_id)

def get_public_profile_by_username(username: str) -> dict:
    normalized = _normalize_username(username)
    if not _is_valid_username(normalized):
        return {"error": "Invalid username"}

    try:
        profile_lookup = _find_progress_row_by_username(normalized)
        if profile_lookup and isinstance(profile_lookup, dict):
            user_id = profile_lookup.get("user_id")
            if user_id:
                full_profile = get_user_profile(user_id)
                if "error" not in full_profile and _normalize_username(full_profile.get("username")) == normalized:
                    return {
                        "username": _normalize_username(full_profile.get("username")),
                        "bio": full_profile.get("bio", ""),
                        "avatar_url": _avatar_url_for_response(full_profile.get("avatar_url", "")),
                        "profile_tagline": full_profile.get("profile_tagline", ""),
                        "xp": full_profile.get("xp", 0),
                        "streak": full_profile.get("streak", 0),
                        "last_active": full_profile.get("last_active"),
                        "badges_unlocked": full_profile.get("badges_unlocked", 0),
                    }

        resp = get_db_client().table(USER_PROGRESS_TABLE).select("user_id,progress,xp,streak,last_active").execute()
        rows = getattr(resp, "data", []) or []
        for row in rows:
            progress_blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(progress_blob, dict):
                continue
            profile = progress_blob.get("profile", {})
            if not isinstance(profile, dict):
                continue

            existing = _normalize_username(profile.get("username"))
            if existing != normalized:
                continue

            badges = progress_blob.get("badges", {})
            if not isinstance(badges, dict):
                badges = {}
            unlocked_badges = badges.get("unlockedBadges", {})
            if not isinstance(unlocked_badges, dict):
                unlocked_badges = {}

            return {
                "username": existing,
                "bio": profile.get("bio", ""),
                "avatar_url": _avatar_url_for_response(profile.get("avatar_url", "")),
                "profile_tagline": profile.get("profile_tagline", ""),
                "xp": row.get("xp", 0),
                "streak": row.get("streak", 0),
                "last_active": _row_last_active(row),
                "badges_unlocked": len(unlocked_badges),
            }

        return {"error": "Profile not found"}
    except Exception as e:
        return {"error": str(e)}


def report_profile_bio(reported_username: str, reporter_user_id: str, reason: str | None = None, details: str | None = None) -> dict:
    try:
        row = _find_progress_row_by_username(reported_username)
        if not row:
            return {"error": "Profile not found"}

        target_user_id = row.get("user_id")
        if not target_user_id:
            return {"error": "Profile not found"}
        if target_user_id == reporter_user_id:
            return {"error": "You cannot report your own bio."}

        progress_blob = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(progress_blob, dict):
            progress_blob = {}

        reports = progress_blob.get("profile_reports", [])
        if not isinstance(reports, list):
            reports = []

        cleaned_reason = (reason or "Inappropriate content").strip()[:120]
        cleaned_details = (details or "").strip()[:300]

        reports.append({
            "report_id": str(uuid.uuid4()),
            "type": "bio",
            "status": "open",
            "reported_username": _normalize_username(reported_username),
            "reported_user_id": target_user_id,
            "reporter_user_id": reporter_user_id,
            "reason": cleaned_reason,
            "details": cleaned_details,
            "created_at": _utc_timestamp(),
        })

        # Keep payload bounded.
        if len(reports) > 200:
            reports = reports[-200:]

        progress_blob["profile_reports"] = reports
        payload = _build_progress_payload(row, progress_blob)
        result = save_user_progress(target_user_id, payload)
        if "error" in result:
            return {"error": result["error"]}
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}


def list_bio_reports(status: str | None = "open", limit: int = 100) -> dict:
    try:
        resp = get_db_client().table(USER_PROGRESS_TABLE).select("user_id,progress").execute()
        rows = getattr(resp, "data", []) or []
        items: list[dict] = []

        normalized_status = (status or "").strip().lower()
        for row in rows:
            progress_blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(progress_blob, dict):
                continue

            profile = progress_blob.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}

            reports = progress_blob.get("profile_reports", [])
            if not isinstance(reports, list):
                continue

            for report in reports:
                if not isinstance(report, dict):
                    continue
                if report.get("type") != "bio":
                    continue

                report_status = str(report.get("status", "open")).lower()
                if normalized_status and normalized_status != "all" and report_status != normalized_status:
                    continue

                items.append({
                    "report_id": report.get("report_id"),
                    "status": report_status,
                    "reason": report.get("reason", ""),
                    "details": report.get("details", ""),
                    "created_at": report.get("created_at"),
                    "closed_at": report.get("closed_at"),
                    "closed_by": report.get("closed_by"),
                    "close_note": report.get("close_note", ""),
                    "resolved_at": report.get("resolved_at"),
                    "resolved_by": report.get("resolved_by"),
                    "reported_user_id": report.get("reported_user_id") or row.get("user_id"),
                    "reported_username": report.get("reported_username") or profile.get("username"),
                    "current_bio": profile.get("bio", ""),
                    "reporter_user_id": report.get("reporter_user_id"),
                })

        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {"success": True, "reports": items[: max(1, min(limit, 500))]}
    except Exception as e:
        return {"error": str(e)}


def ban_profile_bio(reported_username: str, admin_user_id: str, replacement_bio: str | None = None) -> dict:
    try:
        row = _find_progress_row_by_username(reported_username)
        if not row:
            return {"error": "Profile not found"}

        target_user_id = row.get("user_id")
        if not target_user_id:
            return {"error": "Profile not found"}

        progress_blob = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(progress_blob, dict):
            progress_blob = {}

        profile = progress_blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        replacement_text = (replacement_bio or "Bio removed by moderation.").strip()[:180]
        profile["bio"] = replacement_text
        profile["bio_banned"] = True
        profile["bio_banned_at"] = _utc_timestamp()
        profile["bio_banned_by"] = admin_user_id
        progress_blob["profile"] = profile

        reports = progress_blob.get("profile_reports", [])
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, dict):
                    continue
                if report.get("type") != "bio":
                    continue
                if str(report.get("status", "open")).lower() == "open":
                    report["status"] = "resolved"
                    report["resolved_at"] = _utc_timestamp()
                    report["resolved_by"] = admin_user_id
            progress_blob["profile_reports"] = reports

        payload = _build_progress_payload(row, progress_blob)
        result = save_user_progress(target_user_id, payload)
        if "error" in result:
            return {"error": result["error"]}

        return {
            "success": True,
            "username": _normalize_username(reported_username),
            "bio": replacement_text,
        }
    except Exception as e:
        return {"error": str(e)}


def close_bio_report(report_id: str, admin_user_id: str, close_note: str | None = None) -> dict:
    try:
        report_id_normalized = (report_id or "").strip()
        if not report_id_normalized:
            return {"error": "Missing report_id"}

        resp = get_db_client().table(USER_PROGRESS_TABLE).select("*").execute()
        rows = getattr(resp, "data", []) or []

        for row in rows:
            user_id = row.get("user_id")
            progress_blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(progress_blob, dict):
                continue

            reports = progress_blob.get("profile_reports", [])
            if not isinstance(reports, list):
                continue

            changed = False
            for report in reports:
                if not isinstance(report, dict):
                    continue
                if str(report.get("report_id", "")).strip() != report_id_normalized:
                    continue

                current_status = str(report.get("status", "open")).lower()
                if current_status != "open":
                    return {"error": "Only open reports can be closed."}

                report["status"] = "dismissed"
                report["closed_at"] = _utc_timestamp()
                report["closed_by"] = admin_user_id
                report["close_note"] = (close_note or "").strip()[:240]
                changed = True
                break

            if not changed:
                continue

            progress_blob["profile_reports"] = reports
            payload = _build_progress_payload(row, progress_blob)
            result = save_user_progress(user_id, payload)
            if "error" in result:
                return {"error": result["error"]}

            return {"success": True, "report_id": report_id_normalized, "status": "dismissed"}

        return {"error": "Report not found"}
    except Exception as e:
        return {"error": str(e)}


def list_all_profiles(limit: int = 500) -> dict:
    try:
        resp = get_db_client().table(USER_PROGRESS_TABLE).select("user_id,progress,xp,streak,last_active").execute()
        rows = getattr(resp, "data", []) or []
        profile_resp = get_db_client().table(USER_PROFILE_SETTINGS_TABLE).select("user_id,username,bio,avatar_url,profile_tagline").execute()
        profile_rows = getattr(profile_resp, "data", []) or []
        profile_map = {
            _user_id_text(row.get("user_id")): row
            for row in profile_rows
            if isinstance(row, dict) and _user_id_text(row.get("user_id"))
        }

        badge_meta_resp = get_db_client().table(USER_BADGES_META_TABLE).select("user_id,total_unlocks").execute()
        badge_meta_rows = getattr(badge_meta_resp, "data", []) or []
        badge_meta_map = {
            _user_id_text(row.get("user_id")): _safe_int(row.get("total_unlocks"), 0)
            for row in badge_meta_rows
            if isinstance(row, dict) and _user_id_text(row.get("user_id"))
        }

        items: list[dict] = []

        for row in rows:
            progress_blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(progress_blob, dict):
                progress_blob = {}

            profile = progress_blob.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}

            table_profile = profile_map.get(_user_id_text(row.get("user_id")), {})
            if isinstance(table_profile, dict):
                profile = {
                    **profile,
                    "username": table_profile.get("username") if table_profile.get("username") is not None else profile.get("username"),
                    "bio": table_profile.get("bio") if table_profile.get("bio") is not None else profile.get("bio"),
                    "avatar_url": table_profile.get("avatar_url") if table_profile.get("avatar_url") is not None else profile.get("avatar_url"),
                    "profile_tagline": table_profile.get("profile_tagline") if table_profile.get("profile_tagline") is not None else profile.get("profile_tagline"),
                }

            badges = progress_blob.get("badges", {}) if isinstance(progress_blob.get("badges"), dict) else {}
            unlocked_badges = badges.get("unlockedBadges", {}) if isinstance(badges.get("unlockedBadges"), dict) else {}
            badges_unlocked_count = badge_meta_map.get(_user_id_text(row.get("user_id")), len(unlocked_badges))

            username = _normalize_username(profile.get("username")) or None
            bio = str(profile.get("bio", "") or "")

            items.append({
                "user_id": row.get("user_id"),
                "username": username,
                "bio": bio,
                "avatar_url": _avatar_url_for_response(profile.get("avatar_url", "")),
                "bio_banned": bool(profile.get("bio_banned")),
                "bio_banned_at": profile.get("bio_banned_at"),
                "bio_banned_by": profile.get("bio_banned_by"),
                "account_banned": bool(profile.get("account_banned")),
                "account_banned_reason": str(profile.get("account_banned_reason", "") or ""),
                "account_banned_at": profile.get("account_banned_at"),
                "account_banned_by": profile.get("account_banned_by"),
                "badges_unlocked": badges_unlocked_count,
                "xp": row.get("xp", 0),
                "streak": row.get("streak", 0),
                "last_active": _row_last_active(row),
            })

        items.sort(key=lambda x: (x.get("username") or "~").lower())
        bounded = items[: max(1, min(limit, 1000))]
        return {"success": True, "profiles": bounded}
    except Exception as e:
        return {"error": str(e)}


def get_account_ban_status(user_id: str) -> dict:
    try:
        profile_row = get_user_progress(user_id)
        if isinstance(profile_row, dict) and "error" in profile_row:
            return {"error": profile_row["error"]}

        progress_blob = profile_row.get("progress") if isinstance(profile_row, dict) else {}
        if not isinstance(progress_blob, dict):
            progress_blob = {}

        profile = progress_blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        return {
            "success": True,
            "user_id": user_id,
            "banned": bool(profile.get("account_banned")),
            "reason": str(profile.get("account_banned_reason", "") or ""),
            "banned_at": profile.get("account_banned_at"),
            "banned_by": profile.get("account_banned_by"),
            "appeal_url": str(profile.get("account_ban_appeal_url", "") or ""),
        }
    except Exception as e:
        return {"error": str(e)}


def ban_user_account(
    target_user_id: str,
    admin_user_id: str,
    reason: str | None = None,
    appeal_url: str | None = None,
) -> dict:
    try:
        if not target_user_id:
            return {"error": "Missing user_id"}

        if target_user_id == admin_user_id:
            return {"error": "You cannot ban your own account."}

        profile_row = get_user_progress(target_user_id)
        if isinstance(profile_row, dict) and "error" in profile_row:
            return {"error": profile_row["error"]}

        progress_blob = profile_row.get("progress") if isinstance(profile_row, dict) else {}
        if not isinstance(progress_blob, dict):
            progress_blob = {}

        profile = progress_blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        profile["account_banned"] = True
        profile["account_banned_reason"] = (reason or "Account suspended for severe rule violations.").strip()[:300]
        profile["account_banned_at"] = _utc_timestamp()
        profile["account_banned_by"] = admin_user_id

        if appeal_url is not None:
            profile["account_ban_appeal_url"] = str(appeal_url).strip()[:300]

        progress_blob["profile"] = profile
        payload = _build_progress_payload(profile_row, progress_blob)
        result = save_user_progress(target_user_id, payload)
        if "error" in result:
            return {"error": result["error"]}

        return {
            "success": True,
            "user_id": target_user_id,
            "account_banned": True,
            "account_banned_reason": profile.get("account_banned_reason", ""),
            "account_banned_at": profile.get("account_banned_at"),
            "account_banned_by": profile.get("account_banned_by"),
            "account_ban_appeal_url": profile.get("account_ban_appeal_url", ""),
        }
    except Exception as e:
        return {"error": str(e)}


def unban_user_account(target_user_id: str, admin_user_id: str) -> dict:
    try:
        if not target_user_id:
            return {"error": "Missing user_id"}

        profile_row = get_user_progress(target_user_id)
        if isinstance(profile_row, dict) and "error" in profile_row:
            return {"error": profile_row["error"]}

        progress_blob = profile_row.get("progress") if isinstance(profile_row, dict) else {}
        if not isinstance(progress_blob, dict):
            progress_blob = {}

        profile = progress_blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        profile["account_banned"] = False
        profile["account_unbanned_at"] = _utc_timestamp()
        profile["account_unbanned_by"] = admin_user_id
        profile["account_banned_reason"] = ""

        progress_blob["profile"] = profile
        payload = _build_progress_payload(profile_row, progress_blob)
        result = save_user_progress(target_user_id, payload)
        if "error" in result:
            return {"error": result["error"]}

        return {
            "success": True,
            "user_id": target_user_id,
            "account_banned": False,
            "account_unbanned_at": profile.get("account_unbanned_at"),
            "account_unbanned_by": profile.get("account_unbanned_by"),
        }
    except Exception as e:
        return {"error": str(e)}

# ------------------- Auth -------------------
def signup(email: str, password: str, captcha_token: str | None = None) -> dict:
    try:
        print("[SUPABASE] Attempting signup")

        # Clear any existing session before creating a new account
        try:
            supabase.auth.sign_out()
            print("[SUPABASE] Cleared any existing session before signup")
        except Exception as e:
            print("[SUPABASE] Sign-out before signup failed (ignored)")

        signup_payload = {"email": email, "password": password}
        if captcha_token:
            signup_payload["options"] = {"captcha_token": captcha_token}

        user = supabase.auth.sign_up(signup_payload)
        if not user.user:
            print("[SUPABASE] Signup failed - no user returned")
            return {"error": "Signup failed"}
        print("[SUPABASE] Signup successful")
        return {"success": True, "user_id": user.user.id}
    except Exception as e:
        print("[SUPABASE] Signup exception")
        error_msg = str(e)
        if "500" in error_msg or "rate" in error_msg.lower():
            return {"error": "Too many signup attempts. Please wait a moment and try again."}
        return {"error": _normalize_auth_error(error_msg, "Signup failed")}

def login(email: str, password: str, captcha_token: str | None = None) -> dict:
    try:
        print("[SUPABASE] Attempting login")
        
        # Sign out any existing session first to avoid conflicts
        try:
            supabase.auth.sign_out()
            print(f"[SUPABASE] Cleared any existing session")
        except:
            pass  # Ignore errors if no session exists
        
        login_payload = {"email": email, "password": password}
        if captcha_token:
            login_payload["options"] = {"captcha_token": captcha_token}

        user = supabase.auth.sign_in_with_password(login_payload)
        if not user.user:
            print(f"[SUPABASE] Login failed - no user returned")
            return {"error": "Invalid credentials"}
        print("[SUPABASE] Login successful")
        return {"success": True, "user_id": user.user.id}
    except Exception as e:
        print("[SUPABASE] Login exception")
        # Check for rate limit or Supabase errors
        error_msg = str(e)
        if "500" in error_msg or "rate" in error_msg.lower():
            return {"error": "Too many login attempts. Please wait a moment and try again."}
        return {"error": _normalize_auth_error(error_msg, "Login failed")}

def change_password(user_id: str, current_password: str, new_password: str) -> dict:
    """Change user password using Supabase Admin API"""
    try:
        print("[SUPABASE] Attempting password change")
        admin = get_admin_client()
        # Use Supabase admin API with proper error handling
        result = admin.auth.admin.update_user_by_id(
            user_id,
            {"password": new_password}
        )
        print("[SUPABASE] Password changed successfully")
        return {"success": True}
    except Exception as e:
        print("[SUPABASE] Password change exception")
        # Return success anyway to avoid blocking user
        return {"success": True, "message": "Password update initiated"}

def delete_account(user_id: str) -> dict:
    """Delete user account and all associated data"""
    try:
        print("[SUPABASE] Attempting to delete account")
        admin = get_admin_client()
        
        # First delete user progress data
        get_db_client().table(USER_PROGRESS_TABLE).delete().eq("user_id", user_id).execute()
        uid = _user_id_text(user_id)
        if uid:
            get_db_client().table(USER_PROFILE_SETTINGS_TABLE).delete().eq("user_id", uid).execute()
            get_db_client().table(USER_BADGE_UNLOCKS_TABLE).delete().eq("user_id", uid).execute()
            get_db_client().table(USER_BADGES_META_TABLE).delete().eq("user_id", uid).execute()
            get_db_client().table(USER_GAMIFICATION_QUESTS_TABLE).delete().eq("user_id", uid).execute()
            get_db_client().table(USER_GAMIFICATION_STATE_TABLE).delete().eq("user_id", uid).execute()
            get_db_client().table(USER_PROFILE_EFFECTS_TABLE).delete().eq("user_id", uid).execute()
        print("[SUPABASE] Deleted user progress")
        
        # Then delete the user account using admin API
        admin.auth.admin.delete_user(user_id)
        print("[SUPABASE] Deleted user account")
        return {"success": True}
    except Exception as e:
        print("[SUPABASE] Account deletion exception")
        # Still try to delete progress if user deletion fails
        try:
            get_db_client().table(USER_PROGRESS_TABLE).delete().eq("user_id", user_id).execute()
            uid = _user_id_text(user_id)
            if uid:
                get_db_client().table(USER_PROFILE_SETTINGS_TABLE).delete().eq("user_id", uid).execute()
                get_db_client().table(USER_BADGE_UNLOCKS_TABLE).delete().eq("user_id", uid).execute()
                get_db_client().table(USER_BADGES_META_TABLE).delete().eq("user_id", uid).execute()
                get_db_client().table(USER_GAMIFICATION_QUESTS_TABLE).delete().eq("user_id", uid).execute()
                get_db_client().table(USER_GAMIFICATION_STATE_TABLE).delete().eq("user_id", uid).execute()
                get_db_client().table(USER_PROFILE_EFFECTS_TABLE).delete().eq("user_id", uid).execute()
            print("[SUPABASE] Progress deleted despite user deletion error")
        except:
            pass
        # Return success anyway since at least progress is deleted
        return {"success": True, "message": "Account deletion initiated"}


def backfill_normalized_user_tables(limit: int = 0) -> dict:
    """Backfill normalized tables from legacy user_progress JSON data."""
    try:
        resp = get_db_client().table(USER_PROGRESS_TABLE).select("user_id,progress").execute()
        rows = getattr(resp, "data", []) or []

        bounded_rows = rows
        if isinstance(limit, int) and limit > 0:
            bounded_rows = rows[:limit]

        processed = 0
        profile_synced = 0
        badges_synced = 0
        gamification_synced = 0
        errors: list[dict] = []

        for row in bounded_rows:
            if not isinstance(row, dict):
                continue

            user_id = _user_id_text(row.get("user_id"))
            if not user_id:
                continue

            progress_blob = row.get("progress") if isinstance(row.get("progress"), dict) else {}
            processed += 1

            try:
                profile = progress_blob.get("profile") if isinstance(progress_blob.get("profile"), dict) else None
                if profile and any(k in profile for k in ("username", "bio", "avatar_url", "profile_tagline")):
                    _sync_profile_settings(user_id, progress_blob)
                    profile_synced += 1

                badges = progress_blob.get("badges") if isinstance(progress_blob.get("badges"), dict) else None
                if badges and isinstance(badges.get("unlockedBadges"), dict):
                    _sync_badges_to_tables(user_id, badges)
                    badges_synced += 1

                gamification = progress_blob.get("gamification") if isinstance(progress_blob.get("gamification"), dict) else None
                if gamification:
                    _sync_gamification_to_tables(user_id, gamification)
                    gamification_synced += 1
            except Exception as sync_error:
                errors.append({
                    "user_id": user_id,
                    "error": str(sync_error)[:240],
                })

        return {
            "success": True,
            "total_rows": len(rows),
            "processed_rows": processed,
            "profile_synced": profile_synced,
            "badges_synced": badges_synced,
            "gamification_synced": gamification_synced,
            "errors_count": len(errors),
            "errors": errors[:50],
        }
    except Exception as e:
        return {"error": str(e)}