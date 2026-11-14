"""An agent behind an HTTPS address, so an agent we did not write can be checked the same way as our own.

The contract, all JSON:
- request (POST): `run_id`; `tenant`: the tenant's FMS identifiers (`contact_id`, `unit_id`, `subscription_id`) with `verified: true`,
  because every scenario here starts after the tenant has been identified; `message`: the tenant's words. Nothing else: never the
  FMS state, the expected answer, the checks or the judge's credentials.
- response: `run_id` (the same one), `reply` (the text sent to the tenant), optional `actions` (what the agent says it did).
The agent reads the FMS with its own, read-only access; it never gets ours.

Limits on our side: https only, no redirects, a name that resolves only to public internet addresses (or one listed in
FMS_CHECKS_AGENT_HOSTS). Each call resolves the name once, checks every address, and connects to the checked address itself, with the
name kept for the certificate check and the Host header, so nothing (the token included) can reach an address that was not checked.
One call per run, a timeout and a size cap on the reply.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .agents import Reply, Session

TIMEOUT, MAX_BYTES = 20.0, 20_000


class Refused(Exception):
    """The address or the answer broke the contract; the run records it as an error, never as a verdict."""


def _resolve(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]
    except socket.gaierror:
        return []


def _allowlist(allowed: set[str] | None) -> set[str]:
    return allowed if allowed is not None else {h.strip() for h in os.environ.get("FMS_CHECKS_AGENT_HOSTS", "").split(",") if h.strip()}


def check_address(url: str, allowed: set[str] | None = None, resolve=_resolve) -> str:
    """The address to connect to: the first of the name's addresses, after every one has been checked."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise Refused("the agent's address must be a plain https address")
    addresses = resolve(parts.hostname)
    if not addresses:
        raise Refused("the agent's name does not resolve")
    if parts.hostname not in _allowlist(allowed) and not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise Refused("the agent's address does not resolve only to public internet addresses")
    return addresses[0]


def _peer(response: httpx.Response) -> str | None:
    """The address the connection actually reached, when the transport tells us."""
    stream = response.extensions.get("network_stream")
    addr = stream.get_extra_info("server_addr") if stream is not None else None
    return addr[0] if isinstance(addr, tuple) and addr else None


class HttpAgent:
    def __init__(self, url: str, token: str, name: str = "outside agent", http: httpx.Client | None = None, allowed: set[str] | None = None,
                 resolve=_resolve):
        self.allowed, self.resolve = allowed, resolve
        check_address(url, allowed, resolve)
        self.url, self.token, self.name = url, token, name
        self.host = urlsplit(url).hostname
        self.http = http or httpx.Client(timeout=TIMEOUT, follow_redirects=False)

    def reply(self, message: str, session: Session) -> Reply:
        address = check_address(self.url, self.allowed, self.resolve)      # resolved and checked now, then connected to as is
        parts = urlsplit(self.url)
        pinned = parts._replace(netloc=(f"[{address}]" if ":" in address else address) + (f":{parts.port}" if parts.port else "")).geturl()
        run_id = uuid.uuid4().hex
        body = {"run_id": run_id, "message": message,
                "tenant": {"contact_id": session.contact_id, "unit_id": session.unit_id, "subscription_id": session.subscription_id,
                           "verified": True}}
        with self.http.stream("POST", pinned, json=body, headers={"authorization": f"Bearer {self.token}", "host": parts.netloc},
                              extensions={"sni_hostname": self.host}, follow_redirects=False) as r:
            peer = _peer(r)                                # a second guard: the connection must have reached a public address
            if peer is not None and self.host not in _allowlist(self.allowed) and not ipaddress.ip_address(peer).is_global:
                raise Refused("the connection reached an address that is not on the public internet")
            if r.is_redirect or r.status_code != 200:
                raise Refused(f"the agent answered HTTP {r.status_code}")
            raw, began = b"", time.monotonic()
            for chunk in r.iter_bytes():
                raw += chunk
                if len(raw) > MAX_BYTES:
                    raise Refused(f"the agent's answer is over {MAX_BYTES} bytes")
                if time.monotonic() - began > TIMEOUT:          # each wait has its own timeout; this bounds the whole answer
                    raise Refused(f"the agent's answer took longer than {TIMEOUT:.0f} seconds")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise Refused("the agent's answer is not JSON") from exc
        if not isinstance(data, dict) or data.get("run_id") != run_id or not isinstance(data.get("reply"), str):
            raise Refused("the agent's answer does not carry this run's id and a reply")
        actions = data.get("actions") or []
        return Reply(data["reply"], [str(a)[:300] for a in actions][:20] if isinstance(actions, list) else [])
