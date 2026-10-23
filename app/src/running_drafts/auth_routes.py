import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse, Response

from running_drafts.application_sessions import (
    end_application_session,
    replace_application_session,
    resolve_application_session,
)
from running_drafts.auth import SESSION_COOKIE_NAME, authenticated_user_id
from running_drafts.models import CamelCaseResponse
from running_drafts.oauth import (
    OIDCCancelled,
    OIDCInvalidResponse,
    OIDCTemporaryFailure,
    ProviderRegistry,
    pkce_challenge,
)
from running_drafts.oauth_identities import get_account_identity, resolve_or_provision_identity
from running_drafts.oauth_transactions import (
    LOGIN_ATTEMPT_LIFETIME_MS,
    consume_login_attempt,
    create_login_attempt,
    delete_login_attempt,
)
from running_drafts.problems import ProblemException, problem_response
from running_drafts.session_cookie import clear_session_cookie, set_session_cookie
from running_drafts.settings import Settings

logger = logging.getLogger(__name__)
router = APIRouter()
OAUTH_STATE_COOKIE_NAME = "running_drafts_oidc_state"
STATE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ProviderSummary(CamelCaseResponse):
    id: str
    display_name: str


class ProvidersResponse(CamelCaseResponse):
    providers: list[ProviderSummary]


class AccountSummary(CamelCaseResponse):
    id: UUID


class ProfileSummary(CamelCaseResponse):
    email: str | None
    email_verified: bool | None
    display_name: str | None
    given_name: str | None
    family_name: str | None
    picture_url: str | None
    locale: str | None


class CurrentSessionResponse(CamelCaseResponse):
    account: AccountSummary
    provider: ProviderSummary
    profile: ProfileSummary
    session_expires_at: datetime


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _providers(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def _safe_return_path(candidate: str | None) -> str:
    if candidate is None:
        return "/projects"
    parsed = urlsplit(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in candidate
        or any(ord(character) < 0x20 for character in candidate)
    ):
        raise ProblemException(
            status=400,
            title="Invalid return path",
            detail="The return path must be a local application path.",
            code="invalid_return_path",
        )
    return candidate


def _redirect_uri(request: Request, provider: str, settings: Settings) -> str:
    if provider == "google" and settings.google_redirect_uri:
        return settings.google_redirect_uri
    return str(request.url_for("oidc_callback", provider=provider))


def _editor_redirect(settings: Settings, path: str) -> str:
    return f"{settings.public_editor_url}{path}"


def _error_redirect(settings: Settings, error: str) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.public_editor_url}/login?{urlencode({'authError': error})}",
        status_code=303,
    )


def _unexpected_error_redirect(
    settings: Settings,
    return_to: str,
    reference_id: str,
) -> RedirectResponse:
    parsed = urlsplit(return_to)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"authError", "referenceId"}
    ]
    query_items.extend(
        (
            ("authError", "authentication_failed"),
            ("referenceId", reference_id),
        )
    )
    canonical_return_to = urlunsplit(
        ("", "", parsed.path, urlencode(query_items), "")
    )
    return RedirectResponse(
        _editor_redirect(settings, canonical_return_to),
        status_code=303,
    )


def _log_unexpected_authentication_failure(
    provider: str,
    stage: str,
    reference_id: str,
) -> None:
    logger.error(
        "unexpected OIDC authentication failure",
        extra={
            "provider": provider,
            "stage": stage,
            "category": "unexpected_exception",
            "referenceId": reference_id,
        },
    )


def _clear_state_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        path="/auth/callback",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _delete_cookie_login_attempt(settings: Settings, cookie_state: str | None) -> None:
    if cookie_state is not None and STATE_PATTERN.fullmatch(cookie_state) is not None:
        delete_login_attempt(settings, cookie_state)


@router.get("/api/v1/auth/providers", response_model=ProvidersResponse)
def configured_providers(request: Request) -> ProvidersResponse:
    return ProvidersResponse(
        providers=[
            ProviderSummary(id=provider.name, display_name=provider.display_name)
            for provider in _providers(request).configured()
        ]
    )


@router.get("/auth/login/{provider}")
async def oidc_login(
    provider: str,
    request: Request,
    return_to: str | None = Query(default=None),
) -> RedirectResponse:
    settings = _settings(request)
    adapter = _providers(request).get(provider)
    if adapter is None:
        raise ProblemException(
            status=404,
            title="Provider not found",
            detail="The requested authentication provider is not available.",
            code="provider_not_found",
        )
    if resolve_application_session(settings, request.cookies.get(SESSION_COOKIE_NAME)) is not None:
        raise ProblemException(
            status=409,
            title="Already authenticated",
            detail="Log out before signing in with another account.",
            code="already_authenticated",
        )
    attempt = create_login_attempt(settings, provider, _safe_return_path(return_to))
    try:
        location = await adapter.authorization_url(
            state=attempt.state,
            nonce=attempt.nonce,
            code_challenge=pkce_challenge(attempt.code_verifier),
            redirect_uri=_redirect_uri(request, provider, settings),
        )
    except OIDCTemporaryFailure:
        delete_login_attempt(settings, attempt.state)
        logger.warning("OIDC login start failed provider=%s category=temporary", provider)
        response = _error_redirect(settings, "provider_unavailable")
        _clear_state_cookie(response, settings)
        return response
    except OIDCInvalidResponse:
        delete_login_attempt(settings, attempt.state)
        logger.warning("OIDC login start failed provider=%s category=invalid", provider)
        response = _error_redirect(settings, "authentication_failed")
        _clear_state_cookie(response, settings)
        return response
    except Exception:
        delete_login_attempt(settings, attempt.state)
        reference_id = str(uuid4())
        _log_unexpected_authentication_failure(
            provider,
            "authorization_request",
            reference_id,
        )
        response = _unexpected_error_redirect(
            settings,
            attempt.return_to,
            reference_id,
        )
        _clear_state_cookie(response, settings)
        return response
    response = RedirectResponse(location, status_code=303)
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        attempt.state,
        max_age=LOGIN_ATTEMPT_LIFETIME_MS // 1_000,
        path="/auth/callback",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/auth/callback/{provider}", name="oidc_callback")
