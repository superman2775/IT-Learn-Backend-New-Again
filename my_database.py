                                          
                                                            
                                                                 
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
import psycopg2
import psycopg2.extras
import psycopg2.pool
from config import DATABASE_URL
                                                                             
                 
                                                                             
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool
@contextmanager
def _db():
    """Yield a connection from the pool, commit on success, rollback on error."""
    conn = _get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)
def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
def _row(conn, sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None
def _exec(conn, sql: str, params: tuple = ()) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
                                                                             
                                          
                                                                             
USER_PROGRESS_TABLE          = "user_progress"
USER_PROFILE_SETTINGS_TABLE  = "user_profile_settings"
USER_BADGE_UNLOCKS_TABLE     = "user_badge_unlocks"
USER_BADGES_META_TABLE       = "user_badges_meta"
USER_GAMIFICATION_STATE_TABLE  = "user_gamification_state"
USER_GAMIFICATION_QUESTS_TABLE = "user_gamification_quests"
USER_PROFILE_EFFECTS_TABLE   = "user_profile_effects"
                                                                             
                                           
                                                                             
MAX_XP_GAIN_PER_SAVE        = 500
MAX_DAILY_XP_GAIN_PER_SAVE  = 500
MAX_WEEKLY_XP_GAIN_PER_SAVE = 700
MAX_COIN_GAIN_PER_SAVE      = 300
MAX_STREAK_GAIN_PER_SAVE    = 1
MAX_COUNTER_GAIN_PER_SAVE   = 2
MAX_LESSONS_SINCE_BOX       = 12
MAX_HEARTS_ALLOWED          = 5
MAX_QUEST_ITEMS             = 10
ALLOWED_PROFILE_EFFECTS     = {"glow"}
                                                                             
                                                   
                                                                             
def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
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
    current  = _safe_int(raw_value, previous_value)
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
def _normalize_username(username: str) -> str:
    return (username or "").strip().lower()
def _is_valid_username(username: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]{3,24}", username or ""))
MAX_AVATAR_DATA_URL_LENGTH = 450_000
def _normalize_avatar_url(avatar_url: str | None) -> str:
    if avatar_url is None:
        return ""
    value = str(avatar_url).strip()
    if not value:
        return ""
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
        return value if len(value) <= MAX_AVATAR_DATA_URL_LENGTH else ""
    return ""
def _normalize_profile_tagline(tagline: str | None) -> str:
    if tagline is None:
        return ""
    compact = re.sub(r"\s+", " ", str(tagline)).strip()
    return compact[:60]
def _utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
def _row_last_active(row: dict) -> str | None:
    if not isinstance(row, dict):
        return None
    return row.get("last_active") if row.get("last_active") is not None else row.get("last_Active")
def _build_progress_payload(progress_row: dict, progress_blob: dict) -> dict:
    return {
        "progress":    progress_blob,
        "xp":          progress_row.get("xp", 0)        if isinstance(progress_row, dict) else 0,
        "streak":      progress_row.get("streak", 0)    if isinstance(progress_row, dict) else 0,
        "last_active": progress_row.get("last_active")  if isinstance(progress_row, dict) else None,
        "missions":    progress_row.get("missions", {}) if isinstance(progress_row, dict) else {},
        "mistakes":    progress_row.get("mistakes", []) if isinstance(progress_row, dict) else [],
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
        "user_id":     source.get("user_id") or user_id,
        "progress":    progress_blob,
        "xp":          _as_int(source.get("xp"), 0),
        "streak":      _as_int(source.get("streak"), 0),
        "last_active": last_active,
        "missions":    missions,
        "mistakes":    mistakes,
    }
                                                                             
                                                                    
                                                                             
def _sanitize_gamification_state(incoming: dict | None, existing: dict | None) -> dict:
    previous = existing if isinstance(existing, dict) else {}
    current  = incoming if isinstance(incoming, dict) else {}
    previous_xp  = previous.get("xp")  if isinstance(previous.get("xp"),  dict) else {}
    current_xp   = current.get("xp")   if isinstance(current.get("xp"),   dict) else {}
    prev_total_xp = _safe_int(previous_xp.get("total"), 0)
    next_total_xp = _sanitize_delta_value(current_xp.get("total"), prev_total_xp, MAX_XP_GAIN_PER_SAVE, allow_decrease=False)
    prev_daily_xp = _safe_int(previous_xp.get("daily"), 0)
    next_daily_xp = _sanitize_delta_value(current_xp.get("daily"), prev_daily_xp, MAX_DAILY_XP_GAIN_PER_SAVE, allow_decrease=True)
    previous_streak = previous.get("streak") if isinstance(previous.get("streak"), dict) else {}
    current_streak  = current.get("streak")  if isinstance(current.get("streak"),  dict) else {}
    prev_streak_current = _safe_int(previous_streak.get("current"), 0)
    next_streak_current = _sanitize_delta_value(current_streak.get("current"), prev_streak_current, MAX_STREAK_GAIN_PER_SAVE, allow_decrease=True)
    prev_streak_longest = _safe_int(previous_streak.get("longest"), prev_streak_current)
    raw_longest         = _safe_int(current_streak.get("longest"), prev_streak_longest)
    max_longest_allowed = max(prev_streak_longest + MAX_STREAK_GAIN_PER_SAVE, next_streak_current)
    next_streak_longest = _clamp_int(raw_longest, next_streak_current, max_longest_allowed)
    prev_weekly = _safe_int((previous.get("leaderboard") or {}).get("weeklyXp"), 0)
    raw_weekly  = _safe_int((current.get("leaderboard")  or {}).get("weeklyXp"), prev_weekly)
    next_weekly = _sanitize_delta_value(raw_weekly, prev_weekly, MAX_WEEKLY_XP_GAIN_PER_SAVE, allow_decrease=True)
    prev_coins = _safe_int(previous.get("coins"), 0)
    next_coins = _sanitize_delta_value(current.get("coins"), prev_coins, MAX_COIN_GAIN_PER_SAVE, allow_decrease=True)
    prev_inventory = previous.get("inventory") if isinstance(previous.get("inventory"), dict) else {}
    cur_inventory  = current.get("inventory")  if isinstance(current.get("inventory"),  dict) else {}
    next_streak_freezes = _sanitize_delta_value(cur_inventory.get("streakFreezes"), _safe_int(prev_inventory.get("streakFreezes"), 0), MAX_COUNTER_GAIN_PER_SAVE, allow_decrease=True)
    next_double_xp      = _sanitize_delta_value(cur_inventory.get("doubleXp"),      _safe_int(prev_inventory.get("doubleXp"), 0),      MAX_COUNTER_GAIN_PER_SAVE, allow_decrease=True)
    next_profile_effects = _sanitize_profile_effects(cur_inventory.get("profileEffects"))
    if not next_profile_effects:
        next_profile_effects = _sanitize_profile_effects(prev_inventory.get("profileEffects"))
    prev_hearts = previous.get("hearts") if isinstance(previous.get("hearts"), dict) else {}
    cur_hearts  = current.get("hearts")  if isinstance(current.get("hearts"),  dict) else {}
    next_hearts_max     = _clamp_int(_safe_int(cur_hearts.get("max"),     _safe_int(prev_hearts.get("max"),     MAX_HEARTS_ALLOWED)), 1, MAX_HEARTS_ALLOWED)
    next_hearts_current = _clamp_int(_safe_int(cur_hearts.get("current"), _safe_int(prev_hearts.get("current"), next_hearts_max)),    0, next_hearts_max)
    quests     = current.get("quests")  if isinstance(current.get("quests"),  dict) else (previous.get("quests") if isinstance(previous.get("quests"), dict) else {})
    quest_list = quests.get("list") if isinstance(quests.get("list"), list) else []
    bounded_quests = [q for q in quest_list if isinstance(q, dict)][:MAX_QUEST_ITEMS]
    return {
        "hearts": {"current": next_hearts_current, "max": next_hearts_max,
                   "lastRefillDate": _sanitize_date_only(cur_hearts.get("lastRefillDate"), _sanitize_date_only(prev_hearts.get("lastRefillDate"), None))},
        "xp": {"total": next_total_xp, "daily": next_daily_xp,
               "dailyDate": _sanitize_date_only(current_xp.get("dailyDate"), _sanitize_date_only(previous_xp.get("dailyDate"), None))},
        "coins": next_coins,
        "streak": {"current": next_streak_current, "longest": next_streak_longest,
                   "lastDate": _sanitize_date_only(current_streak.get("lastDate"), _sanitize_date_only(previous_streak.get("lastDate"), None))},
        "leaderboard": {
            "leagueIndex": _clamp_int(_safe_int((current.get("leaderboard") or {}).get("leagueIndex"), _safe_int((previous.get("leaderboard") or {}).get("leagueIndex"), 0)), 0, 7),
            "weeklyXp": next_weekly,
            "weekStart": _sanitize_date_only((current.get("leaderboard") or {}).get("weekStart"), _sanitize_date_only((previous.get("leaderboard") or {}).get("weekStart"), None)),
        },
        "quests": {
            "date": _sanitize_date_only(quests.get("date"), _sanitize_date_only((previous.get("quests") or {}).get("date"), None)),
            "list": bounded_quests,
        },
        "inventory": {"streakFreezes": next_streak_freezes, "doubleXp": next_double_xp, "profileEffects": next_profile_effects},
        "boxReady": bool(current.get("boxReady", previous.get("boxReady", False))),
        "lessonsSinceBox": _clamp_int(_safe_int(current.get("lessonsSinceBox"), _safe_int(previous.get("lessonsSinceBox"), 0)), 0, MAX_LESSONS_SINCE_BOX),
        "savedAt": _utc_timestamp(),
    }
def _is_suspicious_gamification_payload(raw_incoming: dict | None, sanitized: dict | None) -> bool:
    if not isinstance(raw_incoming, dict) or not isinstance(sanitized, dict):
        return False
    if _safe_int((raw_incoming.get("xp") or {}).get("total"), 0) > _safe_int((sanitized.get("xp") or {}).get("total"), 0) + 100:
        return True
    if _safe_int(raw_incoming.get("coins"), 0) > _safe_int(sanitized.get("coins"), 0) + 50:
        return True
    if _safe_int((raw_incoming.get("streak") or {}).get("current"), 0) > _safe_int((sanitized.get("streak") or {}).get("current"), 0):
        return True
    return False
                                                                             
                               
                                                                             
def _load_profile_settings(conn, user_id: str) -> dict:
    uid = _user_id_text(user_id)
    if not uid:
        return {}
    row = _row(conn, f"SELECT * FROM {USER_PROFILE_SETTINGS_TABLE} WHERE user_id = %s", (uid,))
    if not row:
        return {}
    return {
        "username":        _normalize_username(row.get("username")),
        "bio":             str(row.get("bio", "") or ""),
        "avatar_url":      _avatar_url_for_response(row.get("avatar_url", "")),
        "profile_tagline": str(row.get("profile_tagline", "") or ""),
    }
def _sync_profile_settings(conn, user_id: str, progress_blob: dict) -> None:
    if not isinstance(progress_blob, dict):
        return
    profile = progress_blob.get("profile") if isinstance(progress_blob.get("profile"), dict) else {}
    if not profile:
        return
    uid = _user_id_text(user_id)
    if not uid:
        return
    _exec(conn, f"""
        INSERT INTO {USER_PROFILE_SETTINGS_TABLE} (user_id, username, bio, avatar_url, profile_tagline, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            username        = EXCLUDED.username,
            bio             = EXCLUDED.bio,
            avatar_url      = EXCLUDED.avatar_url,
            profile_tagline = EXCLUDED.profile_tagline,
            updated_at      = EXCLUDED.updated_at
    """, (
        uid,
        _normalize_username(profile.get("username")) or None,
        str(profile.get("bio", "") or "")[:180],
        _normalize_avatar_url(profile.get("avatar_url")),
        _normalize_profile_tagline(profile.get("profile_tagline")),
        _utc_timestamp(),
    ))
                                                                             
                     
                                                                             
def _load_badges_from_tables(conn, user_id: str) -> dict:
    uid = _user_id_text(user_id)
    if not uid:
        return {}
    unlock_rows = _rows(conn, f"SELECT badge_key, title, unlocked_at FROM {USER_BADGE_UNLOCKS_TABLE} WHERE user_id = %s", (uid,))
    unlocked = {}
    for row in unlock_rows:
        bk = str(row.get("badge_key") or "").strip()
        if bk:
            unlocked[bk] = {"unlockedAt": row.get("unlocked_at"), "title": row.get("title") or bk}
    meta_row = _row(conn, f"SELECT last_synced_at, total_unlocks FROM {USER_BADGES_META_TABLE} WHERE user_id = %s", (uid,))
    meta = meta_row or {}
    return {
        "unlockedBadges": unlocked,
        "totalUnlocks":   _safe_int(meta.get("total_unlocks"), len(unlocked)),
        "lastSyncedAt":   meta.get("last_synced_at"),
    }
def _sync_badges_to_tables(conn, user_id: str, badges_data: dict) -> None:
    if not isinstance(badges_data, dict):
        return
    uid = _user_id_text(user_id)
    if not uid:
        return
    unlock_map = badges_data.get("unlockedBadges") if isinstance(badges_data.get("unlockedBadges"), dict) else {}
    _exec(conn, f"DELETE FROM {USER_BADGE_UNLOCKS_TABLE} WHERE user_id = %s", (uid,))
    for badge_key, entry in unlock_map.items():
        key = str(badge_key or "").strip()
        if not key:
            continue
        entry_obj = entry if isinstance(entry, dict) else {}
        _exec(conn, f"""
            INSERT INTO {USER_BADGE_UNLOCKS_TABLE} (user_id, badge_key, title, unlocked_at, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, badge_key) DO UPDATE SET
                title       = EXCLUDED.title,
                unlocked_at = EXCLUDED.unlocked_at,
                updated_at  = EXCLUDED.updated_at
        """, (uid, key, str(entry_obj.get("title") or key)[:200], entry_obj.get("unlockedAt") or _utc_timestamp(), _utc_timestamp()))
    _exec(conn, f"""
        INSERT INTO {USER_BADGES_META_TABLE} (user_id, last_synced_at, total_unlocks, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            last_synced_at = EXCLUDED.last_synced_at,
            total_unlocks  = EXCLUDED.total_unlocks,
            updated_at     = EXCLUDED.updated_at
    """, (uid, badges_data.get("lastSyncedAt") or _utc_timestamp(), _safe_int(badges_data.get("totalUnlocks"), len(unlock_map)), _utc_timestamp()))
                                                                             
                           
                                                                             
def _load_gamification_from_tables(conn, user_id: str) -> dict | None:
    uid = _user_id_text(user_id)
    if not uid:
        return None
    state_row = _row(conn, f"SELECT * FROM {USER_GAMIFICATION_STATE_TABLE} WHERE user_id = %s", (uid,))
    if not state_row:
        return None
    quest_rows   = _rows(conn, f"SELECT * FROM {USER_GAMIFICATION_QUESTS_TABLE} WHERE user_id = %s ORDER BY quest_order", (uid,))
    effects_rows = _rows(conn, f"SELECT effect_name FROM {USER_PROFILE_EFFECTS_TABLE} WHERE user_id = %s", (uid,))
    effects = [str(r.get("effect_name") or "").strip() for r in effects_rows if str(r.get("effect_name") or "").strip()]
    quests_list = []
    for r in quest_rows:
        quests_list.append({
            "id":        r.get("quest_id"),
            "icon":      r.get("icon"),
            "label":     r.get("label"),
            "reward":    _safe_int(r.get("reward"), 0),
            "target":    _safe_int(r.get("target"), 0),
            "claimed":   bool(r.get("claimed")),
            "progress":  _safe_int(r.get("progress"), 0),
            "completed": bool(r.get("completed")),
        })
    return {
        "hearts":      {"current": _safe_int(state_row.get("hearts_current"), 5), "max": _safe_int(state_row.get("hearts_max"), 5), "lastRefillDate": _sanitize_date_only(state_row.get("hearts_last_refill_date"), None)},
        "xp":          {"total": _safe_int(state_row.get("xp_total"), 0), "daily": _safe_int(state_row.get("xp_daily"), 0), "dailyDate": _sanitize_date_only(state_row.get("xp_daily_date"), None)},
        "coins":       _safe_int(state_row.get("coins"), 0),
        "streak":      {"current": _safe_int(state_row.get("streak_current"), 0), "longest": _safe_int(state_row.get("streak_longest"), 0), "lastDate": _sanitize_date_only(state_row.get("streak_last_date"), None)},
        "leaderboard": {"weeklyXp": _safe_int(state_row.get("leaderboard_weekly_xp"), 0), "weekStart": _sanitize_date_only(state_row.get("leaderboard_week_start"), None), "leagueIndex": _safe_int(state_row.get("leaderboard_league_index"), 0)},
        "quests":      {"date": _sanitize_date_only(state_row.get("quests_date"), None), "list": quests_list},
        "inventory":   {"doubleXp": _safe_int(state_row.get("inventory_double_xp"), 0), "streakFreezes": _safe_int(state_row.get("inventory_streak_freezes"), 0), "profileEffects": _sanitize_profile_effects(effects)},
        "boxReady":         bool(state_row.get("box_ready")),
        "lessonsSinceBox":  _safe_int(state_row.get("lessons_since_box"), 0),
        "savedAt":          state_row.get("saved_at"),
    }
def _sync_gamification_to_tables(conn, user_id: str, gamification: dict) -> None:
    if not isinstance(gamification, dict):
        return
    uid = _user_id_text(user_id)
    if not uid:
        return
    _exec(conn, f"""
        INSERT INTO {USER_GAMIFICATION_STATE_TABLE}
            (user_id, xp_daily, xp_total, xp_daily_date, coins,
             hearts_max, hearts_current, hearts_last_refill_date, quests_date,
             streak_current, streak_longest, streak_last_date, saved_at,
             box_ready, inventory_double_xp, inventory_streak_freezes,
             leaderboard_weekly_xp, leaderboard_week_start, leaderboard_league_index,
             lessons_since_box, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
            xp_daily                 = EXCLUDED.xp_daily,
            xp_total                 = EXCLUDED.xp_total,
            xp_daily_date            = EXCLUDED.xp_daily_date,
            coins                    = EXCLUDED.coins,
            hearts_max               = EXCLUDED.hearts_max,
            hearts_current           = EXCLUDED.hearts_current,
            hearts_last_refill_date  = EXCLUDED.hearts_last_refill_date,
            quests_date              = EXCLUDED.quests_date,
            streak_current           = EXCLUDED.streak_current,
            streak_longest           = EXCLUDED.streak_longest,
            streak_last_date         = EXCLUDED.streak_last_date,
            saved_at                 = EXCLUDED.saved_at,
            box_ready                = EXCLUDED.box_ready,
            inventory_double_xp      = EXCLUDED.inventory_double_xp,
            inventory_streak_freezes = EXCLUDED.inventory_streak_freezes,
            leaderboard_weekly_xp    = EXCLUDED.leaderboard_weekly_xp,
            leaderboard_week_start   = EXCLUDED.leaderboard_week_start,
            leaderboard_league_index = EXCLUDED.leaderboard_league_index,
            lessons_since_box        = EXCLUDED.lessons_since_box,
            updated_at               = EXCLUDED.updated_at
    """, (
        uid,
        _safe_int((gamification.get("xp") or {}).get("daily"), 0),
        _safe_int((gamification.get("xp") or {}).get("total"), 0),
        _sanitize_date_only((gamification.get("xp") or {}).get("dailyDate"), None),
        _safe_int(gamification.get("coins"), 0),
        _safe_int((gamification.get("hearts") or {}).get("max"), 5),
        _safe_int((gamification.get("hearts") or {}).get("current"), 5),
        _sanitize_date_only((gamification.get("hearts") or {}).get("lastRefillDate"), None),
        _sanitize_date_only((gamification.get("quests") or {}).get("date"), None),
        _safe_int((gamification.get("streak") or {}).get("current"), 0),
        _safe_int((gamification.get("streak") or {}).get("longest"), 0),
        _sanitize_date_only((gamification.get("streak") or {}).get("lastDate"), None),
        gamification.get("savedAt") or _utc_timestamp(),
        bool(gamification.get("boxReady", False)),
        _safe_int((gamification.get("inventory") or {}).get("doubleXp"), 0),
        _safe_int((gamification.get("inventory") or {}).get("streakFreezes"), 0),
        _safe_int((gamification.get("leaderboard") or {}).get("weeklyXp"), 0),
        _sanitize_date_only((gamification.get("leaderboard") or {}).get("weekStart"), None),
        _safe_int((gamification.get("leaderboard") or {}).get("leagueIndex"), 0),
        _safe_int(gamification.get("lessonsSinceBox"), 0),
        _utc_timestamp(),
    ))
    quests = (gamification.get("quests") or {}).get("list") if isinstance((gamification.get("quests") or {}).get("list"), list) else []
    _exec(conn, f"DELETE FROM {USER_GAMIFICATION_QUESTS_TABLE} WHERE user_id = %s", (uid,))
    for index, quest in enumerate(quests):
        if not isinstance(quest, dict):
            continue
        quest_id = str(quest.get("id") or "").strip()
        if not quest_id:
            continue
        _exec(conn, f"""
            INSERT INTO {USER_GAMIFICATION_QUESTS_TABLE}
                (user_id, quest_id, icon, label, reward, target, claimed, progress, completed, quest_order, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id, quest_id) DO UPDATE SET
                icon        = EXCLUDED.icon,
                label       = EXCLUDED.label,
                reward      = EXCLUDED.reward,
                target      = EXCLUDED.target,
                claimed     = EXCLUDED.claimed,
                progress    = EXCLUDED.progress,
                completed   = EXCLUDED.completed,
                quest_order = EXCLUDED.quest_order,
                updated_at  = EXCLUDED.updated_at
        """, (uid, quest_id, str(quest.get("icon") or "")[:20], str(quest.get("label") or "")[:200],
              _safe_int(quest.get("reward"), 0), _safe_int(quest.get("target"), 0),
              bool(quest.get("claimed")), _safe_int(quest.get("progress"), 0),
              bool(quest.get("completed")), index, _utc_timestamp()))
    effects = _sanitize_profile_effects((gamification.get("inventory") or {}).get("profileEffects"))
    _exec(conn, f"DELETE FROM {USER_PROFILE_EFFECTS_TABLE} WHERE user_id = %s", (uid,))
    for effect in effects:
        _exec(conn, f"""
            INSERT INTO {USER_PROFILE_EFFECTS_TABLE} (user_id, effect_name, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, effect_name) DO UPDATE SET updated_at = EXCLUDED.updated_at
        """, (uid, effect, _utc_timestamp()))
                                                                             
                              
                                                                             
def _is_username_taken(conn, username: str, exclude_user_id: str | None = None) -> bool:
    normalized = _normalize_username(username)
    if not normalized:
        return False
    rows = _rows(conn, f"SELECT user_id FROM {USER_PROFILE_SETTINGS_TABLE} WHERE username = %s", (normalized,))
    for r in rows:
        if exclude_user_id and str(r.get("user_id") or "") == str(exclude_user_id):
            continue
        return True
    return False
def _find_progress_row_by_username(conn, username: str) -> dict | None:
    normalized = _normalize_username(username)
    if not _is_valid_username(normalized):
        return None
                                                  
    profile_row = _row(conn, f"SELECT user_id FROM {USER_PROFILE_SETTINGS_TABLE} WHERE username = %s", (normalized,))
    if profile_row:
        uid = profile_row.get("user_id")
        if uid:
            return _row(conn, f"SELECT * FROM {USER_PROGRESS_TABLE} WHERE user_id = %s", (uid,)) or {"user_id": uid, "progress": {}}
                                        
    rows = _rows(conn, f"SELECT * FROM {USER_PROGRESS_TABLE}", ())
    for row in rows:
        blob = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(blob, dict):
            continue
        profile = blob.get("profile", {})
        if _normalize_username(profile.get("username")) == normalized:
            return row
    return None
                                                                             
                                                                       
                                                                             
def _fetch_raw_progress(conn, user_id: str) -> dict:
    row = _row(conn, f"SELECT * FROM {USER_PROGRESS_TABLE} WHERE user_id = %s", (user_id,))
    return _normalize_progress_row(row, user_id)
                                                                             
                                                                             
                                                                             
def _sanitize_progress_for_save(conn, user_id: str, incoming_row: dict | None) -> dict:
    normalized_incoming = _normalize_progress_row(incoming_row, user_id)
    existing_row        = _fetch_raw_progress(conn, user_id)
    existing_progress = existing_row.get("progress")  if isinstance(existing_row.get("progress"),  dict) else {}
    incoming_progress = normalized_incoming.get("progress") if isinstance(normalized_incoming.get("progress"), dict) else {}
    sanitized_progress = dict(existing_progress)
    sanitized_progress.update(incoming_progress)
    existing_gamification = existing_progress.get("gamification")  if isinstance(existing_progress.get("gamification"),  dict) else {}
    incoming_gamification = incoming_progress.get("gamification")  if isinstance(incoming_progress.get("gamification"),  dict) else None
    existing_profile      = existing_progress.get("profile")       if isinstance(existing_progress.get("profile"),       dict) else {}
    incoming_has_profile  = isinstance(incoming_progress.get("profile"), dict)
    suspicious_profile_drop = bool(existing_profile.get("username")) and bool(incoming_progress) and not incoming_has_profile
    sanitized_gamification = None
    suspicious_gamification = False
    if incoming_gamification is not None:
        sanitized_gamification = _sanitize_gamification_state(incoming_gamification, existing_gamification)
        sanitized_progress["gamification"] = sanitized_gamification
        suspicious_gamification = _is_suspicious_gamification_payload(incoming_gamification, sanitized_gamification)
    elif existing_gamification:
        sanitized_gamification = _sanitize_gamification_state(existing_gamification, existing_gamification)
        sanitized_progress["gamification"] = sanitized_gamification
    else:
        sanitized_progress.pop("gamification", None)
    top_xp     = _safe_int(existing_row.get("xp"), 0)
    top_streak = _safe_int(existing_row.get("streak"), 0)
    if isinstance(sanitized_gamification, dict):
        top_xp     = _safe_int((sanitized_gamification.get("xp")     or {}).get("total"),   top_xp)
        top_streak = _safe_int((sanitized_gamification.get("streak") or {}).get("current"), top_streak)
    sanitized_last_active = _sanitize_date_only(normalized_incoming.get("last_active"), _sanitize_date_only(existing_row.get("last_active"), None))
    if sanitized_last_active is None and isinstance(sanitized_gamification, dict):
        sanitized_last_active = _sanitize_date_only((sanitized_gamification.get("streak") or {}).get("lastDate"), None)
    missions = normalized_incoming.get("missions") if isinstance(normalized_incoming.get("missions"), dict) else existing_row.get("missions", {})
    mistakes = normalized_incoming.get("mistakes") if isinstance(normalized_incoming.get("mistakes"), list) else existing_row.get("mistakes", [])
    return {
        "user_id":          user_id,
        "progress":         sanitized_progress,
        "xp":               _safe_int(top_xp, 0),
        "streak":           _safe_int(top_streak, 0),
        "last_active":      sanitized_last_active,
        "missions":         missions,
        "mistakes":         mistakes[:500],
        "__cheat_detected": bool(suspicious_profile_drop or suspicious_gamification),
    }
                                                                             
                       
                                                                             
def get_user_progress(user_id: str) -> dict:
    try:
        with _db() as conn:
            row          = _fetch_raw_progress(conn, user_id)
            progress_blob = row.get("progress") if isinstance(row.get("progress"), dict) else {}
            profile_from_table = _load_profile_settings(conn, user_id)
            if profile_from_table:
                profile_existing = progress_blob.get("profile") if isinstance(progress_blob.get("profile"), dict) else {}
                merged = dict(profile_existing)
                merged.update({k: v for k, v in profile_from_table.items() if v not in (None, "")})
                progress_blob["profile"] = merged
            badges_from_table = _load_badges_from_tables(conn, user_id)
            if badges_from_table and isinstance(badges_from_table.get("unlockedBadges"), dict):
                progress_blob["badges"] = badges_from_table
            gamification_from_table = _load_gamification_from_tables(conn, user_id)
            if isinstance(gamification_from_table, dict):
                progress_blob["gamification"] = gamification_from_table
                row["xp"]     = _safe_int((gamification_from_table.get("xp")     or {}).get("total"),   row.get("xp", 0))
                row["streak"] = _safe_int((gamification_from_table.get("streak") or {}).get("current"), row.get("streak", 0))
            row["progress"] = progress_blob
            return row
    except Exception as e:
        return {"error": str(e)}
def save_user_progress(user_id: str, progress_data: dict) -> dict:
    try:
        with _db() as conn:
            data_to_save   = _sanitize_progress_for_save(conn, user_id, progress_data)
            cheat_detected = bool(data_to_save.pop("__cheat_detected", False))
            if cheat_detected:
                return {"error": "Cheat detected", "cheat_detected": True}
            if isinstance(data_to_save.get("last_active"), datetime):
                data_to_save["last_active"] = data_to_save["last_active"].strftime("%Y-%m-%d")
            _exec(conn, f"""
                INSERT INTO {USER_PROGRESS_TABLE}
                    (user_id, progress, xp, streak, last_active, missions, mistakes, updated_at)
                VALUES (%s, %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    progress    = EXCLUDED.progress,
                    xp          = EXCLUDED.xp,
                    streak      = EXCLUDED.streak,
                    last_active = EXCLUDED.last_active,
                    missions    = EXCLUDED.missions,
                    mistakes    = EXCLUDED.mistakes,
                    updated_at  = NOW()
            """, (
                data_to_save["user_id"],
                json.dumps(data_to_save["progress"]),
                data_to_save["xp"],
                data_to_save["streak"],
                data_to_save["last_active"],
                json.dumps(data_to_save["missions"]),
                json.dumps(data_to_save["mistakes"]),
            ))
            progress_blob = data_to_save.get("progress") if isinstance(data_to_save.get("progress"), dict) else {}
            try:
                _sync_profile_settings(conn, user_id, progress_blob)
            except Exception:
                pass
            try:
                badges_blob = progress_blob.get("badges") if isinstance(progress_blob.get("badges"), dict) else None
                if badges_blob is not None:
                    _sync_badges_to_tables(conn, user_id, badges_blob)
            except Exception:
                pass
            try:
                gamification_blob = progress_blob.get("gamification") if isinstance(progress_blob.get("gamification"), dict) else None
                if gamification_blob is not None:
                    _sync_gamification_to_tables(conn, user_id, gamification_blob)
            except Exception:
                pass
            return {"success": True}
    except Exception as e:
        return {"error": str(e)}
def get_user_badges(user_id: str) -> dict:
    try:
        with _db() as conn:
            table_badges = _load_badges_from_tables(conn, user_id)
            if table_badges and isinstance(table_badges.get("unlockedBadges"), dict):
                return table_badges
    except Exception:
        pass
    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}
    blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    return blob.get("badges", {}) if isinstance(blob, dict) else {}
