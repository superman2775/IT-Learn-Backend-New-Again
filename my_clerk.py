import time
import requests
import jwt
import threading
from jwt import PyJWKClient
from config import CLERK_SECRET_KEY, CLERK_JWKS_URL

CLERK_API_BASE = "https://api.clerk.com/v1"

# Thread-safe storage for the last verification error
_verification_errors = threading.local()


def get_last_verification_error() -> str | None:
    """Return the last error from verify_clerk_token on this thread, or None."""
    return getattr(_verification_errors, 'last_error', None)


def get_last_verification_debug() -> dict:
    """Return debug info from the last verify_clerk_token call on this thread."""
    return {
        "error": getattr(_verification_errors, 'last_error', None),
        "jwks_url": getattr(_verification_errors, 'last_jwks_url', None),
        "issuer": getattr(_verification_errors, 'last_issuer', None),
    }


# Cache JWKS clients keyed by JWKS URL so different Clerk instances reuse clients
_jwks_clients: dict = {}
_jwks_clients_lock = threading.Lock()


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    with _jwks_clients_lock:
        if jwks_url not in _jwks_clients:
            _jwks_clients[jwks_url] = PyJWKClient(jwks_url, cache_keys=True)
        return _jwks_clients[jwks_url]


def _extract_jwks_url_from_token(token: str) -> str | None:
    """
    Decode the JWT without verification, extract the 'iss' (issuer) claim,
    and build the correct JWKS URL for that Clerk instance.
    Clerk's JWKS lives at: {issuer}/.well-known/jwks.json
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        print(f"[CLERK] Cannot decode token to extract issuer — {e}")
        return None
    iss = unverified.get("iss")
    if not iss:
        print("[CLERK] JWT missing 'iss' claim — not a valid Clerk token?")
        return None
    return f"{iss.rstrip('/')}/.well-known/jwks.json"


def verify_clerk_token(token: str) -> str | None:
    """
    Verify a Clerk session JWT and return the Clerk user ID (subject claim).
    Automatically discovers the correct JWKS URL from the token's issuer claim.
    Set the CLERK_JWKS_URL env var to override the auto-discovery.
    Returns None if the token is invalid or expired.
    Use get_last_verification_error() to see why verification failed.
    """
    if not token:
        _verification_errors.last_error = "empty token"
        print("[CLERK] verify_clerk_token: empty token")
        return None

    # Prefer auto-discovery from the token itself, fall back to configured URL
    discovered = _extract_jwks_url_from_token(token)
    jwks_url = discovered or CLERK_JWKS_URL
    _verification_errors.last_jwks_url = jwks_url
    if jwks_url:
        # Store the issuer for debugging
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            _verification_errors.last_issuer = unverified.get("iss")
        except Exception:
            _verification_errors.last_issuer = None
    if not jwks_url:
        _verification_errors.last_error = "could not determine JWKS URL from token"
        print("[CLERK] verify_clerk_token: could not determine JWKS URL")
        return None

    try:
        signing_key = _get_jwks_client(jwks_url).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        sub = payload.get("sub")
        if not sub:
            _verification_errors.last_error = "JWT payload missing 'sub' claim"
            print("[CLERK] verify_clerk_token: JWT payload missing 'sub' claim")
        return sub
    except jwt.ExpiredSignatureError:
        _verification_errors.last_error = "JWT token has expired"
        print("[CLERK] verify_clerk_token: JWT token has expired")
        return None
    except jwt.InvalidTokenError as e:
        _verification_errors.last_error = f"invalid JWT: {e}"
        print(f"[CLERK] verify_clerk_token: invalid token — {e}")
        return None
    except Exception as e:
        _verification_errors.last_error = f"{type(e).__name__}: {e}"
        print(f"[CLERK] verify_clerk_token: unexpected error — {type(e).__name__}: {e}")
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
