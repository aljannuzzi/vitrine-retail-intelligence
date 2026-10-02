import base64
from collections import defaultdict, deque
import hashlib
import hmac
import json
import secrets
import time

COOKIE = "__Host-vitrine"
SESSION_SECONDS = 8 * 60 * 60


class Sessions:
    def __init__(self, settings):
        self.password_hash = hashlib.sha256(settings.app_password.encode()).digest()
        self.key = hmac.new(
            (settings.session_secret or settings.app_password).encode(),
            b"vitrine/session/v1\0" + self.password_hash, hashlib.sha256).digest()

    def password_valid(self, password):
        return hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.password_hash)

    def issue(self, now=None):
        now = time.time() if now is None else now
        body = base64.urlsafe_b64encode(json.dumps(
            {"exp": int(now + SESSION_SECONDS), "iat": int(now), "nonce": secrets.token_hex(16)},
            separators=(",", ":")).encode()).decode().rstrip("=")
        return body + "." + hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def valid(self, token, now=None):
        now = time.time() if now is None else now
        if not token or len(token) > 512:
            return False
        try:
            body, signature = token.split(".")
            expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            return payload["iat"] <= now < payload["exp"] <= payload["iat"] + SESSION_SECONDS
        except (ValueError, KeyError, TypeError):
            return False


class LoginBudget:
    def __init__(self):
        self.attempts = defaultdict(deque)
        self.global_attempts = deque()

    def allow(self, key, now):
        for queue in (self.global_attempts, self.attempts[key]):
            while queue and queue[0] <= now - 60:
                queue.popleft()
        if len(self.global_attempts) >= 30 or len(self.attempts[key]) >= 8:
            return False
        self.global_attempts.append(now)
        self.attempts[key].append(now)
        if len(self.attempts) > 1000:
            self.attempts = defaultdict(deque, {k: q for k, q in self.attempts.items()
                                                if q and q[-1] > now - 60})
        return True
