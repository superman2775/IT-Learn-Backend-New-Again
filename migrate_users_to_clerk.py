                      
"""
RUN THIS SCRIPT ONCE ON THE SERVER (pythonanywhere) @broodje56
migrate_users_to_clerk.py
=========================
Imports every Supabase auth user into Clerk, then rewrites the user_id
column in all your PostgreSQL tables so they point to the new Clerk user ID.
HOW IT WORKS
------------
1. Reads all users from Supabase via the Admin API (email + metadata).
2. Creates each user in Clerk (email only — no password transfer possible,
   see NOTE below).
3. Stores a mapping:  supabase_user_id  →  clerk_user_id
4. For every table that has a user_id column, rewrites old IDs → new IDs.
5. Writes a mapping CSV so you can audit / re-run safely.
NOTE ON PASSWORDS
-----------------
Supabase stores passwords as bcrypt hashes but does NOT expose them via
the API. Users will receive a "reset your password" email from Clerk on
first login. Clerk supports importing bcrypt hashes only if you export them
directly from the database — if you have direct DB access to Supabase's
auth.users table (e.g. via a self-hosted instance), set
EXPORT_HASHES_FROM_DB=true and provide DB credentials below.
USAGE
-----
1. Fill in the config section below (or set env vars).
2. Run on your server:
       python3 migrate_users_to_clerk.py
3. Check migration_map.csv for the full old→new ID mapping.
4. If anything fails midway, re-running is safe: already-migrated users
   are detected via external_id and skipped.
REQUIREMENTS
------------
    pip install requests psycopg2-binary python-dotenv --break-system-packages
"""
import csv
import os
import sys
import time
import json
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
                                                                             
                                                 
                                                                             
SUPABASE_URL        = os.getenv("SUPABASE_URL",        "https://otgkiqornzlqceurjnse.supabase.co")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "")                     
CLERK_SECRET_KEY    = os.getenv("CLERK_SECRET_KEY",    "")                
CLERK_API_BASE      = "https://api.clerk.com/v1"
DATABASE_URL        = os.getenv("DATABASE_URL",        "postgresql://myapp:your-password@localhost:5432/myapp_db")
                                                     
TABLES_WITH_USER_ID = [
    "user_progress",
    "user_profile_settings",
    "user_badge_unlocks",
    "user_badges_meta",
    "user_gamification_state",
    "user_gamification_quests",
    "user_profile_effects",
]
MAPPING_CSV = "migration_map.csv"                                
                                                               
                                
CLERK_CREATE_DELAY_SECONDS = 0.1
                                                                             
         
                                                                             
def _supabase_headers():
    return {
        "apikey":        SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type":  "application/json",
    }
def _clerk_headers():
    return {
        "Authorization": f"Bearer {CLERK_SECRET_KEY}",
        "Content-Type":  "application/json",
    }