def save_user_badges(user_id: str, badges_data: dict) -> dict:
    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}
    blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    if not isinstance(blob, dict):
        blob = {}
    blob["badges"] = badges_data if isinstance(badges_data, dict) else {}
    payload = _build_progress_payload(progress_row, blob)
    result = save_user_progress(user_id, payload)
    if "error" in result:
        return result
    try:
        with _db() as conn:
            _sync_badges_to_tables(conn, user_id, badges_data if isinstance(badges_data, dict) else {})
    except Exception:
        pass
    return result
                                                                             
                       
                                                                             
def get_user_profile(user_id: str) -> dict:
    progress_row = get_user_progress(user_id)
    if isinstance(progress_row, dict) and "error" in progress_row:
        return {"error": progress_row["error"]}
    blob    = progress_row.get("progress") if isinstance(progress_row, dict) else {}
    if not isinstance(blob, dict):
        blob = {}
    profile = blob.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}
    badges  = blob.get("badges", {})
    if not isinstance(badges, dict):
        badges = {}
    unlocked_badges = badges.get("unlockedBadges", {})
    if not isinstance(unlocked_badges, dict):
        unlocked_badges = {}
    return {
        "user_id":         user_id,
        "username":        profile.get("username"),
        "bio":             profile.get("bio", ""),
        "avatar_url":      _avatar_url_for_response(profile.get("avatar_url", "")),
        "profile_tagline": profile.get("profile_tagline", ""),
        "xp":              progress_row.get("xp", 0),
        "streak":          progress_row.get("streak", 0),
        "last_active":     progress_row.get("last_active"),
        "badges_unlocked": len(unlocked_badges),
    }
