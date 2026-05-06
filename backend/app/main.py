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


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request, exc: RequestValidationError):
    logger.warning("Request validation failed: %s", exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Request validation failed.",
            "errors": exc.errors(),
        },
    )


app.include_router(generate.router, prefix="/api")
app.include_router(import_assistant.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Backend is running locally with Grok integration enabled."}
