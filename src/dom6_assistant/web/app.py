"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    from dom6_assistant.journal.db import init_db
    init_db()

    from dom6_assistant.gamestate import service as ingest_service

    app = FastAPI(
        title="Dominions 6 Assistant",
        description="Web interface for reading dom6 game state and AI-assisted play.",
        version="0.1.0",
        # Turns are ingested while the server runs; forgetting to do it by hand
        # looked exactly like the assistant being wrong about the current turn.
        lifespan=ingest_service.lifespan,
    )

    # Static files (HTML/JS/CSS)
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    # API routers
    from dom6_assistant.web.routes.process   import router as process_router
    from dom6_assistant.web.routes.memory    import router as memory_router
    from dom6_assistant.web.routes.settings  import router as settings_router
    from dom6_assistant.web.routes.chat      import router as chat_router
    from dom6_assistant.web.routes.gamestate  import router as gamestate_router
    from dom6_assistant.web.routes.journal    import router as journal_router
    from dom6_assistant.web.routes.correlate  import router as correlate_router
    from dom6_assistant.web.routes.trndiff    import router as trndiff_router
    from dom6_assistant.web.routes.agent      import router as agent_router

    app.include_router(process_router,   prefix="/api")
    app.include_router(memory_router,    prefix="/api")
    app.include_router(settings_router,  prefix="/api")
    app.include_router(chat_router,      prefix="/api")
    app.include_router(gamestate_router, prefix="/api")
    app.include_router(journal_router,   prefix="/api")
    app.include_router(correlate_router, prefix="/api")
    app.include_router(trndiff_router,   prefix="/api")
    app.include_router(agent_router,     prefix="/api")
    app.include_router(ingest_service.router, prefix="/api")

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/agent", include_in_schema=False)
    def agent_page() -> FileResponse:
        """The assistant: chat with tools, orders, notes, lessons and gaps."""
        return FileResponse(_STATIC_DIR / "agent.html")

    @app.get("/verify", include_in_schema=False)
    def verification_page() -> FileResponse:
        """Model-free audit of the exact live information agent tools expose."""
        return FileResponse(_STATIC_DIR / "verify.html")

    return app
