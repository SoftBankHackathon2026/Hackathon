from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from controlplane.api import access_pin
from controlplane.api.errors import register_exception_handlers
from controlplane.api.router import api_router
from controlplane.container import Container, build_container
from controlplane.core import db
from controlplane.core.config import AuthMode, HttpSettings
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.releases import ReleaseStore

DB_POOL_SIZE = 24


def _lifespan(container: Container):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with db.pooling(max_size=DB_POOL_SIZE):
            await run_in_threadpool(container.prepare)
            yield

    return lifespan


def _add_middleware(app: FastAPI, http: HttpSettings) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=http.allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=http.allowed_hosts)


def create_app(
    *,
    registry: ProviderRegistry | None = None,
    store: ReleaseStore | None = None,
    operator_token: str | None = None,
    http_settings: HttpSettings | None = None,
    auth_mode: AuthMode | str | None = None,
) -> FastAPI:
    container = build_container(
        registry=registry,
        store=store,
        operator_token=operator_token,
        http_settings=http_settings,
        auth_mode=auth_mode,
    )
    app = FastAPI(
        title="Oneport · Deployment Platform", version="0.1.0", lifespan=_lifespan(container)
    )
    app.state.container = container
    _add_middleware(app, container.http)
    register_exception_handlers(app)
    app.include_router(api_router)
    access_pin.install(app, access_pin.gate_from_environment())
    return app


app = create_app()
