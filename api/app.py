"""Versioned FastAPI transport. Authentication runs before routing/body parsing."""
import asyncio
from contextlib import asynccontextmanager
import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.contract import ACCOUNT_MAX, LIFECYCLE_MAX, JOB_ID, validate_accounts, validate_lifecycle
from api.jobs import Conflict, Jobs
from api.security import authenticated
from api.worlds import catalogue

LOG = logging.getLogger('civicrm_bridge_api')


def error(status, code):
    return JSONResponse({'version': 1, 'error': {'code': code}}, status_code=status,
                        headers={'Cache-Control': 'no-store', **({'WWW-Authenticate': 'Bearer'} if status == 401 else {})})


def create_app(config, runner=None, background=True):
    @asynccontextmanager
    async def lifespan(app):
        jobs = Jobs(config, runner)
        app.state.jobs = jobs
        stop = asyncio.Event()

        async def consume(tick):
            while not stop.is_set():
                try:
                    await asyncio.to_thread(tick)
                except Exception:
                    # Submitted passwords and external exception text stay out of logs.
                    LOG.error('Job processing failed; inspect private state and service permissions.')
                try:
                    await asyncio.wait_for(stop.wait(), timeout=.25)
                except asyncio.TimeoutError:
                    pass

        tasks = [asyncio.create_task(consume(jobs.tick_accounts)),
                 asyncio.create_task(consume(jobs.tick_lifecycle))] if background else []
        try:
            yield
        finally:
            stop.set()
            # Do not close SQLite while a lifecycle thread is completing.
            await asyncio.gather(*tasks)
            jobs.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)

    @app.middleware('http')
    async def authorize(request: Request, call_next):
        if not authenticated(config['token_file'], request.headers.getlist('authorization')):
            return error(401, 'unauthorized')
        try:
            response = await call_next(request)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except (OSError, sqlite3.Error):
            return error(503, 'state_unavailable')

    async def submit(request, kind, world):
        if kind == 'accounts' and world not in config['worlds']:
            return error(404, 'unknown_world')
        content_types = request.headers.getlist('content-type')
        if len(content_types) != 1 or content_types[0].split(';', 1)[0].strip().lower() != 'application/json':
            return error(415, 'unsupported_media_type')
        if request.headers.get('content-encoding', 'identity').lower() != 'identity':
            return error(415, 'unsupported_encoding')
        maximum = ACCOUNT_MAX if kind == 'accounts' else LIFECYCLE_MAX
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > maximum:
                return error(413, 'request_too_large')
            raw.extend(chunk)
        raw = bytes(raw)
        try:
            value = (validate_accounts if kind == 'accounts' else validate_lifecycle)(raw)
        except (ValueError, UnicodeError, TypeError, RecursionError):
            return error(400, 'invalid_request')
        try:
            result = await asyncio.to_thread(app.state.jobs.accept, kind, world, value['job_id'], raw)
        except Conflict:
            return error(409, 'job_id_conflict')
        status = 202 if result['state'] == 'pending' else 200
        return JSONResponse(result, status_code=status, headers={'Location': result['result_url']})

    async def retrieve(kind, world, job_id):
        if (kind == 'accounts' and world not in config['worlds']) or not JOB_ID.fullmatch(job_id):
            return error(404, 'unknown_job')
        value = await asyncio.to_thread(app.state.jobs.get, kind, world, job_id)
        return JSONResponse(value) if value else error(404, 'unknown_job')

    @app.get('/v1/worlds')
    async def list_worlds():
        try:
            value = await asyncio.to_thread(catalogue, config)
        except (OSError, ValueError, TypeError):
            return error(503, 'world_catalogue_unavailable')
        return JSONResponse(value)

    @app.post('/v1/worlds/{world_id}/accounts/jobs')
    async def submit_accounts(world_id: str, request: Request):
        return await submit(request, 'accounts', world_id)

    @app.get('/v1/worlds/{world_id}/accounts/jobs/{job_id}')
    async def get_accounts(world_id: str, job_id: str):
        return await retrieve('accounts', world_id, job_id)

    @app.post('/v1/lifecycle/jobs')
    async def submit_lifecycle(request: Request):
        return await submit(request, 'lifecycle', '')

    @app.get('/v1/lifecycle/jobs/{job_id}')
    async def get_lifecycle(job_id: str):
        return await retrieve('lifecycle', '', job_id)

    return app
