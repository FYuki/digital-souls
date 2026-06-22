from dotenv import load_dotenv
from fastapi import FastAPI

from app.routers.chat import router as chat_router

load_dotenv()

app = FastAPI()

app.include_router(chat_router)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
