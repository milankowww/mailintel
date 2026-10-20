# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
from contextvars import ContextVar

from ..connectors.registry import ConnectorRegistry
from ..db import connect_database, init_database
from ..repositories import Repository
from ..secrets import PasswordEncryptor
from ..services.chat import ChatService
from ..services.dashboard import DashboardService
from ..services.llm import LLMService, HTTPTransport, llm_pause_controller
from ..services.work_queue import WorkQueueRuntime
from ..services.pipeline import PipelineRuntime
from ..services.smart_search import SmartSearchRuntime
from ..settings import get_settings
from ..audit import audit_event
from ..auth import verify_bearer_token
from ..auth.token import PUBLIC_API_PATHS
from ..api.dependencies import AppContext
from ..api.routers import register_routes


class RepositoryProxy:
    def __init__(self, connection_factory):
        self._connection_factory = connection_factory
        self._scope = ContextVar('database_scope', default=None)

    @contextmanager
    def connection_scope(self):
        if self._scope.get() is not None:
            yield self
            return
        scope = {}
        token = self._scope.set(scope)
        try:
            yield self
        finally:
            self._scope.reset(token)
            if 'repository' in scope:
                scope['repository'].connection.close()

    def _with_repository(self, callback):
        scope = self._scope.get()
        if scope is not None:
            if 'repository' not in scope:
                scope['repository'] = Repository(self._connection_factory())
            return callback(scope['repository'])
        connection = self._connection_factory()
        repository = Repository(connection)
        try:
            return callback(repository)
        finally:
            connection.close()

    def commit(self):
        bound = (self._scope.get() or {}).get('repository')
        if bound is not None:
            bound.commit()

    def rollback(self):
        bound = (self._scope.get() or {}).get('repository')
        if bound is not None:
            bound.rollback()

    @contextmanager
    def transaction(self):
        with self.connection_scope():
            repository = self._with_repository(lambda repository: repository)
            with repository.transaction():
                yield self

    def __getattr__(self, name: str):
        def proxy_method(*args, **kwargs):
            return self._with_repository(lambda repository: getattr(repository, name)(*args, **kwargs))

        return proxy_method


def create_app():
    try:
        from contextlib import asynccontextmanager
        from fastapi import Request
        from fastapi import FastAPI
        from starlette.middleware.cors import CORSMiddleware
        from starlette.responses import JSONResponse
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("fastapi must be installed to create the API app") from exc

    settings = get_settings()
    llm_pause_controller.set_paused(settings.llm_paused)
    password_encryptor = PasswordEncryptor(settings.encryption_key_file)
    connection = connect_database(settings.database_url)
    init_database(connection)
    startup_repository = Repository(connection)
    app_started_at = datetime.now(UTC)
    startup_repository.discard_unfinished_smart_searches()
    startup_repository.mark_interrupted_jobs(app_started_at)
    startup_repository.remove_rule_based_summaries()
    connection.close()
    repository = RepositoryProxy(lambda: connect_database(settings.database_url))
    
    llm_service = LLMService(transport=HTTPTransport(timeout_seconds=settings.llm_provider_timeout_seconds))
    
    connector_registry = ConnectorRegistry()
    dashboard_service = DashboardService(repository)
    chat_service = ChatService(repository, settings, llm_service=llm_service)
    pipeline_runtime = PipelineRuntime(settings, password_encryptor, connector_registry, llm_service=llm_service)
    smart_search_runtime = SmartSearchRuntime(settings, llm_service)
    work_queue_runtime = WorkQueueRuntime(settings, llm_service)
    pipeline_runtime.register_queue_handlers(work_queue_runtime)
    smart_search_runtime.register_queue_handlers(work_queue_runtime)
    context = AppContext(
        settings=settings,
        repository=repository,
        password_encryptor=password_encryptor,
        connector_registry=connector_registry,
        dashboard_service=dashboard_service,
        chat_service=chat_service,
        llm_service=llm_service,
        pipeline_runtime=pipeline_runtime,
        smart_search_runtime=smart_search_runtime,
        work_queue_runtime=work_queue_runtime,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        work_queue_runtime.start()
        try:
            pipeline_runtime.start()
            smart_search_runtime.start()
            yield
        finally:
            work_queue_runtime.stop()
            smart_search_runtime.stop()
            pipeline_runtime.stop()

    app = FastAPI(title="MailIntel", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
            if not verify_bearer_token(settings, request.headers.get("Authorization")):
                audit_event("auth.failed", path=request.url.path, method=request.method)
                return JSONResponse({"detail": "Missing or invalid bearer token"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/search"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def close_database_transaction(_request, call_next):
        with repository.connection_scope():
            try:
                response = await call_next(_request)
            except BaseException:
                repository.rollback()
                raise
            if response.status_code >= 400:
                repository.rollback()
            else:
                repository.commit()
            return response

    register_routes(app, context)
    return app
