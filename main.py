print("🔥 AI BACKEND SERVER RUNNING 🔥")

import os
import uuid
import shutil
import logging
import json
from pathlib import Path
from typing import Optional
import stripe

import httpx
from groq import Groq
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
import cv2
import numpy as np
import face_recognition

import firebase_admin
from firebase_admin import credentials, firestore, messaging

# ══════════════════════════════════════════════════════════════
# LOAD ENV
# ══════════════════════════════════════════════════════════════
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-backend")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

FACE_MATCH_TOLERANCE = 0.50

# Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Anthropic (chatbot)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

# Stripe
stripe.api_key        = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

app = FastAPI(title="AI Backend", version="5.0")

# ══════════════════════════════════════════════════════════════
# ✅ FIREBASE ADMIN INIT — FIXED
# ══════════════════════════════════════════════════════════════
db = None  # ✅ Start as None — set after successful init

def _init_firebase():
    """
    Initialize Firebase Admin SDK.
    Supports both:
      - Local: serviceAccountKey.json file on disk
      - Railway: FIREBASE_SERVICE_ACCOUNT env var with JSON string
    """
    global db

    if firebase_admin._apps:
        db = firestore.client()
        logger.info("✅ Firebase already initialized.")
        return True

    # Option 1: JSON string in environment variable (Railway)
    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if service_account_json:
        try:
            service_account_info = json.loads(service_account_json)
            cred = credentials.Certificate(service_account_info)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            logger.info("✅ Firebase initialized from FIREBASE_SERVICE_ACCOUNT_JSON env var.")
            return True
        except Exception as e:
            logger.error(f"❌ Firebase init from env JSON failed: {e}")

    # Option 2: Path to JSON file (local development)
    service_account_path = os.getenv("FIREBASE_SERVICE_ACCOUNT", "serviceAccountKey.json")
    if os.path.exists(service_account_path):
        try:
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            db = firestore.client()
            logger.info(f"✅ Firebase initialized from file: {service_account_path}")
            return True
        except Exception as e:
            logger.error(f"❌ Firebase init from file failed: {e}")

    logger.error(
        "❌ Firebase NOT initialized. "
        "Set FIREBASE_SERVICE_ACCOUNT_JSON in Railway or place serviceAccountKey.json locally."
    )
    return False


# ✅ Initialize Firebase on startup
_init_firebase()


# ══════════════════════════════════════════════════════════════
# HELPER: Get DB safely
# ══════════════════════════════════════════════════════════════
def get_db():
    """Returns Firestore client, retries init if not ready."""
    global db
    if db is None:
        logger.warning("⚠️ Firestore not ready, retrying init...")
        _init_firebase()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Firebase not initialized. Check FIREBASE_SERVICE_ACCOUNT_JSON in Railway."
        )
    return db


# ══════════════════════════════════════════════════════════════
# HELPER: Firestore notification + FCM push
# ══════════════════════════════════════════════════════════════
async def send_notification(
    user_id: str,
    sender_id: str,
    notification_type: str,
    title: str,
    message: str,
    job_id: str = "",
    proposal_id: str = "",
):
    try:
        _db = get_db()
        _db.collection("notifications").add({
            "userId":     user_id,
            "senderId":   sender_id,
            "type":       notification_type,
            "title":      title,
            "message":    message,
            "jobId":      job_id,
            "proposalId": proposal_id,
            "isRead":     False,
            "createdAt":  firestore.SERVER_TIMESTAMP,
        })
        logger.info(f"✅ Notification saved for user: {user_id}")

        user_doc = _db.collection("users").document(user_id).get()
        if not user_doc.exists:
            logger.warning(f"User document not found: {user_id}")
            return

        fcm_token = user_doc.to_dict().get("fcmToken")
        if not fcm_token:
            logger.warning(f"No FCM token for user: {user_id}")
            return

        fcm_msg = messaging.Message(
            notification=messaging.Notification(title=title, body=message),
            data={
                "type":         notification_type,
                "jobId":        job_id,
                "proposalId":   proposal_id,
                "click_action": "FLUTTER_NOTIFICATION_CLICK",
            },
            token=fcm_token,
        )
        response = messaging.send(fcm_msg)
        logger.info(f"✅ FCM push sent: {response}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ send_notification error: {e}", exc_info=True)