def update_user_profile(user_id: str, username: str | None = None, bio: str | None = None,
                        avatar_url: str | None = None, profile_tagline: str | None = None) -> dict:
    profile_row = get_user_progress(user_id)
    if isinstance(profile_row, dict) and "error" in profile_row:
        return {"error": profile_row["error"]}
    blob = profile_row.get("progress") if isinstance(profile_row, dict) else {}
    if not isinstance(blob, dict):
        blob = {}
    profile = blob.get("profile", {})
    if not isinstance(profile, dict):
        profile = {}
    is_bio_banned = bool(profile.get("bio_banned"))
    if username is not None:
        normalized = _normalize_username(username)
        if not _is_valid_username(normalized):
            return {"error": "Username must be 3-24 chars (letters, numbers, underscore)."}
        try:
            with _db() as conn:
                if _is_username_taken(conn, normalized, exclude_user_id=user_id):
                    return {"error": "Username is already taken."}
        except Exception as e:
            return {"error": str(e)}
        profile["username"] = normalized
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
    blob["profile"] = profile
    payload = _build_progress_payload(profile_row, blob)
    result = save_user_progress(user_id, payload)
    if "error" in result:
        return {"error": result["error"]}
    return get_user_profile(user_id)
def get_public_profile_by_username(username: str) -> dict:
    normalized = _normalize_username(username)
    if not _is_valid_username(normalized):
        return {"error": "Invalid username"}
    try:
        with _db() as conn:
            row = _find_progress_row_by_username(conn, normalized)
            if row:
                uid = row.get("user_id")
                if uid:
                    full = get_user_profile(uid)
                    if "error" not in full and _normalize_username(full.get("username")) == normalized:
                        return {
                            "username":        _normalize_username(full.get("username")),
                            "bio":             full.get("bio", ""),
                            "avatar_url":      _avatar_url_for_response(full.get("avatar_url", "")),
                            "profile_tagline": full.get("profile_tagline", ""),
                            "xp":              full.get("xp", 0),
                            "streak":          full.get("streak", 0),
                            "last_active":     full.get("last_active"),
                            "badges_unlocked": full.get("badges_unlocked", 0),
                        }
            return {"error": "Profile not found"}
    except Exception as e:
        return {"error": str(e)}
                                                                             
                         
                                                                             
