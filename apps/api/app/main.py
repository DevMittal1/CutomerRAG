import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.config import settings
from app.db import connect_to_mongo, close_mongo_connection, get_db
from app.logging_config import setup_logging, get_logger, request_id_var
from app.routers.auth import router as auth_router
from app.routers.chat import router as chat_router
from app.routers.documents import router as documents_router
from app.services.rag_chat import rag_chat_service

from app.rate_limiter import global_rate_limiter

# Initialize structured console logging
setup_logging()
logger = get_logger("app.main")

# System route constants to ensure a single source of truth and satisfy static analysis
DOCS_URL = "/docs"
REDOC_URL = "/redoc"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manages the application startup and shutdown lifecycles.
    Guarantees database connections are opened and closed cleanly.
    """
    logger.info("Initializing application startup sequence...")
    # Establish connection pool to MongoDB
    await connect_to_mongo()
    yield
    # Terminate connection pool gracefully
    logger.info("Initiating application shutdown sequence...")
    await rag_chat_service.close()
    await close_mongo_connection()


# Create main FastAPI application instance
app = FastAPI(
    title="Production-Grade RAG Backend",
    description="Secure, production-grade authentication and S3 pre-signed upload tracking system.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=DOCS_URL,
    redoc_url=REDOC_URL,
)

# Apply CORS middleware with settings configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- MIDDLEWARES ---


@app.middleware("http")
async def request_lifecycle_middleware(request: Request, call_next):
    """
    HTTP Middleware that runs for every incoming request.
    1. Generates and propagates a unique Request ID (Correlation ID) across thread contexts.
    2. Enforces global sliding-window rate limiting on all paths except exempt paths.
    3. Measures and logs request latency.
    4. Attaches Request ID and rate-limiting quotas to the outbound response headers.
    """
    start_time = time.time()

    # 1. Generate request ID (check if client provided one, otherwise generate)
    client_req_id = request.headers.get("X-Request-ID")
    request_id = client_req_id if client_req_id else uuid.uuid4().hex

    # Set the request ID in context-local storage for loggers
    token = request_id_var.set(request_id)

    logger.info(
        f"Incoming Request: {request.method} {request.url.path} from client {request.client.host if request.client else 'unknown'}"
    )

    # 2. Rate limit evaluation (exempting infrastructure endpoints like health and docs)
    path = request.url.path
    exempt_paths = {"/health", DOCS_URL, REDOC_URL, "/openapi.json"}
    is_exempt = path in exempt_paths or path.startswith((DOCS_URL, REDOC_URL))

    if not is_exempt:
        client_ip = request.client.host if request.client else "unknown"

        is_limited, remaining, retry_after = await global_rate_limiter.is_rate_limited(
            client_ip
        )

        request.state.rate_limit_remaining = remaining
        request.state.rate_limit_limit = settings.RATE_LIMIT_REQUESTS

        if is_limited:
            logger.warning(
                f"Rate limit exceeded for client {client_ip} on path {path}. Retry after {retry_after}s"
            )
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "success": False,
                    "error": {
                        "code": 429,
                        "message": "Rate limit exceeded. Please cool down before retrying.",
                    },
                },
                headers={"Retry-After": str(retry_after), "X-Request-ID": request_id},
            )
            request_id_var.reset(token)
            return response

    # 3. Proceed with executing request lifecycle
    try:
        response = await call_next(request)
    except Exception as e:
        # Fallback in case of middleware crashes
        logger.exception(f"Request execution crashed: {e}", exc_info=True)
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": {"code": 500, "message": "An unhandled exception occurred."},
            },
        )
    finally:
        # Calculate latency
        latency_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"Completed Request: {request.method} {request.url.path} - Status: {response.status_code} in {latency_ms}ms"
        )

        # 3. Inject tracing header into response
        response.headers["X-Request-ID"] = request_id

        # 4. Inject rate-limiting headers if set in request state
        if hasattr(request.state, "rate_limit_remaining"):
            response.headers["X-RateLimit-Limit"] = str(request.state.rate_limit_limit)
            response.headers["X-RateLimit-Remaining"] = str(
                request.state.rate_limit_remaining
            )

        # Reset the context variable
        request_id_var.reset(token)

    return response


# --- CUSTOM ERROR HANDLERS ---


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Custom exception handler for Standard HTTP Exceptions.
    """
    logger.warning(f"HTTP Error: {exc.detail} (Status Code: {exc.status_code})")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {"code": exc.status_code, "message": exc.detail},
        },
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom exception handler for Input Validation errors (Pydantic / Request parameter validation).
    Formats error pathways cleanly without exposing internal system files.
    """
    errors = []
    for err in exc.errors():
        # Format field locations nicely (e.g. body -> email)
        loc = " -> ".join(str(p) for p in err["loc"])
        errors.append({"field": loc, "issue": err["msg"]})

    logger.warning(f"Request validation failed: {errors}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "error": {
                "code": 422,
                "message": "Input validation failed. Please check payload constraints.",
                "details": errors,
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Global catch-all exception handler to avoid leaking core database or system stack traces.
    Logs full exception with stack traces securely inside files/consoles.
    """
    logger.critical(f"Unhandled server exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": {
                "code": 500,
                "message": "An unexpected error occurred. Our engineering team has been notified.",
            },
        },
    )


# --- HEALTH AND CORE ROUTES ---


@app.get("/health", tags=["Health"], summary="Service health check")
async def health_check():
    """
    Validates api readiness and tests connection to MongoDB.
    """
    status_details = {"api": "healthy", "mongodb": "unhealthy"}
    try:
        db = get_db()
        await db.command("ping")
        status_details["mongodb"] = "healthy"
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "healthy", "details": status_details},
        )
    except Exception as e:
        logger.exception(f"Health check failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "details": status_details},
        )


# Aggregate API router endpoints under /api/v1 prefix
app.include_router(auth_router, prefix="/api/v1")
app.include_router(documents_router, prefix="/api/v1")
app.include_router(chat_router, prefix="/api/v1")
