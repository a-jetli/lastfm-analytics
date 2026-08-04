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
    # on boot, start the daily background refresh loop. nothing on shutdown.
    sync_service.start_scheduler()
    yield


app = FastAPI(title="Rotation", lifespan=lifespan)

# the deployed frontend is served by this app, same origin, no CORS needed. this
# only opens the API to a local static dev server (VS Code Live Server on :5500,
# say) so the page can be edited and reloaded there while still talking to this
# backend. any localhost port, nothing public.
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


# the frontend, plain files in app/static served by this same process. mounted
# last so it only answers URLs no API route claimed (html=True serves index.html
# at /). same origin as the API, so the page's fetch() calls need no CORS.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
