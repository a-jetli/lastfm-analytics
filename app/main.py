"""app entry point. `uvicorn app.main:app --reload`, then /docs.

layers: routers/* http only, queries/* sql only, lastfm.py the only outbound
caller, sync_service.py decides when work runs, recommender.py is the math.
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
    # start the periodic refresh loop. nothing to do on shutdown.
    sync_service.start_scheduler()
    yield


app = FastAPI(title="Rotation", lifespan=lifespan)

# the deployed page is same-origin, so it needs no CORS. this exists only so a
# local dev server (Live Server on :5500) can talk to this backend while you
# edit. localhost only, nothing public.
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


# the page itself, served by this same process. mounted last so it only answers
# what no API route claimed. html=True serves index.html at /.
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