def report_profile_bio(reported_username: str, reporter_user_id: str,
                       reason: str | None = None, details: str | None = None) -> dict:
    try:
        with _db() as conn:
            row = _find_progress_row_by_username(conn, reported_username)
        if not row:
            return {"error": "Profile not found"}
        target_user_id = row.get("user_id")
        if not target_user_id:
            return {"error": "Profile not found"}
        if target_user_id == reporter_user_id:
            return {"error": "You cannot report your own bio."}
        blob    = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(blob, dict):
            blob = {}
        reports = blob.get("profile_reports", [])
        if not isinstance(reports, list):
            reports = []
        reports.append({
            "report_id":          str(uuid.uuid4()),
            "type":               "bio",
            "status":             "open",
            "reported_username":  _normalize_username(reported_username),
            "reported_user_id":   target_user_id,
            "reporter_user_id":   reporter_user_id,
            "reason":             (reason or "Inappropriate content").strip()[:120],
            "details":            (details or "").strip()[:300],
            "created_at":         _utc_timestamp(),
        })
        if len(reports) > 200:
            reports = reports[-200:]
        blob["profile_reports"] = reports
        payload = _build_progress_payload(row, blob)
        result  = save_user_progress(target_user_id, payload)
        if "error" in result:
            return {"error": result["error"]}
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}
def list_bio_reports(status: str | None = "open", limit: int = 100) -> dict:
    try:
        with _db() as conn:
            rows = _rows(conn, f"SELECT user_id, progress FROM {USER_PROGRESS_TABLE}", ())
        items: list[dict] = []
        normalized_status = (status or "").strip().lower()
        for row in rows:
            blob = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(blob, dict):
                continue
            profile = blob.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            for report in (blob.get("profile_reports", []) or []):
                if not isinstance(report, dict):
                    continue
                if report.get("type") != "bio":
                    continue
                report_status = str(report.get("status", "open")).lower()
                if normalized_status and normalized_status != "all" and report_status != normalized_status:
                    continue
                items.append({
                    "report_id":          report.get("report_id"),
                    "status":             report_status,
                    "reason":             report.get("reason", ""),
                    "details":            report.get("details", ""),
                    "created_at":         report.get("created_at"),
                    "closed_at":          report.get("closed_at"),
                    "closed_by":          report.get("closed_by"),
                    "close_note":         report.get("close_note", ""),
                    "resolved_at":        report.get("resolved_at"),
                    "resolved_by":        report.get("resolved_by"),
                    "reported_user_id":   report.get("reported_user_id") or row.get("user_id"),
                    "reported_username":  report.get("reported_username") or profile.get("username"),
                    "current_bio":        profile.get("bio", ""),
                    "reporter_user_id":   report.get("reporter_user_id"),
                })
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {"success": True, "reports": items[: max(1, min(limit, 500))]}
    except Exception as e:
        return {"error": str(e)}
