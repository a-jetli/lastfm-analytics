"""FastAPI entry point.

Run locally:  uvicorn app.main:app --reload   then open /docs.

Layering: routers/* = HTTP (no SQL), queries/* = SQL (no HTTP), lastfm.py =
the only file that calls Last.fm, sync_service.py = when/how syncs and the
periodic enrichment run, recommender.py = pure recommendation math.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import sync_service
from app.routers import analytics, sync


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On boot: start the daily background refresh loop. Nothing on shutdown.
    sync_service.start_scheduler()
    yield


app = FastAPI(title="Rotation", lifespan=lifespan)

# The deployed frontend is served by this app (same origin, no CORS needed).
# This only opens the API to a LOCAL static dev server -- e.g. VS Code Live
# Server on :5500 -- so the page can be edited/reloaded there while still
# talking to this backend. Any localhost port; nothing public is allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sync.router)
app.include_router(analytics.router)


@app.get("/health")
def health():
    return {"status": "ok"}


# The frontend: plain files in app/static, served by this same process. Mounted
# last so it only answers URLs no API route claimed (html=True serves index.html
# at /). Same origin as the API, so the page's fetch() calls need no CORS.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