async def _push_fcm(token: str, title: str, body: str, data: dict | None = None):
    try:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={**(data or {}), "click_action": "FLUTTER_NOTIFICATION_CLICK"},
            token=token,
        )
        messaging.send(msg)
        logger.info("✅ FCM (chatbot) push sent")
    except Exception as e:
        logger.error(f"❌ FCM (chatbot) error: {e}")


# ══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════
@app.get("/")
def root():
    return {
        "status":   "online",
        "service":  "AI Backend v5",
        "firebase": "connected" if db is not None else "❌ NOT connected",
        "stripe":   "configured" if stripe.api_key else "❌ NOT configured",
        "anthropic":"configured" if ANTHROPIC_API_KEY else "❌ NOT configured",
    }


@app.get("/ping")
def ping():
    return {"status": "alive"}


# ══════════════════════════════════════════════════════════════
# NOTIFICATION ENDPOINTS
# ══════════════════════════════════════════════════════════════
class NewProposalNotificationRequest(BaseModel):
    clientId:       str
    freelancerId:   str
    freelancerName: str
    jobId:          str
    jobTitle:       str
    proposalId:     str = ""


class ProposalStatusNotificationRequest(BaseModel):
    freelancerId: str
    clientId:     str
    jobId:        str
    jobTitle:     str
    status:       str
    proposalId:   str = ""


class PaymentNotifyRequest(BaseModel):
    freelancerId: str
    clientId:     str
    jobId:        str
    jobTitle:     str
    amount:       float
    proposalId:   str = ""