def ban_profile_bio(reported_username: str, admin_user_id: str, replacement_bio: str | None = None) -> dict:
    try:
        with _db() as conn:
            row = _find_progress_row_by_username(conn, reported_username)
        if not row:
            return {"error": "Profile not found"}
        target_user_id = row.get("user_id")
        if not target_user_id:
            return {"error": "Profile not found"}
        blob    = row.get("progress") if isinstance(row, dict) else {}
        if not isinstance(blob, dict):
            blob = {}
        profile = blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        replacement_text = (replacement_bio or "Bio removed by moderation.").strip()[:180]
        profile.update({"bio": replacement_text, "bio_banned": True, "bio_banned_at": _utc_timestamp(), "bio_banned_by": admin_user_id})
        blob["profile"] = profile
        for report in (blob.get("profile_reports", []) or []):
            if isinstance(report, dict) and report.get("type") == "bio" and str(report.get("status", "open")).lower() == "open":
                report.update({"status": "resolved", "resolved_at": _utc_timestamp(), "resolved_by": admin_user_id})
        result = save_user_progress(target_user_id, _build_progress_payload(row, blob))
        if "error" in result:
            return {"error": result["error"]}
        return {"success": True, "username": _normalize_username(reported_username), "bio": replacement_text}
    except Exception as e:
        return {"error": str(e)}
