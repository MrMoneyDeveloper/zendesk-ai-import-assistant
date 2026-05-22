from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.settings import get_settings
from app.loggers.logger import configure_logger, get_logger
from app.middlewares.request_context import RequestContextMiddleware
from app.routes import generate, import_assistant

settings = get_settings()
configure_logger()
logger = get_logger(__name__)

app = FastAPI(title=settings.app_name)

allowed_origins = list(
    {
        settings.frontend_origin.rstrip("/"),
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)


def _format_validation_errors(exc: RequestValidationError) -> tuple[list[dict[str, object]], str]:
    raw_errors = exc.errors()
    formatted: list[dict[str, object]] = []
    snippets: list[str] = []

    for item in raw_errors:
        loc = item.get("loc") if isinstance(item, dict) else ()
        message = str(item.get("msg") or "Invalid value.") if isinstance(item, dict) else "Invalid value."
        error_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        input_value = item.get("input") if isinstance(item, dict) else None

        path_parts = [str(part) for part in loc if str(part) != "body"]
        path = ".".join(path_parts) if path_parts else "request"

        formatted.append(
            {
                "path": path,
                "message": message,
                "type": error_type,
                "input": input_value,
            }
        )

        if len(snippets) < 3:
            snippets.append(f"{path}: {message}")

    summary = "; ".join(snippets) if snippets else "Invalid request payload."
    return formatted, summary


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request, exc: RequestValidationError):
    validation_errors, summary = _format_validation_errors(exc)
    logger.warning("Request validation failed: %s", validation_errors)
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "failure_stage": "request",
                "failure_code": "request_validation_failed",
                "failure_reason": f"Request validation failed. {summary}",
                "next_step": "Adjust the invalid fields shown in validation_errors and retry.",
                "validation_errors": validation_errors,
            },
            "errors": validation_errors,
        },
    )


app.include_router(generate.router, prefix="/api")
app.include_router(import_assistant.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Backend is running locally with Grok integration enabled."}
