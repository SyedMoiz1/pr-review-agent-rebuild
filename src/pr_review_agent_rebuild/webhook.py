# Webhook endpoint with HMAC verification — the main thing left. A FastAPI route (POST /webhook) that:
# Reads the X-Hub-Signature-256 header GitHub sends
# Recomputes the HMAC using your secret key and the raw request body
# Uses hmac.compare_digest to compare (timing-safe)
# Returns 200 if valid, 401 if not
import hashlib
import hmac
import json
import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

load_dotenv()
secret = os.environ["WEBHOOK_SECRET"]

app = FastAPI()

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

def validate_hmac(hmac_str: str, payload: str, secret: str) -> bool:
    actual = 'sha256=' + hmac.new(secret.encode(), payload.encode(), digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, hmac_str)

@app.post("/webhook")
async def create_item(request: Request) -> JSONResponse:
    raw_body = await request.body()
    body = raw_body.decode("utf-8")
    try:
        signature = request.headers['x-hub-signature-256']
    except KeyError as e: #TODO: add custom exception hierarchies
        raise KeyError("No HMAC found") from e
    response = validate_hmac(signature, body, secret=secret)
    
    if response:
        data = json.loads(raw_body)
        try:
            payload = WebhookPayload(**data)
            print(f"Webhook number {payload.number} for Pull Request {payload.pull_request.id} successfully verified")
            return JSONResponse(
                status_code=200,
                content={"message": "Pull Request received and validated successfully."}
            )
        except (ValidationError) as e:
            #print(e)
            return JSONResponse(
                status_code=422,
                content={"message": f"Invalid Data Fields. {e}"}
            )
    else:
        return JSONResponse(
            status_code=401,
            content={"message": "HMAC not valid."}
        )