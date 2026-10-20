import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from json.decoder import JSONDecodeError
from typing import Annotated

import arq
import asyncpg
import redis.exceptions
from arq.connections import RedisSettings
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from starlette.middleware.base import RequestResponseEndpoint

from .exceptions import (
    AppError,
    InvalidSignature,
    MalformedPayload,
    QueueError,
    StorageError,
)

load_dotenv()
secret = os.environ["WEBHOOK_SECRET"]

#configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s  - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

#Initiate context vars
request_id_var: ContextVar[str] = ContextVar('request_id_var')

#REDIS setup
REDIS_SETTINGS = RedisSettings.from_dsn(os.environ["REDIS_URL"])



#setup Basemodel for webhook payloads
#Add more data to models as features are added
class PullRequest(BaseModel):
    url: str
    id: int
    number: int

class Repository(BaseModel):
    id: int
    name: str
    html_url: str
    url: str
    full_name: str

class Sender(BaseModel):
    login: str
    id: int

class WebhookPayload(BaseModel):
    action: str
    number: int
    pull_request: PullRequest
    repository: Repository
    sender: Sender

class WebhookEvent(BaseModel):
    id: int
    action: str
    repo: str
    event_type: str
    received_at: datetime

class EventResponse(BaseModel):
    events: list[WebhookEvent]
    next_cursor: str | None


def validate_hmac(hmac_str: str, payload: bytes, secret: str) -> bool:
    actual = 'sha256=' + hmac.new(secret.encode(), payload, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, hmac_str)

def find_field_errors(errors: list[ErrorDetails]) -> list[str]:
    missing_fields = []
    for item in errors:
        field_error = f"Field(s): {'.'.join(str(x) for x in item['loc'])} -> Error Type: {item['type']} -> Message: {item['msg']}"
        missing_fields.append(field_error)
    return missing_fields

#Connection Pool for postgresql
#Lifespan is when the event loop starts running, since create_pool is async, its created here
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.postgres_pool = await asyncpg.create_pool(dsn=os.environ["DATABASE_URL"], command_timeout = 10)
    app.state.redis_pool = await arq.create_pool(REDIS_SETTINGS)
    try:
        yield
    finally:
        await app.state.postgres_pool.close()
        await app.state.redis_pool.close()



#create app
app = FastAPI(lifespan=lifespan)

def get_postgres_pool(request: Request) -> asyncpg.Pool:
    pool: asyncpg.Pool = request.app.state.postgres_pool #using request.app rather just app which allows dependency overrides
    return pool
def get_redis_pool(request: Request) -> arq.ArqRedis:
    pool: arq.ArqRedis = request.app.state.redis_pool #using request.app rather just app which allows dependency overrides
    return pool


@app.middleware("http")
async def add_request_id(request: Request, call_next: RequestResponseEndpoint) -> Response | None:
    headers = request.headers
    if "X-Request-ID" in headers:
        request_uuid = headers["X-Request-ID"]
    else:
        request_uuid = str(uuid.uuid4())
    request_id_var.set(request_uuid)
    response = None

    before_time = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers['X-Request-ID'] = request_uuid
    finally:
        total_time = time.perf_counter() - before_time
        if response is None:
            logger.error(json.dumps({"request_id": f"{request_uuid}", "method": f"{request.method}", "path": f"{request.url.path}", "status": 500, "duration_s": total_time}))
        else:
            message = json.dumps({"request_id": f"{request_uuid}", "method": f"{request.method}", "path": f"{request.url.path}", "status": response.status_code, "duration_s": total_time})
            if 399 >= response.status_code >= 200:
                logger.info(msg=message)
            elif 499 >= response.status_code >= 400:
                logger.warning(msg=message)
            elif 599 >= response.status_code >= 500:
                logger.error(msg=message)
    return response

@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    status = exc.status
    return JSONResponse(status_code=status, content={"request_id": request_id_var.get(), "error": str(exc)})

