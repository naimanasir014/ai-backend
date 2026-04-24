print("🔥 MCQ SERVER RUNNING 🔥")

import os
import uuid
import shutil
import logging
import json
from pathlib import Path
from typing import Optional
from io import BytesIO

from groq import Groq
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
import cv2
import numpy as np
import face_recognition

# LOAD ENV
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("id-verify")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

FACE_MATCH_TOLERANCE = 0.50

# Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

app = FastAPI(title="AI Backend", version="4.0")


# ==============================
# MCQ SECTION (UNCHANGED)
# ==============================

class QueryRequest(BaseModel):
    message: str

SYSTEM_PROMPT = """
You are an MCQ generator AI.

Generate EXACTLY 10 multiple choice questions.

STRICT RULES:
- Each question MUST have 4 options (A, B, C, D)
- Mention correct answer
- DO NOT generate less than 10
- DO NOT add explanations
"""

def generate_mcqs(query):
    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}
            ],
            temperature=0.3,
            max_tokens=1500
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"ERROR: {str(e)}"


@app.post("/chat")
def chat_endpoint(request: QueryRequest):
    query = f"Generate EXACTLY 10 MCQs about {request.message}"
    result = generate_mcqs(query)
    return {"response": result}


# ==============================
# ID VERIFICATION SECTION
# ==============================

def save_upload(upload: UploadFile, suffix: str = ".jpg") -> Path:
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    logger.info(f"Saved: {dest} ({dest.stat().st_size} bytes)")
    return dest


def cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")


def load_image_rgb(path: Path) -> np.ndarray:
    try:
        pil_img = Image.open(path).convert("RGB")
        return np.array(pil_img)
    except Exception:
        img_bgr = cv2.imread(str(path))
        if img_bgr is None:
            raise ValueError(f"Cannot read image: {path}")
        return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)


def is_valid_image(path: Path) -> tuple:
    if not path.exists():
        return False, "File not found."
    size_kb = path.stat().st_size / 1024
    if size_kb < 5:
        return False, f"Image too small ({size_kb:.1f}KB). Please retake clearly."
    try:
        img = load_image_rgb(path)
        h, w = img.shape[:2]
        if w < 80 or h < 80:
            return False, f"Image resolution too low ({w}x{h})."
        return True, "ok"
    except Exception as e:
        return False, f"Cannot read image: {str(e)}"


def preprocess_for_id(image_rgb: np.ndarray) -> np.ndarray:
    h, w = image_rgb.shape[:2]
    if w < 1200:
        scale = 1200 / w
        new_w = int(w * scale)
        new_h = int(h * scale)
        image_rgb = cv2.resize(image_rgb, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    sharpened = cv2.filter2D(bgr, -1, kernel)
    return cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB)


def get_face_encoding(image_rgb: np.ndarray, label: str = "image"):
    processed = preprocess_for_id(image_rgb)
    for upsample in [2, 1, 0]:
        face_locations = face_recognition.face_locations(
            processed, number_of_times_to_upsample=upsample, model="hog"
        )
        if len(face_locations) > 0:
            break
    if len(face_locations) == 0:
        return None, f"No face detected in {label}."
    if len(face_locations) > 1:
        face_locations = [max(face_locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))]
    encodings = face_recognition.face_encodings(processed, face_locations, num_jitters=2)
    if len(encodings) == 0:
        return None, f"Could not encode face in {label}."
    return encodings[0], None


@app.get("/")
def root():
    return {"status": "online", "service": "AI Backend v4"}


@app.get("/ping")
def ping():
    return {"status": "alive"}


