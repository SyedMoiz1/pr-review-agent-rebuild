# Webhook endpoint with HMAC verification — the main thing left. A FastAPI route (POST /webhook) that:
# Reads the X-Hub-Signature-256 header GitHub sends
# Recomputes the HMAC using your secret key and the raw request body
# Uses hmac.compare_digest to compare (timing-safe)
# Returns 200 if valid, 401 if not
import hashlib
import hmac
import logging
import time
import json
import os
import uuid

from contextvars import ContextVar
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from .exceptions import AppError, InvalidSignature, MalformedPayload, StorageError
from json.decoder import JSONDecodeError
from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails
from starlette.middleware.base import RequestResponseEndpoint

load_dotenv()
secret = os.environ["WEBHOOK_SECRET"]

#create app
app = FastAPI()

#configure logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s  - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

#Initiate context vars
request_id_var: ContextVar[str] = ContextVar('request_id_var')

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

class Sender(BaseModel):
    login: str
    id: int

class WebhookPayload(BaseModel): 
    action: str
    number: int
    pull_request: PullRequest
    repository: Repository
    sender: Sender

def validate_hmac(hmac_str: str, payload: bytes, secret: str) -> bool:
    actual = 'sha256=' + hmac.new(secret.encode(), payload, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, hmac_str)

def find_field_errors(errors: list[ErrorDetails]) -> list[str]:
    missing_fields = []
    for item in errors:
        field_error = f"Field(s): {'.'.join(str(x) for x in item['loc'])} -> Error Type: {item['type']} -> Message: {item['msg']}"
        missing_fields.append(field_error)
    return missing_fields

@app.middleware("http")
async def add_request_id(request: Request, call_next: RequestResponseEndpoint) -> Response | None:
    headers = request.headers
    if "X-Request-ID" in headers:
        request_uuid = headers["X-Request-ID"]
    else:
        request_uuid = str(uuid.uuid4())
    request_id_var.set(request_uuid)
    response = None

    try:
        before_time = time.perf_counter()
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
async def app_error_handler(exc: AppError) -> JSONResponse:
    status = exc.status
    return JSONResponse(status_code=status, content={"request_id": request_id_var.get(), "error": str(exc)})

@app.post("/webhook")
async def create_item(request: Request) -> JSONResponse:

    raw_body = await request.body()
    signature = request.headers.get('x-hub-signature-256')
    if not signature:
        logger.warning(json.dumps({"request_id": f"{request_id_var.get()}", "path": f"{request.url.path}", "message": f"Request failed due to a missing HMAC."})) 
        raise InvalidSignature(reason = "Authentication Error")
    valid_hmac = validate_hmac(signature, raw_body, secret=secret)
    
    if valid_hmac:
        try:
            data = json.loads(raw_body)
            payload = WebhookPayload(**data)
            logger.info(json.dumps({"request_id": f"{request_id_var.get()}", "message": f"Webhook number {payload.number} for Pull Request {payload.pull_request.id} successfully verified"})) 
            return JSONResponse(
                status_code=200,
                content={"message": "Pull Request received and validated successfully."}
            )
        except ValidationError as e:
            fields = find_field_errors(e.errors())
            raise MalformedPayload(reason = f"Invalid Data Fields. Payload has an issue with the following fields: {', '.join(fields)}") from e
        except JSONDecodeError as e:
            raise MalformedPayload(reason = "Invalid Data Fields. Please ensure payload is sent in JSON format.") from e
    else:
        logger.warning(json.dumps({"request_id": f"{request_id_var.get()}", "message": f"Request failed due to an Invalid Signature"})) 
        raise InvalidSignature(reason = "Authentication Error")
    
