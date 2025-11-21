import subprocess
import sys
from fastapi import FastAPI, Request ,BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
import json ,os
import dotenv
from dotenv import load_dotenv
load_dotenv()   
app = FastAPI()

# Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173","https://0.0.0.0:5173",
    "http://0.0.0.0:5173",
    "https://0.0.0.0:5174",
    "https://0.0.0.0:4173",
    "https://0.0.0.0:4174",
    "http://0.0.0.0:3000",],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = "https://voicetest-lzl976kv.livekit.cloud"
API_KEY = "API7kNixTJ6heAg"
API_SECRET = "P6oxdtEtLwcUodBsziSl0JN685FNXVzjeeplBkuWAUd"


# --------------------------------------------------
# GET TOKEN for Frontend
# --------------------------------------------------
@app.get("/get-token")
def get_token(identity: str = "anonymous-user", room: str = "survey-room"):
    """
    Create a valid JWT token for LiveKit Cloud.
    """

    token = (
        api.AccessToken(API_KEY, API_SECRET)
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )

    return {
        "token": token,
        "url": LIVEKIT_URL,
        "room": room,
    }


# --------------------------------------------------
# UPLOAD QUESTIONS
# --------------------------------------------------
@app.post("/upload-questions")
async def upload_questions(request: Request):
    data = await request.json()
    token = request.headers.get("X-Auth-Token")

    print("🔍 Received token:", token)
    
    # Save token
    with open("token.txt", "w", encoding="utf-8") as f:
        f.write(token)


    # Save questions
    with open("questions.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # DETACHED background run (Windows Safe)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    subprocess.Popen(
        [
            sys.executable,
            "survey_agent.py",
            "connect",
            "--room",
            "survey-room"
        ],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True
    )

    return {"message": "Questions updated and agent started"}