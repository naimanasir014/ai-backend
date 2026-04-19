from fastapi import FastAPI
from pydantic import BaseModel
from main import ChatBot

app = FastAPI()

class Query(BaseModel):
    message: str

@app.post("/chat")
def chat(query: Query):
    return {"response": ChatBot(query.message)}