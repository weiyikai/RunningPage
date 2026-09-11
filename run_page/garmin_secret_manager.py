"""
Manage Garmin secrets with automatic renewal.

Credentials (email/password) are read from a local gitignored JSON file
(``.garmin_credentials`` at the repo root). Fresh secrets are cached in
``.garmin_secrets`` so we don't hit Garmin's rate-limited OAuth2 token-exchange
endpoint on every run.

Per account the flow is:

1. Reuse the cached secret while its oauth2 (access) token is still valid.
2. If the oauth2 token expired -> ``refresh_oauth2()`` with the oauth1 token;
   cache the result.
3. If that fails (oauth1 session dead) -> full ``garth.login(email, password)``;
   cache the result.

Note: if the Garmin account has MFA enabled, step 3 will prompt for a code and
cannot run fully unattended.
"""

import argparse
import json
import os
import time

import garth
import requests
from garth.exc import GarthHTTPError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CREDENTIALS_FILE = os.path.join(REPO_ROOT, ".garmin_credentials")
SECRETS_FILE = os.path.join(REPO_ROOT, ".garmin_secrets")

ACCOUNTS = ("cn", "global")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _configure_domain(account):
    # garth.cn needs ssl_verify off; also reset cleanly when switching accounts
    # within the same process (the garth client is a module-level singleton).
    if account == "cn":
        garth.configure(domain="garmin.cn", ssl_verify=False)
    else:
        garth.configure(domain="garmin.com", ssl_verify=True)


def load_credentials():
    creds = _load_json(CREDENTIALS_FILE, {})
    # CI (GitHub Actions) has no checked-in credentials file, so overlay the
    # same values from environment variables. Env takes precedence over file.
    env_map = {
        "cn": ("GARMIN_CN_EMAIL", "GARMIN_CN_PASSWORD"),
        "global": ("GARMIN_GLOBAL_EMAIL", "GARMIN_GLOBAL_PASSWORD"),
    }
    for account, (email_key, password_key) in env_map.items():
        email = os.environ.get(email_key)
        password = os.environ.get(password_key)
        if email or password:
            creds.setdefault(account, {})
            if email:
                creds[account]["email"] = email
            if password:
                creds[account]["password"] = password
    return creds


def load_cached_secrets():
    return _load_json(SECRETS_FILE, {})


def save_cached_secrets(secrets):
    _save_json(SECRETS_FILE, secrets)


def _http_status(exc):
    # garth wraps requests HTTPError in GarthHTTPError(.error); the refresh
    # path raises the raw requests.exceptions.HTTPError instead.
    inner = getattr(exc, "error", None)
    resp = getattr(inner, "response", None) if inner is not None else None
    if resp is None:
        resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def _login_with_retry(account, email, password, max_attempts=5):
    """garth.login with exponential backoff on HTTP 429 (Garmin rate limits
    the SSO login endpoint, especially from shared/datacenter IPs)."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return garth.login(email, password)
        except (GarthHTTPError, requests.exceptions.HTTPError) as e:
            last_exc = e
            if _http_status(e) != 429:
                raise
            if attempt >= max_attempts - 1:
                break
            wait = 60 * (attempt + 1)
            print(
                f"[garmin_secret_manager] login '{account}' rate-limited "
                f"(HTTP 429), retry in {wait}s "
                f"(attempt {attempt + 1}/{max_attempts})..."
            )
            time.sleep(wait)
    raise last_exc


def get_or_refresh_secret(account, email, password):
    """Return a valid secret string for ``account`` ("cn" or "global")."""
    _configure_domain(account)
    cache = load_cached_secrets()
    cached = cache.get(account)

    if cached:
        try:
            garth.client.loads(cached)
            if not garth.client.oauth2_token.expired:
                return cached
            garth.client.refresh_oauth2()
            fresh = garth.client.dumps()
            cache[account] = fresh
            save_cached_secrets(cache)
            print(f"[garmin_secret_manager] refreshed oauth2 token for '{account}'")
            return fresh
        except Exception as e:
            print(f"[garmin_secret_manager] cached secret for '{account}' unusable "
                  f"({e}), re-login")

    _configure_domain(account)
    _login_with_retry(account, email, password)
    fresh = garth.client.dumps()
    cache[account] = fresh
    save_cached_secrets(cache)
    print(f"[garmin_secret_manager] logged in and cached secret for '{account}'")
    return fresh


def get_secrets(credentials=None):
    """Return {"cn": ..., "global": ...}, renewing each secret as needed."""
    creds = credentials if credentials is not None else load_credentials()
    if not creds:
        raise SystemExit(
            f"No credentials found. Create {CREDENTIALS_FILE} with "
            f"{{'cn': {{'email': ..., 'password': ...}}, 'global': {{...}}}}"
        )
    result = {}
    for account in ACCOUNTS:
        c = creds.get(account) or {}
        email, password = c.get("email"), c.get("password")
        if not email or not password:
            raise SystemExit(
                f"Missing email/password for account '{account}' in {CREDENTIALS_FILE}"
            )
        result[account] = get_or_refresh_secret(account, email, password)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Refresh / renew Garmin secrets and print them (CN then Global)."
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force a full re-login (ignore cached secret)",
    )
    options = parser.parse_args()

    if options.refresh:
        # drop the cache so every account re-logs in
        cache = load_cached_secrets()
        for account in ACCOUNTS:
            cache.pop(account, None)
        save_cached_secrets(cache)

    secrets = get_secrets()
    print(secrets["cn"])
    print(secrets["global"])