def close_bio_report(report_id: str, admin_user_id: str, close_note: str | None = None) -> dict:
    try:
        rid = (report_id or "").strip()
        if not rid:
            return {"error": "Missing report_id"}
        with _db() as conn:
            rows = _rows(conn, f"SELECT * FROM {USER_PROGRESS_TABLE}", ())
        for row in rows:
            user_id = row.get("user_id")
            blob    = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(blob, dict):
                continue
            reports = blob.get("profile_reports", [])
            if not isinstance(reports, list):
                continue
            changed = False
            for report in reports:
                if not isinstance(report, dict):
                    continue
                if str(report.get("report_id", "")).strip() != rid:
                    continue
                if str(report.get("status", "open")).lower() != "open":
                    return {"error": "Only open reports can be closed."}
                report.update({"status": "dismissed", "closed_at": _utc_timestamp(), "closed_by": admin_user_id, "close_note": (close_note or "").strip()[:240]})
                changed = True
                break
            if not changed:
                continue
            blob["profile_reports"] = reports
            result = save_user_progress(user_id, _build_progress_payload(row, blob))
            if "error" in result:
                return {"error": result["error"]}
            return {"success": True, "report_id": rid, "status": "dismissed"}
        return {"error": "Report not found"}
    except Exception as e:
        return {"error": str(e)}
