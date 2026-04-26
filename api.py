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
        import os
import stripe
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from groq import Groq
import logging

logger = logging.getLogger(__name__)

app = FastAPI()

# ── STRIPE SETUP ──────────────────────────────────────────────
stripe.api_key = os.getenv("sk_test_51TQN26QcqFE5b0RVAswEQCvPKZo6MMhEaG5dmGgCxXo0sVha32QO7FDAbMuBenpCkloM58vjXUSxdNhiQfzlVBPj00ZUqAYXkf", "")

# ── GROQ / CLAUDE SETUP ───────────────────────────────────────
groq_client = Groq(api_key=os.getenv("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", ""))


# ══════════════════════════════════════════════════════════════
# STRIPE — Create Payment Intent
# ══════════════════════════════════════════════════════════════
class CreatePaymentIntentRequest(BaseModel):
    amount: float         # in USD (e.g. 100.0)
    currency: str = "usd"
    freelancerId: str = ""
    jobTitle: str = ""

@app.post("/create-payment-intent")
async def create_payment_intent(req: CreatePaymentIntentRequest):
    try:
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Stripe not configured")

        amount_cents = int(req.amount * 100)  # Stripe needs cents

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=req.currency,
            metadata={
                "freelancerId": req.freelancerId,
                "jobTitle": req.jobTitle,
            },
            automatic_payment_methods={"enabled": True},
        )

        logger.info(f"✅ PaymentIntent created: {intent.id} for ${req.amount}")
        return JSONResponse(content={
            "status": "success",
            "clientSecret": intent.client_secret,
            "paymentIntentId": intent.id,
        })

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"/create-payment-intent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# CHATBOT — Claude / Groq
# ══════════════════════════════════════════════════════════════
class ChatMessage(BaseModel):
    role: str    # "user" or "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    user_id: str = ""
    fcm_token: str = ""

@app.post("/chatbot")
async def chatbot(req: ChatRequest):
    try:
        formatted = [
            {"role": m.role, "content": m.content}
            for m in req.messages
        ]

        response = groq_client.chat.completions.create(
            model="llama3-70b-8192",   # or "mixtral-8x7b-32768"
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful AI assistant for a freelancing platform. "
                        "Help users with job posting, proposal writing, payment questions, "
                        "and general freelancing advice. Be concise and friendly."
                    ),
                },
                *formatted,
            ],
            max_tokens=1024,
            temperature=0.7,
        )

        reply = response.choices[0].message.content
        logger.info(f"✅ Chatbot replied to user {req.user_id}")
        return JSONResponse(content={"reply": reply, "status": "success"})

    except Exception as e:
        logger.error(f"/chatbot error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════
# NOTIFICATIONS — Advance Payment
# ══════════════════════════════════════════════════════════════
class PaymentNotifyRequest(BaseModel):
    freelancerId: str
    clientId: str
    jobId: str
    jobTitle: str
    amount: float
    proposalId: str = ""

@app.post("/notify/advance-payment")
async def notify_advance_payment(req: PaymentNotifyRequest):
    """
    Called after advance payment — notifies both client and freelancer.
    Saves notifications to Firestore (Flutter handles FCM push separately).
    """
    try:
        import firebase_admin
        from firebase_admin import firestore as admin_firestore

        db = admin_firestore.client()

        # Notify freelancer
        db.collection("notifications").add({
            "userId": req.freelancerId,
            "senderId": "system",
            "type": "advance_payment",
            "title": "Advance Payment Received 💰",
            "message": f"You received ${req.amount:.2f} advance for \"{req.jobTitle}\"",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })

        # Notify client
        db.collection("notifications").add({
            "userId": req.clientId,
            "senderId": "system",
            "type": "payment_sent",
            "title": "Payment Sent ✅",
            "message": f"Advance of ${req.amount:.2f} sent for \"{req.jobTitle}\"",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })

        return JSONResponse(content={"status": "success"})

    except Exception as e:
        logger.error(f"/notify/advance-payment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    import os
import stripe
import anthropic
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import firebase_admin
from firebase_admin import credentials, firestore as admin_firestore

# ── LOGGING ────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── FIREBASE INIT ──────────────────────────────────────────────
if not firebase_admin._apps:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)

db = admin_firestore.client()

# ── STRIPE INIT ────────────────────────────────────────────────
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# ── ANTHROPIC INIT ─────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", "")
)

# ── FASTAPI APP ────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════
# MODELS
# ══════════════════════════════════════════════════════════════
class CreatePaymentIntentRequest(BaseModel):
    amount: float
    currency: str = "usd"
    freelancerId: str = ""
    jobTitle: str = ""

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    user_id: str = ""
    fcm_token: str = ""

class PaymentNotifyRequest(BaseModel):
    freelancerId: str
    clientId: str
    jobId: str
    jobTitle: str
    amount: float
    proposalId: str = ""

class ProposalNotifyRequest(BaseModel):
    clientId: str
    freelancerId: str
    freelancerName: str
    jobId: str
    jobTitle: str
    proposalId: str = ""
    status: str = ""

# ══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ══════════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {"status": "running", "message": "FreelancerApp Backend ✅"}

