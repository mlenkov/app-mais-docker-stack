#!/usr/bin/env python3
"""Yandex OAuth forward_auth for Caddy.

Minimal, secure, zero-runtime-dependency auth proxy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import secrets
import socketserver
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
PROJECT = "yandex-auth"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

COOKIE_SESSION_PREFIX = "_ys_"
COOKIE_AUTH_PREFIX = "_ya_"
SESSION_TTL = 600  # 10 minutes for OAuth flow
AUTH_TTL = 604800  # 7 days

MAX_HEADER_SIZE = 4096
MAX_BODY_SIZE = 1024
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 10

DEFAULT_CONFIG_PATH = "/etc/auth/config.yml"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    listen: str = "0.0.0.0:4180"
    domain: str = ".mais.agency"
    cookie_secret: str = ""
    cookie_name: str = "_ya_auth"

    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_url: str = ""
    oauth_auth_url: str = "https://oauth.yandex.ru/authorize"
    oauth_token_url: str = "https://oauth.yandex.ru/token"
    oauth_userinfo_url: str = "https://login.yandex.ru/info"
    oauth_scopes: list = field(default_factory=lambda: ["login:email", "login:info"])

    allowed_emails: list = field(default_factory=list)
    allowed_emails_file: str = ""

    rate_limit_reqs: int = 20
    rate_limit_burst: int = 40
    rate_limit_cleanup: int = 60

    max_workers: int = 16


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    cfg = Config()

    if yaml and os.path.isfile(path):
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        srv = raw.get("server", {})
        cfg.listen = str(srv.get("listen", cfg.listen))
        cfg.domain = str(srv.get("domain", cfg.domain))
        cfg.cookie_secret = str(srv.get("cookie_secret", cfg.cookie_secret))
        cfg.cookie_name = str(srv.get("cookie_name", cfg.cookie_name))
        cfg.max_workers = int(srv.get("max_workers", cfg.max_workers))

        oa = raw.get("oauth", {})
        cfg.oauth_client_id = str(oa.get("client_id", cfg.oauth_client_id))
        cfg.oauth_client_secret = str(oa.get("client_secret", cfg.oauth_client_secret))
        cfg.oauth_redirect_url = str(oa.get("redirect_url", cfg.oauth_redirect_url))
        cfg.oauth_auth_url = str(oa.get("auth_url", cfg.oauth_auth_url))
        cfg.oauth_token_url = str(oa.get("token_url", cfg.oauth_token_url))
        cfg.oauth_userinfo_url = str(oa.get("userinfo_url", cfg.oauth_userinfo_url))
        cfg.oauth_scopes = oa.get("scopes", cfg.oauth_scopes)

        au = raw.get("auth", {})
        cfg.allowed_emails = au.get("allowed_emails", cfg.allowed_emails)
        cfg.allowed_emails_file = str(au.get("allowed_emails_file", cfg.allowed_emails_file))

        rl = raw.get("rate_limit", {})
        cfg.rate_limit_reqs = int(rl.get("requests_per_sec", cfg.rate_limit_reqs))
        cfg.rate_limit_burst = int(rl.get("burst", cfg.rate_limit_burst))
        cfg.rate_limit_cleanup = int(rl.get("cleanup_interval", cfg.rate_limit_cleanup))

    # env overrides
    cfg.oauth_client_id = os.environ.get("YANDEX_CLIENT_ID", cfg.oauth_client_id)
    cfg.oauth_client_secret = os.environ.get("YANDEX_CLIENT_SECRET", cfg.oauth_client_secret)
    cfg.oauth_redirect_url = os.environ.get("REDIRECT_URL", cfg.oauth_redirect_url)
    cfg.cookie_secret = os.environ.get("COOKIE_SECRET", cfg.cookie_secret)

    return cfg


def load_allowed_emails(cfg: Config) -> set:
    emails = set(cfg.allowed_emails)
    if cfg.allowed_emails_file and os.path.isfile(cfg.allowed_emails_file):
        try:
            if cfg.allowed_emails_file.endswith(".yml") or cfg.allowed_emails_file.endswith(".yaml"):
                if yaml:
                    with open(cfg.allowed_emails_file) as f:
                        data = yaml.safe_load(f) or {}
                    for e in data.get("allowed_emails", []):
                        emails.add(str(e).lower().strip())
            else:
                with open(cfg.allowed_emails_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            emails.add(line.lower())
        except OSError:
            pass
    return emails


# ---------------------------------------------------------------------------
# Cryptography helpers
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _sign(secret: bytes, payload: bytes) -> str:
    return _b64(hmac.new(secret, payload, hashlib.sha256).digest())


def hmac_verify(sig: str, secret: bytes, payload: bytes) -> bool:
    expected = _sign(secret, payload)
    return hmac.compare_digest(sig, expected)


def make_session_token(cookie_secret: bytes) -> tuple[str, str]:
    """Generate a session token for PKCE flow. Returns (cookie_value, code_verifier)."""
    code_verifier = _b64(secrets.token_bytes(48))
    nonce = secrets.token_hex(8)
    expires = int(time.time()) + SESSION_TTL
    payload = f"{code_verifier}:{nonce}:{expires}".encode()
    sig = _sign(cookie_secret, payload)
    cookie_val = f"{_b64(payload)}.{sig}"
    return cookie_val, code_verifier


def verify_session_token(val: str, cookie_secret: bytes) -> Optional[str]:
    """Verify session token and return code_verifier if valid."""
    try:
        parts = val.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        payload_bytes = _unb64(payload_b64)
        if not hmac_verify(sig, cookie_secret, payload_bytes):
            return None
        code_verifier, nonce, expires = payload_bytes.decode().split(":", 2)
        if time.time() > float(expires):
            return None
        return code_verifier
    except Exception:
        return None


def make_auth_token(email: str, cookie_secret: bytes) -> str:
    nonce = secrets.token_hex(8)
    expires = int(time.time()) + AUTH_TTL
    payload = f"{email}:{nonce}:{expires}".encode()
    sig = _sign(cookie_secret, payload)
    return f"{_b64(payload)}.{sig}"


def verify_auth_token(val: str, cookie_secret: bytes) -> Optional[str]:
    try:
        parts = val.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        payload_bytes = _unb64(payload_b64)
        if not hmac_verify(sig, cookie_secret, payload_bytes):
            return None
        email, nonce, expires = payload_bytes.decode().split(":", 2)
        if time.time() > float(expires):
            return None
        return email
    except Exception:
        return None


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding window token bucket per IP."""

    def __init__(self, rate: int, burst: int, cleanup: int = 60):
        self.rate = rate
        self.burst = burst
        self.buckets: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.last_cleanup = time.monotonic()
        self.cleanup_interval = cleanup

    def _cleanup(self):
        now = time.monotonic()
        if now - self.last_cleanup < self.cleanup_interval:
            return
        cutoff = now - 60
        self.buckets = {k: v for k, v in self.buckets.items() if v["ts"] > cutoff}
        self.last_cleanup = now

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        with self.lock:
            self._cleanup()
            bucket = self.buckets.get(ip)
            if bucket is None:
                self.buckets[ip] = {"tokens": self.burst - 1, "ts": now}
                return True
            elapsed = now - bucket["ts"]
            bucket["ts"] = now
            bucket["tokens"] = min(self.burst, bucket["tokens"] + elapsed * self.rate)
            if bucket["tokens"] < 1:
                return False
            bucket["tokens"] -= 1
            return True