def validate_config():
    errors = []
    if not SUPABASE_SECRET_KEY:
        errors.append("SUPABASE_SECRET_KEY is not set")
    if not CLERK_SECRET_KEY:
        errors.append("CLERK_SECRET_KEY is not set")
    if not DATABASE_URL or "your-password" in DATABASE_URL:
        errors.append("DATABASE_URL still has the placeholder password — update it")
    if errors:
        print("\n[ERROR] Missing configuration:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
                                                                             
                                   
                                                                             
def fetch_supabase_users() -> list[dict]:
    """Pages through Supabase admin users list and returns them all."""
    print("\n[1/4] Fetching users from Supabase...")
    all_users = []
    page      = 1
    per_page  = 1000
    while True:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/admin/users",
            headers=_supabase_headers(),
            params={"page": page, "per_page": per_page},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  [ERROR] Supabase returned {resp.status_code}: {resp.text[:300]}")
            sys.exit(1)
        data  = resp.json()
        users = data.get("users", [])
        all_users.extend(users)
        print(f"  Fetched page {page}: {len(users)} users (total so far: {len(all_users)})")
        if len(users) < per_page:
            break
        page += 1
    print(f"  Done — {len(all_users)} Supabase users found.")
    return all_users
                                                                             
                                                         
                                                                             
def load_existing_map() -> dict[str, str]:
    """Returns { supabase_id: clerk_id } from a previous run's CSV."""
    mapping = {}
    if not os.path.exists(MAPPING_CSV):
        return mapping
    with open(MAPPING_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sb_id = row.get("supabase_id", "").strip()
            cl_id = row.get("clerk_id", "").strip()
            if sb_id and cl_id:
                mapping[sb_id] = cl_id
    print(f"  Loaded {len(mapping)} existing mappings from {MAPPING_CSV}")
    return mapping
                                                                             
                                
                                                                             
def find_clerk_user_by_external_id(external_id: str) -> str | None:
    """Check if a user with this external_id already exists in Clerk."""
    try:
        resp = requests.get(
            f"{CLERK_API_BASE}/users",
            headers=_clerk_headers(),
            params={"external_id": external_id, "limit": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            users = resp.json()
            if isinstance(users, list) and users:
                return users[0]["id"]
    except Exception:
        pass
    return None
def create_clerk_users(supabase_users: list[dict], existing_map: dict[str, str]) -> dict[str, str]:
    """
    Creates Clerk users for every Supabase user not already in the map.
    Returns the full { supabase_id: clerk_id } mapping.
    """
    print(f"\n[2/4] Creating users in Clerk...")
    mapping  = dict(existing_map)
    skipped  = 0
    created  = 0
    failed   = 0
    for i, user in enumerate(supabase_users):
        sb_id = user.get("id", "")
        email = (user.get("email") or "").strip()
        if not sb_id or not email:
            print(f"  [{i+1}] SKIP — missing id or email")
            skipped += 1
            continue
                                            
        if sb_id in mapping:
            print(f"  [{i+1}] SKIP (already mapped) {email}")
            skipped += 1
            continue
                                                               
        existing_clerk_id = find_clerk_user_by_external_id(sb_id)
        if existing_clerk_id:
            print(f"  [{i+1}] FOUND in Clerk (external_id match) {email} → {existing_clerk_id}")
            mapping[sb_id] = existing_clerk_id
            created += 1
            continue
                         
                                                                         
                                                                                           
        payload = {
            "email_address":         [email],
            "external_id":           sb_id,
            "skip_password_checks":  True,
            "skip_password_requirement": True,                                                                  
        }
                                                              
        meta = user.get("user_metadata") or {}
        full_name = meta.get("full_name") or meta.get("name") or ""
        if full_name:
            parts = full_name.strip().split(" ", 1)
            payload["first_name"] = parts[0]
            if len(parts) > 1:
                payload["last_name"] = parts[1]
        try:
            resp = requests.post(
                f"{CLERK_API_BASE}/users",
                headers=_clerk_headers(),
                json=payload,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                clerk_id = resp.json()["id"]
                mapping[sb_id] = clerk_id
                created += 1
                print(f"  [{i+1}] CREATED {email} → {clerk_id}")
            elif resp.status_code == 422:
                                                                          
                err_data = resp.json()
                err_msg  = str(err_data)
                if "email" in err_msg.lower() and "taken" in err_msg.lower():
                                           
                    lookup = requests.get(
                        f"{CLERK_API_BASE}/users",
                        headers=_clerk_headers(),
                        params={"email_address": email, "limit": 1},
                        timeout=10,
                    )
                    if lookup.status_code == 200:
                        existing = lookup.json()
                        if existing:
                            clerk_id = existing[0]["id"]
                            mapping[sb_id] = clerk_id
                            created += 1
                            print(f"  [{i+1}] MATCHED existing Clerk user {email} → {clerk_id}")
                            continue
                print(f"  [{i+1}] FAIL 422 {email}: {err_msg[:200]}")
                failed += 1
            else:
                print(f"  [{i+1}] FAIL {resp.status_code} {email}: {resp.text[:200]}")
                failed += 1
        except Exception as exc:
            print(f"  [{i+1}] EXCEPTION {email}: {exc}")
            failed += 1
                                   
        time.sleep(CLERK_CREATE_DELAY_SECONDS)
    print(f"\n  Results: {created} created/matched, {skipped} skipped, {failed} failed")
    return mapping
                                                                             
                           
                                                                             
def save_mapping(mapping: dict[str, str]) -> None:
    with open(MAPPING_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["supabase_id", "clerk_id"])
        for sb_id, cl_id in mapping.items():
            writer.writerow([sb_id, cl_id])
    print(f"\n  Mapping saved to {MAPPING_CSV} ({len(mapping)} rows)")
                                                                             
                                              
                                                                             
def remap_database(mapping: dict[str, str]) -> None:
    """
    For every table in TABLES_WITH_USER_ID, replaces old Supabase UUIDs
    with the corresponding Clerk user IDs.
    This is done inside a single transaction per table so it's atomic.
    Primary key constraints are handled by doing updates in the right order
    (child tables before parent, then parent) — but since we're just changing
    text IDs that aren't FK-constrained in your schema, any order is fine.
    """
    print(f"\n[3/4] Remapping user_id columns in PostgreSQL...")
    if not mapping:
        print("  No mappings to apply.")
        return
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        for table in TABLES_WITH_USER_ID:
            updated_rows = 0
            with conn.cursor() as cur:
                for sb_id, cl_id in mapping.items():
                    cur.execute(
                        f"UPDATE {table} SET user_id = %s WHERE user_id = %s",
                        (cl_id, sb_id),
                    )
                    updated_rows += cur.rowcount
            conn.commit()
            print(f"  {table}: {updated_rows} rows updated")
                                                                   
                                                                          
                                                                          
        print("\n  Patching JSONB internal user_id references in user_progress...")
        with conn.cursor() as cur:
            for sb_id, cl_id in mapping.items():
                                                                       
                cur.execute("""
                    UPDATE user_progress
                    SET progress = jsonb_set(
                        progress,
                        '{profile_reports}',
                        COALESCE((
                            SELECT jsonb_agg(
                                CASE
                                    WHEN elem->>'reporter_user_id' = %s
                                    THEN jsonb_set(elem, '{reporter_user_id}', %s::jsonb)
                                    ELSE elem
                                END
                            )
                            FROM jsonb_array_elements(
                                COALESCE(progress->'profile_reports', '[]'::jsonb)
                            ) AS elem
                        ), '[]'::jsonb)
                    )
                    WHERE progress->'profile_reports' IS NOT NULL
                      AND progress::text LIKE %s
                """, (sb_id, json.dumps(cl_id), f"%{sb_id}%"))
                                                                       
                cur.execute("""
                    UPDATE user_progress
                    SET progress = jsonb_set(
                        progress,
                        '{profile_reports}',
                        COALESCE((
                            SELECT jsonb_agg(
                                CASE
                                    WHEN elem->>'reported_user_id' = %s
                                    THEN jsonb_set(elem, '{reported_user_id}', %s::jsonb)
                                    ELSE elem
                                END
                            )
                            FROM jsonb_array_elements(
                                COALESCE(progress->'profile_reports', '[]'::jsonb)
                            ) AS elem
                        ), '[]'::jsonb)
                    )
                    WHERE progress->'profile_reports' IS NOT NULL
                      AND progress::text LIKE %s
                """, (sb_id, json.dumps(cl_id), f"%{sb_id}%"))
        conn.commit()
        print("  JSONB patching done.")
    except Exception as exc:
        conn.rollback()
        print(f"\n  [ERROR] Database remap failed, rolled back: {exc}")
        raise
    finally:
        conn.close()
                                                                             
                                               
                                                                             
def send_password_reset_emails(mapping: dict[str, str]) -> None:
    """
    Triggers a 'forgot password' email for every migrated user so they can
    set a password and log in. You can skip this if you want to send a custom
    email instead.
    """
    answer = input("\n[4/4] Send password-reset emails to all migrated users? (y/N): ").strip().lower()
    if answer != "y":
        print("  Skipped. Users won't be able to log in until they reset their password.")
        print("  You can trigger resets later from the Clerk dashboard or re-run this section.")
        return
    print(f"  Sending resets for {len(mapping)} users...")
    sent   = 0
    failed = 0
    for sb_id, cl_id in mapping.items():
        try:
            resp = requests.post(
                f"{CLERK_API_BASE}/users/{cl_id}/forgot_password",
                headers=_clerk_headers(),
                timeout=10,
            )
            if resp.status_code in (200, 201):
                sent += 1
            else:
                print(f"  FAIL {cl_id}: {resp.status_code} {resp.text[:100]}")
                failed += 1
        except Exception as exc:
            print(f"  EXCEPTION {cl_id}: {exc}")
            failed += 1
        time.sleep(0.05)
    print(f"  Sent: {sent}, Failed: {failed}")
                                                                             
      
                                                                             
def main():
    print("=" * 60)
    print("  Supabase → Clerk user migration")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)
    validate_config()
                                   
    existing_map = load_existing_map()
                          
    supabase_users = fetch_supabase_users()
                     
    mapping = create_clerk_users(supabase_users, existing_map)
                                                                                  
    save_mapping(mapping)
                   
    remap_database(mapping)
                                    
    send_password_reset_emails(mapping)
    print("\n" + "=" * 60)
    print("  Migration complete!")
    print(f"  {len(mapping)} users mapped.")
    print(f"  Full mapping saved to: {MAPPING_CSV}")
    print("=" * 60)
if __name__ == "__main__":
    main()