# ══════════════════════════════════════════════════════════════
# STRIPE — Create Payment Intent
# ══════════════════════════════════════════════════════════════
@app.post("/create-payment-intent")
async def create_payment_intent(req: CreatePaymentIntentRequest):
    try:
        if not stripe.api_key:
            raise HTTPException(status_code=500, detail="Stripe not configured")

        amount_cents = int(req.amount * 100)

        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=req.currency,
            metadata={
                "freelancerId": req.freelancerId,
                "jobTitle": req.jobTitle,
            },
            automatic_payment_methods={"enabled": True},
        )

        logger.info(f"✅ PaymentIntent created: {intent.id}")
        return JSONResponse(content={
            "status": "success",
            "clientSecret": intent.client_secret,
            "paymentIntentId": intent.id,
        })

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"/create-payment-intent error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════
# STRIPE — Webhook (Stripe calls this automatically)
# ══════════════════════════════════════════════════════════════
@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        logger.error("❌ Invalid Stripe webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # ── Payment Succeeded ──────────────────────────────────────
    if event["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        payment_intent_id = intent["id"]
        amount_paid = intent["amount"] / 100
        freelancer_id = intent["metadata"].get("freelancerId", "")
        job_title = intent["metadata"].get("jobTitle", "")

        logger.info(f"✅ Payment succeeded: {payment_intent_id} — ${amount_paid}")

        # Update payment status in Firestore
        payments = db.collection("payments")
        query = payments.where(
            "paymentIntentId", "==", payment_intent_id
        ).limit(1).get()
        for doc in query:
            doc.reference.update({
                "status": "confirmed",
                "confirmedAt": admin_firestore.SERVER_TIMESTAMP,
            })

        # Notify freelancer
        if freelancer_id:
            db.collection("notifications").add({
                "userId": freelancer_id,
                "senderId": "system",
                "type": "advance_payment",
                "title": "Payment Confirmed 💰",
                "message": f"${amount_paid:.2f} advance confirmed for \"{job_title}\"",
                "isRead": False,
                "createdAt": admin_firestore.SERVER_TIMESTAMP,
            })

    # ── Payment Failed ─────────────────────────────────────────
    elif event["type"] == "payment_intent.payment_failed":
        intent = event["data"]["object"]
        payment_intent_id = intent["id"]
        logger.warning(f"❌ Payment failed: {payment_intent_id}")

        payments = db.collection("payments")
        query = payments.where(
            "paymentIntentId", "==", payment_intent_id
        ).limit(1).get()
        for doc in query:
            doc.reference.update({"status": "failed"})

    # ── Refund ─────────────────────────────────────────────────
    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"]
        logger.info(f"💸 Refund: {charge['id']}")

    return JSONResponse(content={"status": "received"})

# ══════════════════════════════════════════════════════════════
# CHATBOT — Claude AI
# ══════════════════════════════════════════════════════════════
@app.post("/chatbot")
async def chatbot(req: ChatRequest):
    try:
        formatted = [
            {"role": m.role, "content": m.content}
            for m in req.messages
        ]

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=(
                "You are a helpful AI assistant for a freelancing platform. "
                "Help users with job posting, proposal writing, payment questions, "
                "and general freelancing advice. Be concise and friendly."
            ),
            messages=formatted,
        )

        reply = response.content[0].text
        logger.info(f"✅ Claude replied to user {req.user_id}")
        return JSONResponse(content={"reply": reply, "status": "success"})

    except Exception as e:
        logger.error(f"/chatbot error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════
# NOTIFICATIONS — Advance Payment
# ══════════════════════════════════════════════════════════════
@app.post("/notify/advance-payment")
async def notify_advance_payment(req: PaymentNotifyRequest):
    try:
        # Notify freelancer
        db.collection("notifications").add({
            "userId": req.freelancerId,
            "senderId": "system",
            "type": "advance_payment",
            "title": "Advance Payment Received 💰",
            "message": f"You received ${req.amount:.2f} advance for \"{req.jobTitle}\"",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })

        # Notify client
        db.collection("notifications").add({
            "userId": req.clientId,
            "senderId": "system",
            "type": "payment_sent",
            "title": "Payment Sent ✅",
            "message": f"Advance of ${req.amount:.2f} sent for \"{req.jobTitle}\"",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })

        return JSONResponse(content={"status": "success"})
    except Exception as e:
        logger.error(f"/notify/advance-payment error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════
# NOTIFICATIONS — Proposal Status
# ══════════════════════════════════════════════════════════════
@app.post("/notify/proposal-status")
async def notify_proposal_status(req: ProposalNotifyRequest):
    try:
        accepted = req.status == "accepted"
        db.collection("notifications").add({
            "userId": req.freelancerId,
            "senderId": req.clientId,
            "type": "proposal_accepted" if accepted else "proposal_rejected",
            "title": "Proposal Accepted 🎉" if accepted else "Proposal Rejected",
            "message": f"Your proposal on \"{req.jobTitle}\" was {req.status}",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })
        return JSONResponse(content={"status": "success"})
    except Exception as e:
        logger.error(f"/notify/proposal-status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════
# NOTIFICATIONS — New Proposal
# ══════════════════════════════════════════════════════════════
@app.post("/notify/new-proposal")
async def notify_new_proposal(req: ProposalNotifyRequest):
    try:
        db.collection("notifications").add({
            "userId": req.clientId,
            "senderId": req.freelancerId,
            "type": "new_proposal",
            "title": "New Proposal Received 📨",
            "message": f"{req.freelancerName} submitted a proposal on \"{req.jobTitle}\"",
            "jobId": req.jobId,
            "proposalId": req.proposalId,
            "isRead": False,
            "createdAt": admin_firestore.SERVER_TIMESTAMP,
        })
        return JSONResponse(content={"status": "success"})
    except Exception as e:
        logger.error(f"/notify/new-proposal error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) 