@app.post("/notify/new-proposal")
async def notify_new_proposal(req: NewProposalNotificationRequest):
    try:
        await send_notification(
            user_id=req.clientId,
            sender_id=req.freelancerId,
            notification_type="new_proposal",
            title="New Proposal Received 📨",
            message=f"{req.freelancerName} submitted a proposal on \"{req.jobTitle}\"",
            job_id=req.jobId,
            proposal_id=req.proposalId,
        )
        return JSONResponse(content={"status": "success"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notify/proposal-status")
async def notify_proposal_status(req: ProposalStatusNotificationRequest):
    try:
        accepted = req.status == "accepted"
        await send_notification(
            user_id=req.freelancerId,
            sender_id=req.clientId,
            notification_type="proposal_accepted" if accepted else "proposal_rejected",
            title="Proposal Accepted 🎉" if accepted else "Proposal Rejected",
            message=f"Your proposal on \"{req.jobTitle}\" was {req.status}",
            job_id=req.jobId,
            proposal_id=req.proposalId,
        )
        return JSONResponse(content={"status": "success"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/notify/advance-payment")
async def notify_advance_payment(req: PaymentNotifyRequest):
    try:
        await send_notification(
            user_id=req.freelancerId,
            sender_id="system",
            notification_type="advance_payment",
            title="Advance Payment Received 💰",
            message=f"You received ${req.amount:.2f} advance for \"{req.jobTitle}\"",
            job_id=req.jobId,
            proposal_id=req.proposalId,
        )
        await send_notification(
            user_id=req.clientId,
            sender_id="system",
            notification_type="payment_sent",
            title="Payment Sent ✅",
            message=f"Advance of ${req.amount:.2f} sent for \"{req.jobTitle}\"",
            job_id=req.jobId,
            proposal_id=req.proposalId,
        )
        return JSONResponse(content={"status": "success"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# STRIPE — Create Payment Intent
# ══════════════════════════════════════════════════════════════
class CreatePaymentIntentRequest(BaseModel):
    amount:       float
    currency:     str = "usd"
    freelancerId: str = ""
    jobTitle:     str = ""


@app.post("/create-payment-intent")
async def create_payment_intent(req: CreatePaymentIntentRequest):
    try:
        if not stripe.api_key:
            raise HTTPException(
                status_code=500,
                detail="Stripe not configured — add STRIPE_SECRET_KEY to Railway"
            )

        amount_cents = int(req.amount * 100)

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=req.currency,
            metadata={
                "freelancerId": req.freelancerId,
                "jobTitle":     req.jobTitle,
            },
            automatic_payment_methods={"enabled": True},
        )

        logger.info(f"✅ PaymentIntent created: {intent.id} for ${req.amount}")
        return JSONResponse(content={
            "status":          "success",
            "clientSecret":    intent.client_secret,
            "paymentIntentId": intent.id,
        })

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"/create-payment-intent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# STRIPE — Webhook
# ══════════════════════════════════════════════════════════════
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("❌ Invalid Stripe webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    _db = get_db()

    if event["type"] == "payment_intent.succeeded":
        intent            = event["data"]["object"]
        payment_intent_id = intent["id"]
        amount_paid       = intent["amount"] / 100
        freelancer_id     = intent["metadata"].get("freelancerId", "")
        job_title         = intent["metadata"].get("jobTitle", "")

        logger.info(f"✅ Payment confirmed: {payment_intent_id} — ${amount_paid}")

        query = _db.collection("payments").where(
            "paymentIntentId", "==", payment_intent_id
        ).limit(1).get()
        for doc in query:
            doc.reference.update({
                "status":      "confirmed",
                "confirmedAt": firestore.SERVER_TIMESTAMP,
            })

        if freelancer_id:
            await send_notification(
                user_id=freelancer_id,
                sender_id="system",
                notification_type="advance_payment",
                title="Payment Confirmed 💰",
                message=f"${amount_paid:.2f} confirmed for \"{job_title}\"",
            )

    elif event["type"] == "payment_intent.payment_failed":
        intent            = event["data"]["object"]
        payment_intent_id = intent["id"]
        logger.warning(f"❌ Payment failed: {payment_intent_id}")

        query = _db.collection("payments").where(
            "paymentIntentId", "==", payment_intent_id
        ).limit(1).get()
        for doc in query:
            doc.reference.update({"status": "failed"})

    elif event["type"] == "charge.refunded":
        logger.info(f"💸 Refund: {event['data']['object']['id']}")

    return JSONResponse(content={"status": "received"})


# ══════════════════════════════════════════════════════════════
# CHATBOT — Claude / Anthropic
# ══════════════════════════════════════════════════════════════
CHATBOT_SYSTEM = """You are a helpful in-app assistant for a freelancing platform called FreelancerApp.
Help users with: finding jobs, writing proposals, understanding platform features, general freelancing advice.
Be concise, warm, and professional. Keep responses under 150 words unless more detail is truly needed."""


class ChatMessage(BaseModel):
    role:    str
    content: str


class ChatbotRequest(BaseModel):
    messages:  list[ChatMessage]
    user_id:   Optional[str] = None
    fcm_token: Optional[str] = None


@app.post("/chatbot")
async def chatbot(req: ChatbotRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is empty")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY not configured — add it to Railway"
        )

    payload = {
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "system":     CHATBOT_SYSTEM,
        "messages":   [{"role": m.role, "content": m.content} for m in req.messages],
    }
    headers = {
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as hc:
        res = await hc.post(ANTHROPIC_URL, json=payload, headers=headers)

    if res.status_code != 200:
        logger.error(f"Anthropic {res.status_code}: {res.text}")
        raise HTTPException(status_code=502, detail=f"Anthropic error: {res.text}")

    data       = res.json()
    reply_text = data["content"][0]["text"]

    if req.user_id:
        try:
            _db = get_db()
            _db.collection("notifications").add({
                "userId":    req.user_id,
                "senderId":  "chatbot",
                "type":      "chatbot_reply",
                "title":     "AI Assistant",
                "message":   reply_text[:200],
                "isRead":    False,
                "createdAt": firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            logger.warning(f"Could not save chatbot notification: {e}")

    if req.fcm_token:
        preview = reply_text[:100] + ("…" if len(reply_text) > 100 else "")
        await _push_fcm(
            token=req.fcm_token,
            title="AI Assistant",
            body=preview,
            data={"type": "chatbot_reply", "userId": req.user_id or ""},
        )

    return {"reply": reply_text, "usage": data.get("usage", {})}


# ══════════════════════════════════════════════════════════════
# MCQ SECTION
# ══════════════════════════════════════════════════════════════
class QueryRequest(BaseModel):
    message: str

MCQ_SYSTEM_PROMPT = """
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
                {"role": "system", "content": MCQ_SYSTEM_PROMPT},
                {"role": "user",   "content": query},
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"ERROR: {str(e)}"


@app.post("/chat")
def chat_endpoint(request: QueryRequest):
    result = generate_mcqs(f"Generate EXACTLY 10 MCQs about {request.message}")
    return {"response": result}


# ══════════════════════════════════════════════════════════════
# ID VERIFICATION
# ══════════════════════════════════════════════════════════════
def save_upload(upload: UploadFile, suffix: str = ".jpg") -> Path:
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = UPLOAD_DIR / filename
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
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
        return np.array(Image.open(path).convert("RGB"))
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
        return False, f"Image too small ({size_kb:.1f}KB)."
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
        scale     = 1200 / w
        image_rgb = cv2.resize(
            image_rgb,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_CUBIC,
        )
    kernel    = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    bgr       = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    sharpened = cv2.filter2D(bgr, -1, kernel)
    return cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB)


def get_face_encoding(image_rgb: np.ndarray, label: str = "image"):
    processed = preprocess_for_id(image_rgb)
    for upsample in [2, 1, 0]:
        locs = face_recognition.face_locations(
            processed,
            number_of_times_to_upsample=upsample,
            model="hog",
        )
        if locs:
            break
    if not locs:
        return None, f"No face detected in {label}."
    if len(locs) > 1:
        locs = [max(locs, key=lambda l: (l[2] - l[0]) * (l[1] - l[3]))]
    encs = face_recognition.face_encodings(processed, locs, num_jitters=2)
    if not encs:
        return None, f"Could not encode face in {label}."
    return encs[0], None


@app.post("/verify-front-id")
async def verify_front_id(front_id: UploadFile = File(...)):
    front_path = None
    try:
        front_path = save_upload(front_id, ".jpg")
        valid, reason = is_valid_image(front_path)
        if not valid:
            return JSONResponse(content={"status": "failed", "reason": reason})
        return JSONResponse(content={"status": "verified", "message": "Front ID accepted."})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cleanup(front_path)


@app.post("/verify-id")
async def verify_id(
    selfie:   UploadFile           = File(...),
    front_id: UploadFile           = File(...),
    back_id:  Optional[UploadFile] = File(None),
):
    selfie_path = front_path = back_path = None
    try:
        selfie_path = save_upload(selfie,   ".jpg")
        front_path  = save_upload(front_id, ".jpg")
        if back_id and back_id.filename:
            back_path = save_upload(back_id, ".jpg")

        for path, label in [(selfie_path, "selfie"), (front_path, "front ID")]:
            valid, reason = is_valid_image(path)
            if not valid:
                return JSONResponse(content={
                    "status": "failed",
                    "reason": f"{label.capitalize()} issue: {reason}",
                })

        selfie_enc, _ = get_face_encoding(load_image_rgb(selfie_path), "selfie")
        if selfie_enc is None:
            return JSONResponse(content={"status": "failed", "reason": "No face in selfie."})

        id_enc, _ = get_face_encoding(load_image_rgb(front_path), "ID card")
        if id_enc is None:
            return JSONResponse(content={"status": "failed", "reason": "No face on ID card."})

        distance   = face_recognition.face_distance([id_enc], selfie_enc)[0]
        matched    = bool(distance <= FACE_MATCH_TOLERANCE)
        confidence = (
            "very_high" if distance < 0.35 else
            "high"      if distance < 0.45 else
            "medium"    if distance < FACE_MATCH_TOLERANCE else
            "low"
        )

        if matched:
            return JSONResponse(content={
                "status":     "verified",
                "distance":   round(float(distance), 4),
                "confidence": confidence,
                "message":    "Identity verified successfully.",
            })
        return JSONResponse(content={
            "status":     "failed",
            "reason":     "Face does not match ID photo.",
            "distance":   round(float(distance), 4),
            "confidence": confidence,
        })

    except Exception as e:
        logger.error(f"/verify-id error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Verification error: {str(e)}")
    finally:
        cleanup(selfie_path, front_path, back_path)


# ══════════════════════════════════════════════════════════════
# JOB RECOMMENDATION
# ══════════════════════════════════════════════════════════════
@app.post("/recommend-jobs")
async def recommend_jobs(request: Request):
    try:
        body   = await request.json()
        skills = body.get("skills", [])
        jobs   = body.get("jobs", [])

        if not skills:
            return JSONResponse(content={"status": "failed", "reason": "No skills provided."})
        if not jobs:
            return JSONResponse(content={"status": "failed", "reason": "No jobs available."})

        jobs_to_evaluate = jobs[:30]
        jobs_text = "\n".join(
            f"{i}. ID:{j.get('id','?')} | Title:{j.get('title','?')} | "
            f"Category:{j.get('category','?')} | "
            f"Description:{str(j.get('description',''))[:150]} | "
            f"Budget:{j.get('minBudget','?')}-{j.get('maxBudget','?')}"
            for i, j in enumerate(jobs_to_evaluate)
        )

        prompt = f"""You are an expert job matching AI for a freelancing platform.
Freelancer skills: {", ".join(skills)}
Available jobs:
{jobs_text}
Return ONLY a valid JSON array like:
[{{"job_index":0,"match_score":95,"reason":"Short reason"}}]
Rules: match_score >= 50 only, max 10 jobs, sort descending, reason < 20 words."""

        ai_raw = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        ).choices[0].message.content.strip()

        if "```" in ai_raw:
            parts  = ai_raw.split("```")
            ai_raw = parts[1] if len(parts) > 1 else parts[0]
            if ai_raw.startswith("json"):
                ai_raw = ai_raw[4:]

        matched     = json.loads(ai_raw.strip())
        recommended = []
        for item in matched:
            idx = item.get("job_index")
            if idx is None or not (0 <= idx < len(jobs_to_evaluate)):
                continue
            job = dict(jobs_to_evaluate[idx])
            job["match_score"] = int(item.get("match_score", 0))
            job["reason"]      = item.get("reason", "")
            recommended.append(job)

        recommended.sort(key=lambda x: x.get("match_score", 0), reverse=True)
        return JSONResponse(content={
            "status":           "success",
            "recommended_jobs": recommended,
            "total":            len(recommended),
        })

    except json.JSONDecodeError:
        fallback = [dict(j) for j in jobs[:10]]
        for j in fallback:
            j["match_score"] = 0
            j["reason"]      = "Keyword-based fallback"
        return JSONResponse(content={
            "status":           "success",
            "recommended_jobs": fallback,
            "total":            len(fallback),
        })
    except Exception as e:
        logger.error(f"/recommend-jobs error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# PROPOSAL GENERATION
# ══════════════════════════════════════════════════════════════
class ProposalRequest(BaseModel):
    prompt: str


@app.post("/generate-proposal")
async def generate_proposal(request: ProposalRequest):
    try:
        proposal = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": "You are a professional freelancer who writes compelling job proposals."},
                {"role": "user",   "content": request.prompt},
            ],
            temperature=0.7,
            max_tokens=600,
        ).choices[0].message.content.strip()
        return JSONResponse(content={"status": "success", "proposal": proposal})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
