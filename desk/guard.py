"""The bot check in front of the preview's two public write paths: typing a text as a tenant and starting a live run.

A visitor passes a Turnstile check once, and the desk gives that browser a signed cookie. Without one, the two write paths refuse the
request. A browser that drops its cookie therefore has to pass the check again to get a new count, which is what keeps one source from
using up the day's shared limits. The page sends the check's answer here; the desk asks Turnstile whether it is genuine, whether it was
given on this desk's own address, and whether it was given for this check. The check is on only when both its keys are set."""
from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from collections import deque

import httpx

log = logging.getLogger("desk.guard")

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ACTION = "visit"                 # the name the page gives the check; an answer given for another action is refused
TRIES_PER_HOUR = 10              # checks one address may ask this process to verify in an hour
PASSES_PER_HOUR = 60             # cookies this process gives out in an hour, to anyone: the bound that holds whatever address a request claims
LIFETIME = 86400                 # the cookie lives as long as the daily limits do
ASK = "Pass the quick check first. Reload the page and try again."
UNSET = "The quick check is not set up on this copy, so texts and runs are closed."
SLOW_DOWN = "Too many tries. Wait a while and reload the page."


class Unreachable(Exception):
    """Turnstile did not answer. The visitor is told to try again, not that they failed."""


class Guard:
    def __init__(self, sitekey: str | None = None, secret: str | None = None, host: str | None = None, ask=None, key: bytes | None = None,
                 now=time.time):
        self.sitekey, self.secret, self.host = sitekey, secret, (host or "").lower() or None
        self.on = bool(sitekey and secret and self.host)
        # a check set up in part must close the paths, not open them: the host is the address an answer must have been given on, and the desk
        # never takes it from the request, which the client writes
        self.broken = bool(sitekey or secret or self.host) and not self.on
        self._ask = ask or self._siteverify
        # made when the process starts: a restart ends every cookie, as it ends the counts they belong to
        self._key = key or secrets.token_bytes(32)
        self._now = now
        self._tries: dict[str, deque[float]] = {}
        self._passes: deque[float] = deque()

    def _sig(self, ident: str, expires: str) -> str:
        return hmac.new(self._key, f"visitor:{ident}:{expires}".encode(), "sha256").hexdigest()[:32]

    def cookie(self) -> str:
        """A new cookie value: a random id, the time it stops counting and a signature over both, so nobody can make one the desk will
        accept or keep one past its day."""
        ident, expires = secrets.token_hex(16), str(int(self._now()) + LIFETIME)
        return f"{ident}.{expires}.{self._sig(ident, expires)}"

    def valid(self, value: str | None) -> str | None:
        """The id in a cookie this desk signed and has not yet let lapse, or None."""
        ident, _, rest = (value or "").partition(".")
        expires, _, sig = rest.partition(".")
        if not (re.fullmatch(r"[0-9a-f]{32}", ident) and re.fullmatch(r"\d{1,12}", expires)):
            return None
        if int(expires) <= self._now() or not hmac.compare_digest(sig.encode(), self._sig(ident, expires).encode()):
            return None
        return ident

    def allow(self, who: str) -> bool:
        """Whether a check may be asked for now: no more than TRIES_PER_HOUR from one address in an hour, and no more than PASSES_PER_HOUR
        cookies given in all. The address is whatever the front door reports, which a client may be able to choose, so the second bound is the
        one that holds. Only a pass counts toward it, so garbage tokens cannot use it up. The counts live in this process."""
        now = self._now()
        while self._passes and now - self._passes[0] > 3600:
            self._passes.popleft()
        if len(self._passes) >= PASSES_PER_HOUR:
            return False
        tries = self._tries.setdefault(who, deque())
        while tries and now - tries[0] > 3600:
            tries.popleft()
        if len(tries) >= TRIES_PER_HOUR:
            return False
        tries.append(now)
        if len(self._tries) > 5000:                 # forget the addresses that have been quiet for an hour
            for stale in [k for k, v in self._tries.items() if not v or now - v[-1] > 3600]:
                del self._tries[stale]
        return True

    def passed(self, token: str, address: str) -> bool:
        """Whether Turnstile says this answer is genuine, was given on the desk's own address and was given for the visit check. Raises
        Unreachable when Turnstile cannot say."""
        answer = self._ask(token, address)
        good = bool(answer.get("success")) and answer.get("action") == ACTION and (answer.get("hostname") or "").lower() == self.host
        log.info("bot check %s%s", "passed" if good else "refused",
                 "" if good else f": {answer.get('error-codes') or 'success'} on {answer.get('hostname')} for {answer.get('action')}")
        if good:
            self._passes.append(self._now())
        return good

    def _siteverify(self, token: str, address: str) -> dict:
        form = {"secret": self.secret, "response": token}
        if re.fullmatch(r"[0-9a-fA-F:.]{3,45}", address or ""):
            form["remoteip"] = address
        try:
            reply = httpx.post(SITEVERIFY, data=form, timeout=5.0)
            reply.raise_for_status()
            answer = reply.json()
            if not isinstance(answer, dict):
                raise ValueError("not an answer")
            return answer
        except (httpx.HTTPError, ValueError) as exc:
            raise Unreachable(type(exc).__name__) from exc
