"""openfeed authorisation: discovery, PAR, code exchange and refresh.

Implements the openfeed "Recommended profile": PAR (mandatory),
private_key_jwt client authentication, PKCE S256 and DPoP-bound tokens.
Every endpoint is resolved from the discovery document; nothing but the
issuer is configured.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode

import aiohttp
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from .crypto import ASSERTION_TYPE, client_assertion, dpop_proof


class OpenfeedAuthError(Exception):
    """An OAuth error from the openfeed authorisation server."""

    def __init__(self, error: str, description: str | None = None, status: int = 0):
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description
        self.status = status

    @property
    def is_invalid_grant(self) -> bool:
        """True when the refresh token or code is no longer usable."""
        return self.error == "invalid_grant" or self.status == 401


@dataclass(slots=True)
class Discovery:
    """The subset of the discovery document the integration uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    par_endpoint: str
    jwks_uri: str
    revocation_endpoint: str | None = None

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> Discovery:
        """Parse a discovery document."""
        try:
            return cls(
                issuer=doc["issuer"],
                authorization_endpoint=doc["authorization_endpoint"],
                token_endpoint=doc["token_endpoint"],
                par_endpoint=doc["pushed_authorization_request_endpoint"],
                jwks_uri=doc["jwks_uri"],
                revocation_endpoint=doc.get("revocation_endpoint"),
            )
        except KeyError as err:
            raise OpenfeedAuthError(
                "invalid_discovery", f"discovery document lacks {err}"
            ) from err


