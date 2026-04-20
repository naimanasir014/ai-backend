from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Query(BaseModel):
    message: str
@app.get("/")
def root():
    return {"message": "API running"}
@app.post("/chat")
def chat(query: Query):
    return {"response": (query.message)}
from fastapi import UploadFile, File
from deepface import DeepFace
import shutil
import 
@app.post("/verify-id")
async def verify_id(file: UploadFile = File(...)):
    try:
        file_path = f"temp_{file.filename}"

        # save file
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 🔥 AI FACE ANALYSIS
        result = DeepFace.analyze(
            img_path=file_path,
            actions=['age', 'gender'],
            enforce_detection=False
        )

        os.remove(file_path)

        return {
            "status": "verified",
            "details": result[0]
        }

    except Exception as e:
        return {
            "status": "failed",
            "error": str(e)
        }   