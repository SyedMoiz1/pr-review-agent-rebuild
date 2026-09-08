# Webhook endpoint with HMAC verification — the main thing left. A FastAPI route (POST /webhook) that:
# Reads the X-Hub-Signature-256 header GitHub sends
# Recomputes the HMAC using your secret key and the raw request body
# Uses hmac.compare_digest to compare (timing-safe)
# Returns 200 if valid, 401 if not
import hashlib
import hmac
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

#from pydantic import BaseModel
#import json

load_dotenv()
secret = os.environ["WEBHOOK_SECRET"]

app = FastAPI()

#setup Basemodel for webhook payloads

# class WebhookPayload(BaseModel): TODO: To use this define pull_request, repository and sender as their own pydantic models
#     action: str
#     number: int
#     pull_request: dict
#     repository: dict
#     sender: dict

def validate_hmac(hmac_str: str, payload: str, secret: str) -> bool:
    actual = 'sha256=' + hmac.new(secret.encode(), payload.encode(), digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, hmac_str)

@app.post("/items/")
async def create_item(request: Request) -> JSONResponse:
    raw_body = await request.body()

    #data = json.loads(raw_body)
    payload = raw_body.decode("utf-8")
    try:
        signature = request.headers['x-hub-signature-256']
    except KeyError as e: #TODO: add custom exception hierarchies
        raise KeyError("No HMAC found") from e
    #payload_data = WebhookPayload(**data)
    response = validate_hmac(signature, payload, secret=secret)
    
    if response:
        return JSONResponse(
            status_code=200,
            content={"message": "Pull Request received and validated successfully."}
        )
    else:
        return JSONResponse(
            status_code=401,
            content={"message": "HMAC not valid."}
        )