@app.post("/webhook")
async def create_item(
    request: Request,
    postgres_pool:asyncpg.Pool=Depends(get_postgres_pool), # noqa: B008
    redis_pool:arq.ArqRedis=Depends(get_redis_pool) ) -> JSONResponse: # noqa: B008
    raw_body = await request.body()
    signature = request.headers.get('x-hub-signature-256')
    delivery_id = request.headers.get('X-GitHub-Delivery')
    event_type = request.headers.get("X-GitHub-Event")
    request_id = request_id_var.get()
    if not signature:
        logger.warning(json.dumps({"request_id": f"{request_id}", "path": f"{request.url.path}", "message": "Request failed due to a missing HMAC."}))
        raise InvalidSignature(reason = "Authentication Error")
    valid_hmac = validate_hmac(signature, raw_body, secret=secret)

    if not valid_hmac:#verify signature
        logger.warning(json.dumps({"request_id": f"{request_id}", "message": "Request failed due to an Invalid Signature"}))
        raise InvalidSignature(reason = "Authentication Error")
    if not delivery_id:#check if deliv id exists
        logger.warning(json.dumps({"request_id": f"{request_id}", "path": f"{request.url.path}", "message": "Request failed due to a missing X-GitHub-Delivery header."}))
        raise MalformedPayload(reason = "Missing X-GitHub-Delivery header.")
    if not event_type:#check if event type exists
        logger.warning(json.dumps({"request_id": f"{request_id}", "path": f"{request.url.path}", "message": "Request failed due to a missing X-GitHub-Event header."}))
        raise MalformedPayload(reason = "Missing X-GitHub-Event header.")
    elif event_type not in ["pull_request", "ping"]:#ignore unhandled event types
        logger.info(json.dumps({"request_id": f"{request_id}", "message": f"Unhandled event type received ({event_type})."}))
        return JSONResponse(
            status_code=200,
            content={"message": "Event not handled."}
        )
    elif event_type == "ping":
        logger.info(json.dumps({"request_id": f"{request_id}", "message": "Ping received from github."}))
        return JSONResponse(
            status_code=200,
            content={"message": "Ping acknowledged."}
        )

    try:
        data = json.loads(raw_body)
        payload = WebhookPayload(**data)
        logger.info(json.dumps({"request_id": f"{request_id}", "message": f"Webhook number {payload.pull_request.id} for Pull Request {payload.number} successfully verified"}))
    #--------------------------POSTGRES INSERT--------------------------
        try:
            psql_response = await postgres_pool.execute(
                """INSERT INTO webhook_events
                (delivery_id, event_type, action, repo, pr_number, payload)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (delivery_id) DO NOTHING""",
                delivery_id,
                event_type,
                payload.action,
                payload.repository.full_name,
                payload.number,
                json.dumps(data)
            )
            if psql_response == "INSERT 0 1":
                logger.info(json.dumps({"request_id": f"{request_id}", "message": f"Row successfully inserted for delivery id {delivery_id}"}))
            elif psql_response == "INSERT 0 0":
                logger.info(json.dumps({"request_id": f"{request_id}", "message": f"Row not inserted for webhook number {payload.number} due do a duplicate delivery."}))
        except TimeoutError as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": "Database insert timed out"}))
            raise StorageError(reason= "Database insert timed out") from e
        except OSError as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": "Could not reach database"}))
            raise StorageError(reason= "Could not reach database") from e
        except asyncpg.PostgresError as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": f"Postgres error: {e}"}))
            raise StorageError(reason= "Postgres error") from e
    #--------------------Postgres Insert------------------------------------

    #--------------------REDIS ENQUE---------------------------------------
        try:
            await redis_pool.enqueue_job('fetch_diff', delivery_id, request_id , _job_id=delivery_id)
        except (OSError, redis.exceptions.ConnectionError) as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": "Could not reach redis"}))
            raise QueueError(reason= "Could not reach redis") from e
        except (TimeoutError, redis.exceptions.TimeoutError) as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": "Redis Enque timed out"}))
            raise QueueError(reason= "Redis Enque timed out") from e
        except redis.exceptions.RedisError as e:
            logger.error(json.dumps({"request_id": f"{request_id}", "message": f"Redis error: {e}"}))
            raise QueueError(reason= "Redis error") from e
    #--------------------REDIS ENQUE---------------------------------------

        return JSONResponse(
            status_code=200,
            content={"message": "Pull Request received and validated successfully."}
        )
    except ValidationError as e:
        fields = find_field_errors(e.errors())
        raise MalformedPayload(reason = f"Invalid Data Fields. Payload has an issue with the following fields: {', '.join(fields)}") from e
    except JSONDecodeError as e:
        raise MalformedPayload(reason = "Invalid Data Fields. Please ensure payload is sent in JSON format.") from e