async def oidc_callback(provider: str, request: Request) -> Response:
    settings = _settings(request)
    adapter = _providers(request).get(provider)
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    if adapter is None:
        _delete_cookie_login_attempt(settings, cookie_state)
        response = problem_response(
            status=404,
            title="Provider not found",
            detail="The requested authentication provider is not available.",
            code="provider_not_found",
        )
        _clear_state_cookie(response, settings)
        return response
    states = request.query_params.getlist("state")
    if (
        len(states) != 1
        or cookie_state is None
        or STATE_PATTERN.fullmatch(states[0]) is None
        or not secrets.compare_digest(states[0], cookie_state)
    ):
        _delete_cookie_login_attempt(settings, cookie_state)
        response = _error_redirect(settings, "authentication_failed")
        _clear_state_cookie(response, settings)
        return response
    attempt = consume_login_attempt(settings, states[0])
    if attempt is None or attempt.provider != provider:
        response = _error_redirect(settings, "authentication_failed")
        _clear_state_cookie(response, settings)
        return response
    codes = request.query_params.getlist("code")
    errors = request.query_params.getlist("error")
    if not ((len(codes) == 1 and not errors) or (len(errors) == 1 and not codes)):
        response = _error_redirect(settings, "authentication_failed")
        _clear_state_cookie(response, settings)
        return response
    try:
        claims = await adapter.authenticate(
            callback_parameters=dict(request.query_params),
            nonce=attempt.nonce,
            code_verifier=attempt.code_verifier,
            redirect_uri=_redirect_uri(request, provider, settings),
        )
    except OIDCCancelled:
        logger.info("OIDC callback returned provider=%s outcome=cancelled", provider)
        response = _error_redirect(settings, "cancelled")
    except OIDCTemporaryFailure:
        logger.warning("OIDC callback failed provider=%s category=temporary", provider)
        response = _error_redirect(settings, "provider_unavailable")
    except OIDCInvalidResponse:
        logger.warning("OIDC callback failed provider=%s category=invalid", provider)
        response = _error_redirect(settings, "authentication_failed")
    except Exception:
        reference_id = str(uuid4())
        _log_unexpected_authentication_failure(
            provider,
            "provider_authentication",
            reference_id,
        )
        response = _unexpected_error_redirect(settings, attempt.return_to, reference_id)
    else:
        try:
            identity = resolve_or_provision_identity(settings, claims)
        except Exception:
            reference_id = str(uuid4())
            _log_unexpected_authentication_failure(
                provider,
                "identity_provisioning",
                reference_id,
            )
            response = _unexpected_error_redirect(settings, attempt.return_to, reference_id)
        else:
            try:
                issued = replace_application_session(
                    settings,
                    identity.account_id,
                    request.cookies.get(SESSION_COOKIE_NAME),
                )
            except Exception:
                reference_id = str(uuid4())
                _log_unexpected_authentication_failure(
                    provider,
                    "session_establishment",
                    reference_id,
                )
                response = _unexpected_error_redirect(settings, attempt.return_to, reference_id)
            else:
                expires = datetime.fromtimestamp(issued.expires_at_ms / 1_000, tz=UTC)
                response = RedirectResponse(
                    _editor_redirect(settings, attempt.return_to),
                    status_code=303,
                )
                set_session_cookie(response, settings, issued.token, expires)
                logger.info(
                    "OIDC login completed provider=%s account_id=%s",
                    provider,
                    identity.account_id,
                )
    _clear_state_cookie(response, settings)
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    settings = _settings(request)
    end_application_session(settings, request.cookies.get(SESSION_COOKIE_NAME))
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.get("/api/v1/me", response_model=CurrentSessionResponse)
def current_session(
    request: Request,
    account_id: Annotated[UUID, Depends(authenticated_user_id)],
) -> CurrentSessionResponse:
    settings = _settings(request)
    session = resolve_application_session(settings, request.cookies.get(SESSION_COOKIE_NAME))
    if session is None or session.account_id != account_id:
        raise ProblemException(
            status=401,
            title="Authentication required",
            detail="Valid credentials are required.",
            code="authentication_required",
            headers={"WWW-Authenticate": "Session"},
        )
    identity = get_account_identity(settings, account_id)
    claims = identity.claims
    return CurrentSessionResponse(
        account=AccountSummary(id=account_id),
        provider=ProviderSummary(id=claims.provider, display_name=claims.provider.title()),
        profile=ProfileSummary(
            email=claims.email,
            email_verified=claims.email_verified,
            display_name=claims.display_name,
            given_name=claims.given_name,
            family_name=claims.family_name,
            picture_url=claims.picture_url,
            locale=claims.locale,
        ),
        session_expires_at=datetime.fromtimestamp(session.expires_at_ms / 1_000, tz=UTC),
    )