def list_all_profiles(limit: int = 500) -> dict:
    try:
        with _db() as conn:
            rows         = _rows(conn, f"SELECT user_id, progress, xp, streak, last_active FROM {USER_PROGRESS_TABLE}", ())
            profile_rows = _rows(conn, f"SELECT user_id, username, bio, avatar_url, profile_tagline FROM {USER_PROFILE_SETTINGS_TABLE}", ())
            badge_rows   = _rows(conn, f"SELECT user_id, total_unlocks FROM {USER_BADGES_META_TABLE}", ())
        profile_map  = {_user_id_text(r.get("user_id")): r for r in profile_rows if isinstance(r, dict) and _user_id_text(r.get("user_id"))}
        badge_map    = {_user_id_text(r.get("user_id")): _safe_int(r.get("total_unlocks"), 0) for r in badge_rows if isinstance(r, dict) and _user_id_text(r.get("user_id"))}
        items: list[dict] = []
        for row in rows:
            blob    = row.get("progress") if isinstance(row, dict) else {}
            if not isinstance(blob, dict):
                blob = {}
            profile = blob.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            tp = profile_map.get(_user_id_text(row.get("user_id")), {})
            if isinstance(tp, dict):
                profile = {**profile, **{k: tp[k] for k in ("username", "bio", "avatar_url", "profile_tagline") if tp.get(k) is not None}}
            badges_unlocked = badge_map.get(_user_id_text(row.get("user_id")), len((blob.get("badges") or {}).get("unlockedBadges") or {}))
            items.append({
                "user_id":               row.get("user_id"),
                "username":              _normalize_username(profile.get("username")) or None,
                "bio":                   str(profile.get("bio", "") or ""),
                "avatar_url":            _avatar_url_for_response(profile.get("avatar_url", "")),
                "bio_banned":            bool(profile.get("bio_banned")),
                "bio_banned_at":         profile.get("bio_banned_at"),
                "bio_banned_by":         profile.get("bio_banned_by"),
                "account_banned":        bool(profile.get("account_banned")),
                "account_banned_reason": str(profile.get("account_banned_reason", "") or ""),
                "account_banned_at":     profile.get("account_banned_at"),
                "account_banned_by":     profile.get("account_banned_by"),
                "badges_unlocked":       badges_unlocked,
                "xp":                    row.get("xp", 0),
                "streak":                row.get("streak", 0),
                "last_active":           _row_last_active(row),
            })
        items.sort(key=lambda x: (x.get("username") or "~").lower())
        return {"success": True, "profiles": items[: max(1, min(limit, 1000))]}
    except Exception as e:
        return {"error": str(e)}
                                                                             
                          
                                                                             