@app.post("/verify-front-id")
async def verify_front_id(front_id: UploadFile = File(...)):
    front_path = None
    try:
        front_path = save_upload(front_id, ".jpg")
        valid, reason = is_valid_image(front_path)
        if not valid:
            return JSONResponse(content={"status": "failed", "reason": reason})
        logger.info("Front ID image accepted.")
        return JSONResponse(content={"status": "verified", "message": "Front ID accepted."})
    except Exception as e:
        logger.error(f"/verify-front-id error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup(front_path)


@app.post("/verify-id")
async def verify_id(
    selfie: UploadFile = File(...),
    front_id: UploadFile = File(...),
    back_id: Optional[UploadFile] = File(None),
):
    selfie_path = None
    front_path = None
    back_path = None
    try:
        selfie_path = save_upload(selfie, ".jpg")
        front_path = save_upload(front_id, ".jpg")
        if back_id and back_id.filename:
            back_path = save_upload(back_id, ".jpg")

        for path, label in [(selfie_path, "selfie"), (front_path, "front ID")]:
            valid, reason = is_valid_image(path)
            if not valid:
                return JSONResponse(content={"status": "failed", "reason": f"{label.capitalize()} issue: {reason}"})

        selfie_rgb = load_image_rgb(selfie_path)
        selfie_encoding, _ = get_face_encoding(selfie_rgb, "selfie")
        if selfie_encoding is None:
            return JSONResponse(content={"status": "failed", "reason": "No face detected in selfie. Ensure your face is fully visible, well-lit, and centered."})

        front_rgb = load_image_rgb(front_path)
        id_encoding, _ = get_face_encoding(front_rgb, "ID card")
        if id_encoding is None:
            return JSONResponse(content={"status": "failed", "reason": "No face found on ID card. Ensure the front side with your photo is clearly visible."})

        distance = face_recognition.face_distance([id_encoding], selfie_encoding)[0]
        matched = bool(distance <= FACE_MATCH_TOLERANCE)

        logger.info(f"Face distance: {distance:.4f} | Match: {matched}")

        if distance < 0.35:
            confidence = "very_high"
        elif distance < 0.45:
            confidence = "high"
        elif distance < FACE_MATCH_TOLERANCE:
            confidence = "medium"
        else:
            confidence = "low"

        if matched:
            return JSONResponse(content={"status": "verified", "distance": round(float(distance), 4), "confidence": confidence, "message": "Identity verified successfully."})
        else:
            return JSONResponse(content={"status": "failed", "reason": "Your face does not match the ID photo. Ensure good lighting and try again.", "distance": round(float(distance), 4), "confidence": confidence})

    except Exception as e:
        logger.error(f"/verify-id error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification error: {str(e)}")
    finally:
        cleanup(selfie_path, front_path, back_path)


# ==============================
# JOB RECOMMENDATION SECTION
# ==============================

@app.post("/recommend-jobs")
async def recommend_jobs(request: Request):
    try:
        body = await request.json()
        skills = body.get("skills", [])
        jobs = body.get("jobs", [])

        if not skills:
            return JSONResponse(content={"status": "failed", "reason": "No skills provided."})

        if not jobs:
            return JSONResponse(content={"status": "failed", "reason": "No jobs available."})

        skills_str = ", ".join(skills)
        jobs_to_evaluate = jobs[:30]

        # Build a summary string for the AI prompt, keeping job index for reference
        jobs_summary_lines = []
        for i, job in enumerate(jobs_to_evaluate):
            jobs_summary_lines.append(
                f"{i}. ID:{job.get('id', '?')} | Title:{job.get('title', '?')} | "
                f"Category:{job.get('category', '?')} | "
                f"Description:{str(job.get('description', ''))[:150]} | "
                f"Budget:{job.get('minBudget', '?')}-{job.get('maxBudget', '?')}"
            )

        jobs_text = "\n".join(jobs_summary_lines)

        prompt = f"""You are an expert job matching AI for a freelancing platform.

Freelancer skills: {skills_str}

Available jobs:
{jobs_text}

Task: Analyze the freelancer skills and recommend the most relevant jobs.
Return ONLY a valid JSON array (no extra text, no markdown, no code fences) like this:
[
  {{
    "job_index": 0,
    "match_score": 95,
    "reason": "Short reason why this matches the skills"
  }}
]

Rules:
- Only include jobs with match_score >= 50
- Maximum 10 jobs
- Sort by match_score descending
- Keep reason under 20 words
- Return ONLY the JSON array, nothing else"""

        chat_response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )

        ai_text = chat_response.choices[0].message.content.strip()
        logger.info(f"Groq AI raw response: {ai_text}")

        # Strip markdown code fences if present
        if "```" in ai_text:
            parts = ai_text.split("```")
            # Take the content inside the first code block
            ai_text = parts[1] if len(parts) > 1 else parts[0]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        ai_text = ai_text.strip()

        matched = json.loads(ai_text)

        # ── KEY FIX ──
        # Rebuild each result using the original job dict (which has the real Firestore id)
        # and attach match_score + reason from AI on top.
        recommended = []
        for item in matched:
            idx = item.get("job_index")
            if idx is None or not (0 <= idx < len(jobs_to_evaluate)):
                continue
            job = dict(jobs_to_evaluate[idx])          # copy original job (includes 'id')
            job["match_score"] = int(item.get("match_score", 0))
            job["reason"] = item.get("reason", "")
            recommended.append(job)

        # Sort descending by match_score (AI should already do this, but be safe)
        recommended.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        logger.info(f"Recommended {len(recommended)} jobs for skills: {skills_str}")

        return JSONResponse(content={
            "status": "success",
            "recommended_jobs": recommended,
            "total": len(recommended),
        })

    except json.JSONDecodeError as e:
        # AI returned malformed JSON → return top 10 jobs as fallback with score 0
        logger.error(f"AI JSON parse error: {e}. Raw text: {ai_text if 'ai_text' in dir() else 'N/A'}")
        fallback = [dict(j) for j in jobs[:10]]
        for j in fallback:
            j["match_score"] = 0
            j["reason"] = "Keyword-based fallback"
        return JSONResponse(content={
            "status": "success",
            "recommended_jobs": fallback,
            "total": len(fallback),
        })

    except Exception as e:
        logger.error(f"/recommend-jobs error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ==============================
# PROPOSAL GENERATION SECTION
# ==============================

class ProposalRequest(BaseModel):
    prompt: str

@app.post("/generate-proposal")
async def generate_proposal(request: ProposalRequest):
    try:
        completion = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional freelancer who writes compelling job proposals. Write clear, concise, and persuasive proposals."
                },
                {
                    "role": "user",
                    "content": request.prompt
                }
            ],
            temperature=0.7,
            max_tokens=600,
        )
        proposal = completion.choices[0].message.content.strip()
        logger.info("Proposal generated successfully.")
        return JSONResponse(content={"status": "success", "proposal": proposal})
    except Exception as e:
        logger.error(f"/generate-proposal error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
