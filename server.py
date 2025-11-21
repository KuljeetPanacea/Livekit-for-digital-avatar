# # server.py
# from fastapi import FastAPI, Request, WebSocket
# import json, asyncio, subprocess, os, sys
# from fastapi.middleware.cors import CORSMiddleware

# os.environ["PYTHONIOENCODING"] = "utf-8"
# os.environ["PYTHONLEGACYWINDOWSSTDIO"] = "1"

# app = FastAPI()
# connected_clients = set()

# # Allow CORS
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# agent_process = None


# @app.websocket("/ws/logs")
# async def websocket_logs(ws: WebSocket):
#     await ws.accept()
#     connected_clients.add(ws)
#     print("🟢 WebSocket connected.")

#     try:
#         while True:
#             await asyncio.sleep(1)
#     except:
#         pass
#     finally:
#         connected_clients.remove(ws)
#         print("🔴 WebSocket disconnected.")


# async def broadcast_log(message: str):
#     for ws in list(connected_clients):
#         try:
#             await ws.send_text(message)
#         except:
#             connected_clients.remove(ws)


# @app.post("/upload-questions")
# async def upload_questions(request: Request):
#     global agent_process

#     data = await request.json()
#     with open("questions.json", "w", encoding="utf-8") as f:
#         json.dump(data, f, indent=2)

#     # stop existing agent
#     if agent_process and agent_process.poll() is None:
#         print("🛑 Stopping existing agent...")
#         agent_process.terminate()
#         await asyncio.sleep(1)

#     print("🚀 Starting new LiveKit agent...")

#     agent_process = subprocess.Popen(
#         [sys.executable, "agent.py", "console"],
#         stdout=subprocess.PIPE,
#         stderr=subprocess.STDOUT,
#         text=True,
#         encoding="utf-8",
#         errors="replace",
#         bufsize=1
#     )

#     asyncio.create_task(stream_agent_logs(agent_process))
#     return {"message": "Agent started"}

# async def stream_agent_logs(process):
#     """
#     Only extract USER messages from:
#         "User said: ..."
#     And forward SYSTEM events from agent.py such as:
#         {"speaker":"system","type":"first_question","question":{...}}
#         {"speaker":"system","type":"next_question","question":{...}}

#     Assistant messages ARE NOT extracted from logs.
#     They are sent manually from 'agent.py'.
#     """

#     noise_keys = [
#         "[audio]",
#         "rtc-version",
#         "using proactor",
#         "traceback",
#         "job runner",
#         "starting worker",
#         "initializing",
#         "none of pytorch",
#         "debug livekit.plugins.turn_detector",
#         "eou prediction",
#         "livekit.plugins.turn_detector",
#     ]

#     for raw_line in process.stdout:
#         if raw_line is None:
#             continue

#         print("RAW_LOG:", raw_line.rstrip())  # Debug log

#         clean = raw_line.strip()
#         low = clean.lower()

#         if not clean:
#             continue

#         # Skip noisy internal logs
#         if any(n in low for n in noise_keys):
#             continue

#         # ------------------------------------------------
#         # USER SPEECH (actual STT output)
#         # ------------------------------------------------
#         if "user said:" in low:
#             parts = low.split("user said:", 1)
#             text = parts[1].strip() if len(parts) > 1 else ""

#             if text:
#                 await broadcast_log(json.dumps({
#                     "speaker": "user",
#                     "text": text
#                 }))
#             continue

#         # ------------------------------------------------
#         # SYSTEM EVENTS FROM agent.py
#         # (You will send these manually inside agent.py)
#         #
#         # Example expected line from agent.py:
#         # SYSTEM_EVENT: {"type":"first_question", "question": {...}}
#         # ------------------------------------------------
#         if clean.startswith("SYSTEM_EVENT:"):
#             try:
#                 payload = clean.replace("SYSTEM_EVENT:", "").strip()
#                 await broadcast_log(payload)
#                 continue
#             except Exception as e:
#                 print("SYSTEM_EVENT parse error:", e)
#                 continue

#         # ------------------------------------------------
#         # Everything else ignored
#         # ------------------------------------------------
#         continue
# server.py



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
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "https://cloud.livekit.io")
API_KEY = os.getenv("LIVEKIT_API_KEY", "API7kNixTJ6heAg")
API_SECRET = os.getenv("LIVEKIT_API_SECRET", "P6oxdtEtLwcUodBsziSl0JN685FNXVzjeeplBkuWAUd")


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