@dataclass(slots=True)
class TokenSet:
    """Tokens and the disclosure grant they are bound to."""

    access_token: str
    refresh_token: str | None
    expires_at: float
    grant_id: str | None = None
    id_token: str | None = None
    scope: str | None = None
    token_type: str = "DPoP"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(
        cls,
        body: dict[str, Any],
        now: float,
        previous: TokenSet | None = None,
    ) -> TokenSet:
        """Build from a token endpoint response.

        The grant_id is in the response body under
        authorization_details[0].grant_id; access tokens are opaque and are
        never decoded. A refresh response may omit the refresh token, in
        which case the previous one stays valid.
        """
        details = body.get("authorization_details") or []
        grant_id = None
        if details and isinstance(details[0], dict):
            grant_id = details[0].get("grant_id")
        if grant_id is None and previous is not None:
            grant_id = previous.grant_id
        refresh = body.get("refresh_token") or (previous.refresh_token if previous else None)
        return cls(
            access_token=body["access_token"],
            refresh_token=refresh,
            expires_at=now + float(body.get("expires_in", 3600)),
            grant_id=grant_id,
            id_token=body.get("id_token"),
            scope=body.get("scope"),
            token_type=body.get("token_type", "DPoP"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the config entry. The id_token is not kept."""
        data = asdict(self)
        data.pop("id_token", None)
        data.pop("extra", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenSet:
        """Restore from the config entry."""
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data.get("expires_at", 0)),
            grant_id=data.get("grant_id"),
            scope=data.get("scope"),
            token_type=data.get("token_type", "DPoP"),
        )

    def expires_within(self, seconds: float, now: float | None = None) -> bool:
        """True when the access token expires within the given seconds."""
        return self.expires_at - seconds <= (now if now is not None else time.time())


class OpenfeedAuth:
    """Client for the openfeed authorisation server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        app_key: rsa.RSAPrivateKey,
        dpop_key: rsa.RSAPrivateKey,
        issuer: str,
        resource: str,
        clock: Any = time.time,
    ) -> None:
        self._session = session
        self.client_id = client_id
        self._app_key = app_key
        self.dpop_key = dpop_key
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        self._clock = clock
        self._discovery: Discovery | None = None
        # The most recent DPoP nonce per endpoint (RFC 9449 section 8).
        self.nonces: dict[str, str] = {}

    async def discover(self) -> Discovery:
        """Fetch and cache the discovery document."""
        if self._discovery is None:
            url = f"{self.issuer}/.well-known/openid-configuration"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    raise OpenfeedAuthError("discovery_failed", f"HTTP {resp.status}", resp.status)
                doc = await resp.json(content_type=None)
            disc = Discovery.from_document(doc)
            if disc.issuer.rstrip("/") != self.issuer:
                raise OpenfeedAuthError("issuer_mismatch", disc.issuer)
            self._discovery = disc
        return self._discovery

    def _client_auth(self) -> dict[str, str]:
        assert self._discovery is not None
        return {
            "client_id": self.client_id,
            "client_assertion_type": ASSERTION_TYPE,
            # aud must be the issuer value from discovery, not an endpoint.
            "client_assertion": client_assertion(
                self._app_key, self.client_id, self._discovery.issuer, self._clock()
            ),
        }

    async def push_authorization_request(
        self,
        *,
        redirect_uri: str,
        scopes: list[str],
        state: str,
        nonce: str,
        code_challenge: str,
        grant_management_action: str = "create",
        grant_id: str | None = None,
    ) -> str:
        """POST a pushed authorisation request and return its request_uri."""
        disc = await self.discover()
        form = {
            **self._client_auth(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "resource": self.resource,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
            "state": state,
            "grant_management_action": grant_management_action,
        }
        if grant_id is not None:
            form["grant_id"] = grant_id
        async with self._session.post(disc.par_endpoint, data=form) as resp:
            body = await _json_or_empty(resp)
            if resp.status not in (200, 201):
                raise _auth_error(body, resp.status)
        try:
            return body["request_uri"]
        except KeyError as err:
            raise OpenfeedAuthError("invalid_par_response", "no request_uri") from err

    def authorize_url(self, request_uri: str) -> str:
        """The browser URL for a pushed request: client_id and request_uri only."""
        assert self._discovery is not None
        query = urlencode({"client_id": self.client_id, "request_uri": request_uri})
        return f"{self._discovery.authorization_endpoint}?{query}"

    async def exchange_code(self, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        """Exchange an authorisation code for DPoP-bound tokens."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "resource": self.resource,
            }
        )

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        """Refresh the access token. The resource is re-sent every time."""
        if not tokens.refresh_token:
            raise OpenfeedAuthError("invalid_grant", "no refresh token held", 401)
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "resource": self.resource,
            },
            previous=tokens,
        )

    async def revoke(self, token: str, token_type_hint: str = "refresh_token") -> bool:
        """Revoke a token at openfeed (RFC 7009). False when not supported.

        Revoking an unknown or already revoked token succeeds (HTTP 200), so
        this is safe to call more than once.
        """
        disc = await self.discover()
        if not disc.revocation_endpoint:
            return False
        form = {**self._client_auth(), "token": token, "token_type_hint": token_type_hint}
        async with self._session.post(disc.revocation_endpoint, data=form) as resp:
            if resp.status != 200:
                raise _auth_error(await _json_or_empty(resp), resp.status)
        return True

    async def _token_request(
        self, form: dict[str, str], previous: TokenSet | None = None
    ) -> TokenSet:
        disc = await self.discover()
        url = disc.token_endpoint
        for attempt in range(2):
            proof = dpop_proof(
                self.dpop_key, "POST", url, nonce=self.nonces.get(url), now=self._clock()
            )
            async with self._session.post(
                url, data={**self._client_auth(), **form}, headers={"DPoP": proof}
            ) as resp:
                if nonce := resp.headers.get("DPoP-Nonce"):
                    self.nonces[url] = nonce
                body = await _json_or_empty(resp)
                if resp.status == 200:
                    return TokenSet.from_response(body, self._clock(), previous)
                err = _auth_error(body, resp.status)
                # Retry exactly once when the server demands a nonce.
                if err.error == "use_dpop_nonce" and attempt == 0 and nonce:
                    continue
                raise err
        raise OpenfeedAuthError("use_dpop_nonce", "nonce retry failed")

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        """Verify an id_token signature, iss, aud, exp and nonce."""
        disc = await self.discover()
        header = jwt.get_unverified_header(id_token)
        async with self._session.get(disc.jwks_uri) as resp:
            if resp.status != 200:
                raise OpenfeedAuthError("jwks_failed", f"HTTP {resp.status}", resp.status)
            jwks = await resp.json(content_type=None)
        keys = [k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")]
        if not keys:
            raise OpenfeedAuthError("invalid_id_token", "signing key not in JWKS")
        key = jwt.PyJWK(keys[0], algorithm="PS256")
        try:
            claims = jwt.decode(
                id_token,
                key.key,
                algorithms=["PS256"],
                audience=self.client_id,
                issuer=disc.issuer,
                leeway=60,
            )
        except jwt.PyJWTError as err:
            raise OpenfeedAuthError("invalid_id_token", str(err)) from err
        if claims.get("nonce") != nonce:
            raise OpenfeedAuthError("invalid_id_token", "nonce mismatch")
        return claims


async def _json_or_empty(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _auth_error(body: dict[str, Any], status: int) -> OpenfeedAuthError:
    return OpenfeedAuthError(
        str(body.get("error") or f"http_{status}"),
        body.get("error_description"),
        status,
    )
