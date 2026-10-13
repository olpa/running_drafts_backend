from typing import Any

from fastapi.responses import JSONResponse


class ProblemException(Exception):
    def __init__(
        self,
        *,
        status: int,
        title: str,
        detail: str,
        code: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        self.headers = headers


def problem_response(
    *,
    status: int,
    title: str,
    detail: str,
    code: str,
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    if extensions:
        body.update(extensions)
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )
