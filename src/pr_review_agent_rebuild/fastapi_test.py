from fastapi import FastAPI

#create the app instance
app = FastAPI()

#define route using a decorator
@app.get("/")
def response() -> dict[str, bool]:
    return {"ok": True}


