import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from running_drafts.settings import Settings

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_ISSUER = "https://accounts.google.com"
OIDC_SCOPE = "openid email profile"


class OIDCError(RuntimeError):
    """A safe, classified OIDC failure without provider response data."""


class OIDCCancelled(OIDCError):
    pass


class OIDCTemporaryFailure(OIDCError):
    pass


class OIDCInvalidResponse(OIDCError):
    pass


@dataclass(frozen=True)
class ProviderClaims:
    provider: str
    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool | None = None
    display_name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    picture_url: str | None = None
    locale: str | None = None
    microsoft_tenant_id: str | None = None


class OIDCProvider(Protocol):
    name: str
    display_name: str

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str: ...

    async def authenticate(
        self,
        *,
        callback_parameters: dict[str, str],
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> ProviderClaims: ...


class ProviderRegistry:
    def __init__(self, providers: tuple[OIDCProvider, ...] = ()) -> None:
        self._providers = {provider.name: provider for provider in providers}

    def get(self, name: str) -> OIDCProvider | None:
        return self._providers.get(name)

    def configured(self) -> tuple[OIDCProvider, ...]:
        return tuple(self._providers.values())


class GoogleOIDCProvider:
    name = "google"
    display_name = "Google"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    async def _metadata(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.get(GOOGLE_DISCOVERY_URL)
                response.raise_for_status()
                metadata = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise OIDCTemporaryFailure("provider metadata is unavailable") from error
        required = ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer")
        if not all(isinstance(metadata.get(key), str) for key in required):
            raise OIDCInvalidResponse("provider metadata is invalid")
        if metadata["issuer"] != GOOGLE_ISSUER:
            raise OIDCInvalidResponse("provider issuer is invalid")
        return metadata

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        metadata = await self._metadata()
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": OIDC_SCOPE,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"

    async def authenticate(
        self,
        *,
        callback_parameters: dict[str, str],
        nonce: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> ProviderClaims:
        if callback_parameters.get("error") == "access_denied":
            raise OIDCCancelled("authentication was cancelled")
        if callback_parameters.get("error"):
            raise OIDCInvalidResponse("provider rejected authentication")
        code = callback_parameters.get("code")
        if not code:
            raise OIDCInvalidResponse("authorization code is missing")

        metadata = await self._metadata()
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                token_response = await client.post(
                    metadata["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "redirect_uri": redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
                if token_response.status_code >= 500 or token_response.status_code == 429:
                    raise OIDCTemporaryFailure("provider token endpoint is unavailable")
                if token_response.status_code >= 400:
                    raise OIDCInvalidResponse("provider rejected the authorization code")
                token_data = token_response.json()
                id_token = token_data.get("id_token")
                if not isinstance(id_token, str):
                    raise OIDCInvalidResponse("provider omitted the ID token")
                jwks_response = await client.get(metadata["jwks_uri"])
                jwks_response.raise_for_status()
                key_set = jwks_response.json()
        except OIDCError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise OIDCTemporaryFailure("provider validation data is unavailable") from error

        try:
            token = jwt.decode(
                id_token,
                KeySet.import_key_set(key_set),
                algorithms=["RS256"],
            )
            claims = token.claims
            jwt.JWTClaimsRegistry(
                leeway=60,
                iss={"essential": True, "value": metadata["issuer"]},
                aud={"essential": True, "value": self._client_id},
                exp={"essential": True},
                iat={"essential": True},
                sub={"essential": True},
            ).validate(
                claims
            )
        except (JoseError, KeyError, TypeError, ValueError) as error:
            raise OIDCInvalidResponse("ID token validation failed") from error

        returned_nonce = claims.get("nonce")
        if not isinstance(returned_nonce, str) or not secrets.compare_digest(
            returned_nonce, nonce
        ):
            raise OIDCInvalidResponse("ID token nonce validation failed")
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1:
            authorized_party = claims.get("azp")
            if not isinstance(authorized_party, str) or not secrets.compare_digest(
                authorized_party, self._client_id
            ):
                raise OIDCInvalidResponse("ID token authorized party validation failed")
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not subject or not isinstance(issuer, str):
            raise OIDCInvalidResponse("ID token identity is invalid")
        return ProviderClaims(
            provider=self.name,
            issuer=issuer,
            subject=subject,
            email=_optional_string(claims.get("email")),
            email_verified=_optional_bool(claims.get("email_verified")),
            display_name=_optional_string(claims.get("name")),
            given_name=_optional_string(claims.get("given_name")),
            family_name=_optional_string(claims.get("family_name")),
            picture_url=_optional_string(claims.get("picture")),
            locale=_optional_string(claims.get("locale")),
        )


def create_provider_registry(settings: Settings) -> ProviderRegistry:
    providers: list[OIDCProvider] = []
    if settings.google_client_id and settings.google_client_secret:
        providers.append(
            GoogleOIDCProvider(
                settings.google_client_id,
                settings.google_client_secret.get_secret_value(),
            )
        )
    return ProviderRegistry(tuple(providers))


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(48)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