@app.get("/events")
async def list_events(
    limit: Annotated[int, Query(title="Page Result Limit", ge=1, le=100)] = 10,
    cursor: str | None = None,
    repo: str | None = None,
    event_type: str | None = None,
    pool:asyncpg.Pool=Depends(get_postgres_pool) # noqa: B008
) -> EventResponse:
    try:
        query = "SELECT id, action, repo, event_type, received_at FROM webhook_events"
        conditions = []
        params: list[str | int] = []
        filters = {}

        if repo:
            filters["repo"] = repo
            params.append(repo)
            conditions.append(f"repo = ${len(params)}")
        if event_type:
            filters["event_type"] = event_type
            params.append(event_type)
            conditions.append(f"event_type = ${len(params)}")
        param_filters_hash = normalize_and_hash(filters)
        if cursor:
            try:
                cursor_id, filters_hash = decode_cursor(cursor=cursor)
            except (IndexError, ValueError, UnicodeDecodeError) as e: #Index error can be caught from cursor not having '|'
                logger.warning(json.dumps({"message": f"Could not decode cursor id due to {e}."}))
                raise MalformedPayload(reason = "Invalid Cursor Field.") from e

            #verify filters hash matches to the actual filters provided
            if param_filters_hash != filters_hash:
                logger.warning(json.dumps({"message": "Filter provided does not match the filters provided with the cursor."}))
                raise MalformedPayload(reason = "Filters changed since this cursor was issued. Restart paging without a cursor.")
            params.append(cursor_id)
            conditions.append(f"id < ${len(params)}") #descending id order

        if conditions != []:
            query += " WHERE " + " AND ".join(conditions)

        params.append(limit+1)
        query += f" ORDER BY id DESC LIMIT ${len(params)}"#fetch limit + 1 rows to see if next page is needed
        psql_response = await pool.fetch(query, *params)
        typed_events = []

        for row in psql_response:
            typed_events.append(WebhookEvent(id = row["id"], action=row["action"], repo=row["repo"], event_type=row['event_type'], received_at=row['received_at']))
        if len(psql_response) <= limit: #less items available than limit
            cursor = None
        else:
            cursor = encode_cursor(psql_response[-2]["id"], param_filters_hash)
            typed_events.pop()
        return EventResponse(events=typed_events, next_cursor = cursor)
    except TimeoutError as e:
        logger.error(json.dumps({"message": "Database read timed out"}))
        raise StorageError(reason= "Database read timed out") from e
    except OSError as e:
        logger.error(json.dumps({"message": "Could not reach database"}))
        raise StorageError(reason= "Could not reach database") from e
    except asyncpg.PostgresError as e:
        logger.error(json.dumps({"message": f"Postgres error: {e}"}))
        raise StorageError(reason= "Postgres error") from e

def decode_cursor(cursor: str) -> tuple[int, str]:
    decoded_string = base64.urlsafe_b64decode(cursor.encode()).decode()#str -> bytes -> decoded bytes -> string
    cursor_id = int(decoded_string.split("|")[0])
    filters_hash = decoded_string.split("|")[1]
    return cursor_id, filters_hash
def encode_cursor(data: int, filters_hash: str) -> str:
    encoded_str = "|".join([str(data), filters_hash])
    return base64.urlsafe_b64encode(encoded_str.encode()).decode() #list -> list in bytes ->encoded str in bytes -> str
def normalize_and_hash(filters: dict[str, str]) -> str:
    normalized = json.dumps(filters, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