# ---------------------------------------------------------------------------
# Yandex API helpers
# ---------------------------------------------------------------------------

CTX = ssl.create_default_context()


def yandex_request(url: str, data: Optional[dict] = None,
                   headers: Optional[dict] = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, context=CTX, timeout=READ_TIMEOUT) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class YandexAuthHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"YandexAuth/{VERSION}"

    # rate limiter shared across instances
    limiter: Optional[RateLimiter] = None
    cfg: Optional[Config] = None
    cookie_secret: bytes = b""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    # -- Helpers --

    def log_message(self, fmt, *args):
        log = logging.getLogger(PROJECT)
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        else:
            ip = self.client_address[0]
        try:
            return str(ipaddress.ip_address(ip))
        except ValueError:
            return "0.0.0.0"

    def _safe_path(self, url: str) -> str:
        if not url.startswith("/") or url.startswith("//"):
            return "/"
        return url

    def _get_cookie(self, name: str) -> Optional[str]:
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _set_cookie(self, name: str, value: str, path: str = "/",
                    max_age: Optional[int] = None, http_only: bool = True,
                    same_site: str = "Lax"):
        if max_age is None:
            max_age = AUTH_TTL
        parts = [
            f"{name}={value}",
            f"Path={path}",
            f"Max-Age={max_age}",
            "HttpOnly",
            "Secure",
            f"SameSite={same_site}",
        ]
        self.send_header("Set-Cookie", "; ".join(parts))

    def _del_cookie(self, name: str, path: str = "/"):
        parts = [
            f"{name}=",
            f"Path={path}",
            "Max-Age=0",
            "HttpOnly",
            "Secure",
            "SameSite=Lax",
        ]
        self.send_header("Set-Cookie", "; ".join(parts))

    def _redirect(self, url: str):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def _text(self, code: int, body: str, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body.encode())))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body.encode())

    def _json(self, code: int, data: dict):
        body = json.dumps(data, separators=(",", ":")) + "\n"
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def _too_many_requests(self):
        self._text(429, "429 Too Many Requests\n")

    def _deny(self, reason: str = "Forbidden"):
        self._text(403, reason)

    # -- Rate limit check --

    def _check_rate(self, key: Optional[str] = None) -> bool:
        if key is None:
            key = self._client_ip()
        if self.limiter and not self.limiter.allow(key):
            log = logging.getLogger(PROJECT)
            log.warning("Rate limit exceeded for key=%s %s", key, self.path)
            self._too_many_requests()
            return False
        return True

    # -- Request dispatch --

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        dispatch = {
            "/auth": lambda: self._handle_auth(),
            "/start": lambda: self._do_yandex_redirect(self._safe_path(qs.get("rd", ["/"])[0])),
            "/callback": lambda: self._handle_callback(qs),
            "/oauth2/callback": lambda: self._handle_callback(qs),
            "/oauth/callback": lambda: self._handle_callback(qs),
            "/logout": lambda: self._handle_logout(),
            "/ping": lambda: self._text(200, "OK"),
            "/check": lambda: self._handle_check(),
            "/health": lambda: self._text(200, "OK"),
            "/": lambda: self._text(200, "yandex-auth/" + VERSION),
        }
        handler = dispatch.get(path)
        if handler:
            handler()
        else:
            self._text(404, "Not Found")

    # -- Forward auth (Caddy) --

    def _handle_auth(self):
        log = logging.getLogger(PROJECT)
        cookie_val = self._get_cookie(self.cfg.cookie_name)
        raw = self.headers.get("Cookie", "")
        if cookie_val:
            log.info("[auth] FOUND cookie, token[0:40]=%s, raw_len=%d",
                     cookie_val[:40], len(raw))
            email = verify_auth_token(cookie_val, self.cookie_secret)
            if email:
                if not self._check_rate(key=email):
                    return
                log.info("[auth] VALID token for %s", email)
                allowed = load_allowed_emails(self.cfg)
                if allowed and email.lower() not in allowed:
                    log = logging.getLogger(PROJECT)
                    log.warning("Unauthorized email: %s", email)
                    self._del_cookie(self.cfg.cookie_name)
                    self._deny(f"Email {email} is not authorized")
                    return

                local = email.split("@")[0] if "@" in email else email
                self.send_response(200)
                self.send_header("X-Auth-Request-User", local)
                self.send_header("X-Forwarded-User", local)
                self.send_header("X-Auth-Request-Email", email)
                self.send_header("X-Forwarded-Email", email)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.end_headers()
                return
            else:
                log.warning("[auth] FAILED verification for %s, raw[0:200]=%s",
                            self.cfg.cookie_name, raw[:200])

        if not self._check_rate(key="__anon__"):
            return
        log.info("[auth] NO valid cookie (found=%s), raw[0:200]=%s",
                 bool(cookie_val), raw[:200])
        original_uri = self.headers.get("X-Forwarded-Uri", "/")
        self._do_yandex_redirect(self._safe_path(original_uri), delete_auth_cookie=True)

    def _handle_check(self):
        log = logging.getLogger(PROJECT)
        cookie_val = self._get_cookie(self.cfg.cookie_name)
        raw = self.headers.get("Cookie", "")
        result = {
            "cookie_present": bool(cookie_val),
            "cookie_name": self.cfg.cookie_name,
            "raw_cookie_header_prefix": raw[:200],
        }
        if cookie_val:
            email = verify_auth_token(cookie_val, self.cookie_secret)
            result["token_valid"] = bool(email)
            result["email"] = email or None
            if not email:
                log.warning("[check] Invalid token for %s, raw[0:200]=%s",
                            self.cfg.cookie_name, raw[:200])
        self._json(200, result)

    def _do_yandex_redirect(self, return_to: str, delete_auth_cookie: bool = False):
        """302 redirect to Yandex OAuth with PKCE."""
        if not self._check_rate():
            return
        log = logging.getLogger(PROJECT)
        log.info("[redirect] Deleting auth=%s return_to=%s", delete_auth_cookie, return_to)
        session_val, code_verifier = make_session_token(self.cookie_secret)
        challenge = pkce_challenge(code_verifier)
        state = _b64(secrets.token_bytes(16))

        params = {
            "response_type": "code",
            "client_id": self.cfg.oauth_client_id,
            "redirect_uri": self.cfg.oauth_redirect_url,
            "state": state,
            "scope": " ".join(self.cfg.oauth_scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{self.cfg.oauth_auth_url}?{urllib.parse.urlencode(params)}"

        rd_safe = self._safe_path(return_to)
        self.send_response(302)
        self.send_header("Content-Length", "0")
        if delete_auth_cookie:
            self._del_cookie(self.cfg.cookie_name)
        self._set_cookie(COOKIE_SESSION_PREFIX + "pkce", session_val,
                         max_age=SESSION_TTL, path="/oauth2/callback")
        self._set_cookie(COOKIE_SESSION_PREFIX + "rd", _b64(rd_safe.encode()),
                         max_age=SESSION_TTL, path="/oauth2/callback")
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-cache, no-store")
        self.end_headers()

    # -- OAuth callback --

    def _handle_callback(self, qs: dict):
        if not self._check_rate():
            return
        if "error" in qs:
            log = logging.getLogger(PROJECT)
            log.warning("OAuth error: %s", qs.get("error", ["unknown"])[0])
            self._text(403, "Login failed")
            return

        code = qs.get("code", [None])[0]
        if not code:
            self._text(400, "Missing authorization code")
            return

        session_val = self._get_cookie(COOKIE_SESSION_PREFIX + "pkce")
        rd_b64 = self._get_cookie(COOKIE_SESSION_PREFIX + "rd")
        rd = "/"
        if rd_b64:
            try:
                rd = _unb64(rd_b64).decode()
                if not rd.startswith("/") or rd.startswith("//"):
                    rd = "/"
            except Exception:
                rd = "/"

        if not session_val:
            self._deny("Session expired, please login again")
            return

        code_verifier = verify_session_token(session_val, self.cookie_secret)
        if not code_verifier:
            self._deny("Session expired, please login again")
            return

        try:
            token_data = yandex_request(self.cfg.oauth_token_url, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.cfg.oauth_client_id,
                "client_secret": self.cfg.oauth_client_secret,
                "code_verifier": code_verifier,
            })
            access_token = token_data.get("access_token")
            if not access_token:
                raise ValueError("No access_token in token response")

            userinfo = yandex_request(
                self.cfg.oauth_userinfo_url,
                headers={"Authorization": f"OAuth {access_token}"},
            )
            email = userinfo.get("default_email") or ""
            if not email:
                emails_list = userinfo.get("emails", [])
                email = emails_list[0] if emails_list else ""

            if not email:
                login = userinfo.get("login", "")
                email = f"{login}@yandex.ru" if login else ""
                if not email:
                    raise ValueError("Could not determine user email")

            allowed = load_allowed_emails(self.cfg)
            if allowed and email.lower() not in allowed:
                log = logging.getLogger(PROJECT)
                log.warning("Unauthorized email attempt: %s", email)
                self._text(403, f"Email {email} is not authorized")
                return

            auth_val = make_auth_token(email, self.cookie_secret)
            log = logging.getLogger(PROJECT)
            log.info("[callback] Auth success for %s, rd=%s, cookie[0:40]=%s",
                      email, rd, auth_val[:40])
            self.send_response(302)
            self._del_cookie(COOKIE_SESSION_PREFIX + "pkce", path="/oauth2/callback")
            self._del_cookie(COOKIE_SESSION_PREFIX + "rd", path="/oauth2/callback")
            self.send_header("Content-Length", "0")
            self._set_cookie(self.cfg.cookie_name, auth_val)
            self.send_header("Location", rd)
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()

        except Exception as e:
            log = logging.getLogger(PROJECT)
            log.error("Callback error: %s", str(e)[:200])
            self._text(500, "Authentication failed")

    # -- Logout --

    def _handle_logout(self):
        if not self._check_rate():
            return
        rd = self._safe_path(self.headers.get("Referer", "/"))
        self.send_response(302)
        self._del_cookie(self.cfg.cookie_name)
        self.send_header("Location", rd)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # set socket timeouts
        self.socket.settimeout(CONNECT_TIMEOUT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    cfg = load_config(config_path)

    # validate
    errors = []
    if not cfg.oauth_client_id:
        errors.append("oauth.client_id is required (set YANDEX_CLIENT_ID or config)")
    if not cfg.oauth_client_secret:
        errors.append("oauth.client_secret is required (set YANDEX_CLIENT_SECRET or config)")
    if not cfg.oauth_redirect_url:
        errors.append("oauth.redirect_url is required (set REDIRECT_URL or config)")
    if not cfg.cookie_secret:
        errors.append("cookie_secret is required (set COOKIE_SECRET or config)")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    cookie_secret = cfg.cookie_secret.encode()

    # rate limiter
    limiter = RateLimiter(
        rate=cfg.rate_limit_reqs,
        burst=cfg.rate_limit_burst,
        cleanup=cfg.rate_limit_cleanup,
    )

    # inject shared state into handler class
    YandexAuthHandler.limiter = limiter
    YandexAuthHandler.cfg = cfg
    YandexAuthHandler.cookie_secret = cookie_secret

    host, port_str = cfg.listen.rsplit(":", 1)
    port = int(port_str)

    if not yaml:
        log.warning("PyYAML not available. Config file support disabled, use env vars.")

    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
    log = logging.getLogger(PROJECT)

    server = ThreadedHTTPServer((host, port), YandexAuthHandler)

    # set socket options
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    log.info("Starting yandex-auth on %s:%d", host, port)

    # Log startup config (sanitized)
    domain_safe = cfg.domain[:80] if cfg.domain else "(none)"
    cb_safe = cfg.oauth_redirect_url[:80] if cfg.oauth_redirect_url else "(not set)"
    log.info("domain=%s callback=%s workers=%d ratelimit=%d/s burst=%d",
             domain_safe, cb_safe, cfg.max_workers, cfg.rate_limit_reqs, cfg.rate_limit_burst)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    log = logging.getLogger(PROJECT)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    run()
