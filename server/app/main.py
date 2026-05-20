"""퀀트 플랫폼 API 서버."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import create_db_and_tables
from .routers import (auth, backtest, commands, market, portfolio,
                       settings as settings_router, strategies, sync)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield


app = FastAPI(title="퀀트 플랫폼 API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(strategies.router)
app.include_router(backtest.router)
app.include_router(sync.router)
app.include_router(commands.router)
app.include_router(market.router)
app.include_router(portfolio.router)
app.include_router(settings_router.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "quant-platform-api"}
