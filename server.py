import subprocess
import sys
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
import json, os
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "wss://cloud.livekit.io")
API_KEY = os.getenv("LIVEKIT_API_KEY")
API_SECRET = os.getenv("LIVEKIT_API_SECRET")

active_sessions = {}

@app.get("/get-token")
def get_token(identity: str, room: str):
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


@app.post("/upload-questions")
async def upload_questions(request: Request):
    data = await request.json()
    token = request.headers.get("X-Auth-Token")
    room_name = data.get("roomName")
    
    if not room_name:
        return {"error": "roomName is required"}, 400

    file_path = f"questions_{room_name}.json"
    token_path = f"token_{room_name}.txt"

    with open(file_path, "w") as fq:
        json.dump(data, fq, indent=2)

    with open(token_path, "w") as ft:
        ft.write(token)

    env = dict(os.environ)
    env.update({
        "QUESTIONS_FILE": file_path,
        "TOKEN_FILE": token_path,
        "TARGET_ROOM": room_name
    })

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    subprocess.Popen(
        [
            sys.executable,
            "survey_agent.py",
            "connect",
            "--room", room_name
        ],
        env=env,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )

    return {"message": "Agent started", "room": room_name}
