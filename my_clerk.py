import time
import requests
import jwt
from jwt import PyJWKClient
from functools import lru_cache
from config import CLERK_SECRET_KEY, CLERK_JWKS_URL
CLERK_API_BASE = "https://api.clerk.com/v1"
                                                                             
                                                                
                                                                             
@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
def verify_clerk_token(token: str) -> str | None:
    """
    Verify a Clerk session JWT and return the Clerk user ID (subject claim).
    Returns None if the token is invalid or expired.
    """
    if not token:
        return None
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
                                                           
        return payload.get("sub")
    except Exception:
        return None
                                                                             
                           
                                                                             
def _headers() -> dict:
    return {
        "Authorization": f"Bearer {CLERK_SECRET_KEY}",
        "Content-Type":  "application/json",
    }
def get_clerk_user(user_id: str) -> dict | None:
    """Fetch a user object from the Clerk backend API."""
    try:
        resp = requests.get(f"{CLERK_API_BASE}/users/{user_id}", headers=_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception:
        return None
def delete_clerk_user(user_id: str) -> bool:
    """Hard-delete a user from Clerk."""
    try:
        resp = requests.delete(f"{CLERK_API_BASE}/users/{user_id}", headers=_headers(), timeout=10)
        return resp.status_code in (200, 204)
    except Exception:
        return False
def update_clerk_user_password(user_id: str, new_password: str) -> bool:
    """Update a user's password via the Clerk backend API."""
    try:
        resp = requests.patch(
            f"{CLERK_API_BASE}/users/{user_id}",
            headers=_headers(),
            json={"password": new_password},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False
def list_clerk_users(limit: int = 500, offset: int = 0) -> list[dict]:
    """Return a page of users from Clerk."""
    try:
        resp = requests.get(
            f"{CLERK_API_BASE}/users",
            headers=_headers(),
            params={"limit": limit, "offset": offset},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception:
        return []
def count_clerk_users() -> int:
    """Return the total number of users in Clerk."""
    try:
        resp = requests.get(
            f"{CLERK_API_BASE}/users/count",
            headers=_headers(),
            timeout=10,
        )
        if resp.status_code == 200:
            return int(resp.json().get("total_count", 0))
        return 0
    except Exception:
        return 0
def create_clerk_user(
    email: str,
    password_digest: str | None = None,
    password_hasher: str | None = None,
    external_id: str | None = None,
    first_name: str | None = None,
    skip_password_checks: bool = False,
) -> dict | None:
    """
    Create a user in Clerk. Used during migration.
    - password_digest / password_hasher: for importing existing bcrypt hashes.
    - external_id: store the old Supabase UUID here so you can remap records.
    - skip_password_checks: set True when importing hashed passwords.
    """
    payload: dict = {
        "email_address": [email],
        "skip_password_checks": skip_password_checks,
    }
    if external_id:
        payload["external_id"] = external_id
    if first_name:
        payload["first_name"] = first_name
    if password_digest and password_hasher:
        payload["password_digest"] = password_digest
        payload["password_hasher"] = password_hasher
    try:
        resp = requests.post(
            f"{CLERK_API_BASE}/users",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        print(f"[CLERK] create_user failed {resp.status_code}: {resp.text[:300]}")
        return None
    except Exception as exc:
        print(f"[CLERK] create_user exception: {exc}")
        return None