def get_account_ban_status(user_id: str) -> dict:
    try:
        progress_row = get_user_progress(user_id)
        if isinstance(progress_row, dict) and "error" in progress_row:
            return {"error": progress_row["error"]}
        blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
        if not isinstance(blob, dict):
            blob = {}
        profile = blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        return {
            "success":    True,
            "user_id":    user_id,
            "banned":     bool(profile.get("account_banned")),
            "reason":     str(profile.get("account_banned_reason", "") or ""),
            "banned_at":  profile.get("account_banned_at"),
            "banned_by":  profile.get("account_banned_by"),
            "appeal_url": str(profile.get("account_ban_appeal_url", "") or ""),
        }
    except Exception as e:
        return {"error": str(e)}
def ban_user_account(target_user_id: str, admin_user_id: str,
                     reason: str | None = None, appeal_url: str | None = None) -> dict:
    try:
        if not target_user_id:
            return {"error": "Missing user_id"}
        if target_user_id == admin_user_id:
            return {"error": "You cannot ban your own account."}
        progress_row = get_user_progress(target_user_id)
        if isinstance(progress_row, dict) and "error" in progress_row:
            return {"error": progress_row["error"]}
        blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
        if not isinstance(blob, dict):
            blob = {}
        profile = blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        profile.update({
            "account_banned":        True,
            "account_banned_reason": (reason or "Account suspended for severe rule violations.").strip()[:300],
            "account_banned_at":     _utc_timestamp(),
            "account_banned_by":     admin_user_id,
        })
        if appeal_url is not None:
            profile["account_ban_appeal_url"] = str(appeal_url).strip()[:300]
        blob["profile"] = profile
        result = save_user_progress(target_user_id, _build_progress_payload(progress_row, blob))
        if "error" in result:
            return {"error": result["error"]}
        return {"success": True, "user_id": target_user_id, "account_banned": True,
                "account_banned_reason": profile.get("account_banned_reason", ""),
                "account_banned_at": profile.get("account_banned_at"),
                "account_banned_by": profile.get("account_banned_by"),
                "account_ban_appeal_url": profile.get("account_ban_appeal_url", "")}
    except Exception as e:
        return {"error": str(e)}
def unban_user_account(target_user_id: str, admin_user_id: str) -> dict:
    try:
        if not target_user_id:
            return {"error": "Missing user_id"}
        progress_row = get_user_progress(target_user_id)
        if isinstance(progress_row, dict) and "error" in progress_row:
            return {"error": progress_row["error"]}
        blob = progress_row.get("progress") if isinstance(progress_row, dict) else {}
        if not isinstance(blob, dict):
            blob = {}
        profile = blob.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}
        profile.update({"account_banned": False, "account_unbanned_at": _utc_timestamp(),
                         "account_unbanned_by": admin_user_id, "account_banned_reason": ""})
        blob["profile"] = profile
        result = save_user_progress(target_user_id, _build_progress_payload(progress_row, blob))
        if "error" in result:
            return {"error": result["error"]}
        return {"success": True, "user_id": target_user_id, "account_banned": False,
                "account_unbanned_at": profile.get("account_unbanned_at"),
                "account_unbanned_by": profile.get("account_unbanned_by")}
    except Exception as e:
        return {"error": str(e)}
                                                                             
                                                                           
                                                                             
def delete_account_data(user_id: str) -> dict:
    """Delete all rows for this user from every table. Clerk deletes the auth record separately."""
    uid = _user_id_text(user_id)
    if not uid:
        return {"error": "Missing user_id"}
    try:
        with _db() as conn:
            for table in (
                USER_PROGRESS_TABLE,
                USER_PROFILE_SETTINGS_TABLE,
                USER_BADGE_UNLOCKS_TABLE,
                USER_BADGES_META_TABLE,
                USER_GAMIFICATION_STATE_TABLE,
                USER_GAMIFICATION_QUESTS_TABLE,
                USER_PROFILE_EFFECTS_TABLE,
            ):
                _exec(conn, f"DELETE FROM {table} WHERE user_id = %s", (uid,))
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}
                                                                             
                                                              
                                                                             
def count_users() -> int:
    try:
        with _db() as conn:
            row = _row(conn, f"SELECT COUNT(*) AS n FROM {USER_PROGRESS_TABLE}", ())
            return int((row or {}).get("n", 0))
    except Exception:
        return 0
                                                                             
                       
                                                                             
def backfill_normalized_user_tables(limit: int = 0) -> dict:
    try:
        with _db() as conn:
            rows = _rows(conn, f"SELECT user_id, progress FROM {USER_PROGRESS_TABLE}", ())
        bounded = rows if not (isinstance(limit, int) and limit > 0) else rows[:limit]
        processed = profile_synced = badges_synced = gamification_synced = 0
        errors: list[dict] = []
        for row in bounded:
            if not isinstance(row, dict):
                continue
            uid = _user_id_text(row.get("user_id"))
            if not uid:
                continue
            blob = row.get("progress") if isinstance(row.get("progress"), dict) else {}
            processed += 1
            try:
                with _db() as conn:
                    profile = blob.get("profile") if isinstance(blob.get("profile"), dict) else None
                    if profile and any(k in profile for k in ("username", "bio", "avatar_url", "profile_tagline")):
                        _sync_profile_settings(conn, uid, blob)
                        profile_synced += 1
                    badges = blob.get("badges") if isinstance(blob.get("badges"), dict) else None
                    if badges and isinstance(badges.get("unlockedBadges"), dict):
                        _sync_badges_to_tables(conn, uid, badges)
                        badges_synced += 1
                    gamification = blob.get("gamification") if isinstance(blob.get("gamification"), dict) else None
                    if gamification:
                        _sync_gamification_to_tables(conn, uid, gamification)
                        gamification_synced += 1
            except Exception as sync_error:
                errors.append({"user_id": uid, "error": str(sync_error)[:240]})
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
