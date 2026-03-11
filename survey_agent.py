import asyncio, json, os, aiohttp
import dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, silero, cartesia
import json
import base64
from Crypto.Cipher import AES
from Crypto.Hash import MD5
from Crypto.Util.Padding import pad, unpad

dotenv.load_dotenv(".env")
print("ENV: TOKEN_FILE =", os.getenv("TOKEN_FILE"))
print("ENV: QUESTIONS_FILE =", os.getenv("QUESTIONS_FILE"))
print("ENV: TARGET_ROOM =", os.getenv("TARGET_ROOM"))
SECRET_KEY = "simple-demo-key-32chars"


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    """OpenSSL-compatible key derivation (EVP_BytesToKey)"""
    m = []
    i = 0
    while len(b''.join(m)) < (key_len + iv_len):
        md = MD5.new()
        data = password + salt
        if i > 0:
            data = m[i - 1] + password + salt
        md.update(data)
        m.append(md.digest())
        i += 1
    ms = b''.join(m)
    return ms[:key_len], ms[key_len:key_len + iv_len]


def encrypt_payload(data: dict, secret: str) -> str:
    """Encrypt data compatible with CryptoJS.AES.decrypt"""
    # Convert dict to JSON string
    plaintext = json.dumps(data)
    
    # Generate random salt
    salt = os.urandom(8)
    
    # Derive key and IV using EVP_BytesToKey
    key, iv = _evp_bytes_to_key(
        secret.encode('utf-8'),
        salt,
        key_len=32,
        iv_len=16
    )
    
    # Encrypt using AES-256-CBC
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(plaintext.encode('utf-8'), AES.block_size))
    
    # Format: "Salted__" + salt + ciphertext (OpenSSL format)
    openssl_format = b'Salted__' + salt + ciphertext
    
    # Base64 encode
    return base64.b64encode(openssl_format).decode('utf-8')


def decrypt_payload(ciphertext: str, secret: str) -> dict:
    """Decrypt data from CryptoJS.AES.encrypt"""
    # Decode base64
    encrypted_data = base64.b64decode(ciphertext)
    
    # Check for "Salted__" prefix
    if encrypted_data[:8] != b'Salted__':
        raise ValueError("Invalid encrypted format - missing 'Salted__' prefix")
    
    # Extract salt and ciphertext
    salt = encrypted_data[8:16]
    ciphertext_bytes = encrypted_data[16:]
    
    # Derive key and IV
    key, iv = _evp_bytes_to_key(
        secret.encode('utf-8'),
        salt,
        key_len=32,
        iv_len=16
    )
    
    # Decrypt
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = unpad(cipher.decrypt(ciphertext_bytes), AES.block_size)
    
    # Parse JSON
    return json.loads(decrypted.decode('utf-8'))


def load_token():
    token_file = os.getenv("TOKEN_FILE")
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        print(f"⚠ Token file not found: {token_file}")
        return None

def load_questions():
    questions_file = os.getenv("QUESTIONS_FILE")
    try:
        with open(questions_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠ Questions file not found: {questions_file}")
        return None

# ----------------------------------------------------
# Questionnaire State Machine
# ----------------------------------------------------
class QuestionnaireState:
    def __init__(self, questionnaire):
        self.questionnaire = questionnaire
        self.questions = questionnaire["questions"]
        self.current_question = self.questions[0]
        self.project_id = questionnaire.get("projectId")
        self.assessment_id = questionnaire.get("assessmentId")
        self.questionnaire_id = questionnaire.get("id")
        self.phase = questionnaire.get("phase", "")
        self.current_audit_stage = questionnaire.get("currentAuditStage", "")

    def is_scoping_questionnaire(self):
        """Scoping questionnaires live in project.scopingQSTRNRData; assessment questionnaires in assessment.task."""
        if self.phase == "Scoping":
            return True
        if self.phase == "Assessment" and self.assessment_id:
            return False
        return self.current_audit_stage != "assessment"

    async def cleanup_files(self):
        token_file = os.getenv("TOKEN_FILE")
        questions_file = os.getenv("QUESTIONS_FILE")

        print("🧹 Cleaning up files after session close...")

        for f in [token_file, questions_file]:
            try:
                if f and os.path.exists(f):
                    os.remove(f)
                    print(f"   ✔ Deleted {f}")
                else:
                    print(f"   ⚠ File not found: {f}")
            except Exception as e:
                print(f"   ❌ Failed to delete {f}: {e}")
    async def on_session_closed(self, session, reason, error):
        print(f"🔌 on_session_closed → reason={reason}, error={error}")
        await self.cleanup_files()

    def get_current_question(self):
        return self.current_question

    def update_question(self, new_question):
        self.current_question = new_question

# ----------------------------------------------------
# Agent Logic
# ----------------------------------------------------
class QuestionnaireAgent(Agent):
    def __init__(self, state, ctx: agents.JobContext, auth_token):
        super().__init__(instructions="Ask only given questions.")
        self.state = state
        self.ctx = ctx
        self.auth_token = auth_token  # ✅ Store token
        self._session = None

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, value):
        self._session = value

    async def send_data(self, payload: dict):
        await self.ctx.room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8")
        )

    async def push_to_backend(self, question, user_answer):
        if question["type"] in ["multiple_choice", "single_choice"]:
            possible = [c["value"] for c in question["choices"]]
        else:
            possible = []

        payload = {
            "question": question["text"],
            "responsetype": question["type"],
            "possible_responses": possible,
            "user_comment": user_answer,
            "additional_knowledge": "",
            "question_explaination": "",
            "chatHistory": []
        }

        print("\n📤 Sending to backend:", payload)
        url = "http://127.0.0.1:8001/response"
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                try:
                    backend_json = await resp.json()
                    print("\n📥 Backend response JSON:", backend_json)
                    return backend_json
                except Exception as e:
                    text = await resp.text()
                    print("\n⚠ Backend returned TEXT:", text)
                    print("⚠ JSON parse error:", e)
                    return None

    async def process_backend_response(self, backend_json, question):
        if backend_json is None:
            await self.session.say("Sorry, something went wrong.")
            await self.send_data({
                "speaker": "assistant",
                "text": "Sorry, something went wrong."
            })
            return False

        res_list = backend_json.get("response", [])
        assistant_msg = next((r for r in res_list if r["role"] == "assistant"), None)
        user_msg = next((r for r in res_list if r["role"] == "user"), None)

        if not assistant_msg:
            print("❌ Assistant message missing")
            await self.session.say("Sorry, could not generate a response.")
            return False

        content = assistant_msg.get("content", "")
        intent = user_msg.get("intent") if user_msg else "unknown"

        print("🔍 Intent:", intent)

        # Bad intent - ask for clarification
        if intent != "Good Response":
            print("⛔ Bad Intent: Sending clarification back to user")
            await self.send_data({
                "speaker": "assistant",
                "text": content
            })
            await self.session.say(content)
            return False

        

        cleaned_answer = content.rstrip(".!?").strip()
        
        if question["type"] in ["multiple_choice", "single_choice"]:
            formatted = [cleaned_answer]
        else:
            formatted = [user_msg.get("content")]

        saveresponsePayload = {
            "questionId": question["_id"],
            "choiceValue": (
                [item.strip() for item in cleaned_answer.split(",") if item.strip()]
                if question["type"] in ["multiple_choice", "single_choice"]
                else [user_msg.get("content")]
            ),
            "assessmentId": self.state.assessment_id,
            "projectId": self.state.project_id,
            "questionnaireId": self.state.questionnaire_id
        }
        encrypt_saveresponsePayload = encrypt_payload(saveresponsePayload, SECRET_KEY)
        
        
        SaveUrl = ""
        if self.state.is_scoping_questionnaire():
            SaveUrl = "http://localhost:8000/api/project/userresponse"
        elif self.state.assessment_id:
            SaveUrl = "http://localhost:8000/api/assesment-task/userresponse"
        else:
            SaveUrl = "http://localhost:8000/api/project/userresponse"
      
        headers = {
            "Authorization": f"Bearer {self.auth_token}",   
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.patch(SaveUrl, json={"payload": encrypt_saveresponsePayload}, headers=headers) as resp:
                try:
                    save_reply = await resp.json()
                    if isinstance(save_reply, dict) and "payload" in save_reply:
                # Response is encrypted, decrypt it
                        decrypted_save = decrypt_payload(save_reply["payload"], SECRET_KEY)
                        print("📥 Backend SAVE response JSON (decrypted):", decrypted_save)
                    else:
                        print("📥 Backend SAVE response JSON (plain):", save_reply)
                
                except Exception as e:
                    save_reply = await resp.text()
                    decrypted_save = decrypt_payload(save_reply["payload"], SECRET_KEY)
                    print("📥 Backend SAVE response TEXT:", decrypted_save)
                    print("❌ Failed to decode save-response:", e)

        # Get next question
        current_q = self.state.get_current_question()
        next_payload = {
            "assesmentId": self.state.assessment_id,
            "questionnaireId": self.state.questionnaire_id,
            "currentQuestionId": current_q["_id"],
            "projectId": self.state.project_id,
            "responses": {current_q["_id"]: formatted},
        }

        print("📤 Sending /next-question payload:", next_payload)
        encrypted = encrypt_payload(next_payload, SECRET_KEY)

        # Evaluate: single endpoint; backend routes by role (AE POC → project, AE Internal Assessor → assessment)
        evaluate_url = os.getenv(
            "QUESTIONNAIRE_EVALUATE_URL",
            "http://localhost:8000/api/assesment-task/evaluate"
        )  

        async with aiohttp.ClientSession() as session:
            async with session.post(evaluate_url, json={"payload": encrypted}, headers=headers) as resp:
                try:     
                    encrypted_resp = await resp.json()
                    if "payload" not in encrypted_resp:
                        print("❌ Backend returned non-encrypted response:", encrypted_resp)
                        return True
                    backend_reply = decrypt_payload(encrypted_resp["payload"], SECRET_KEY)
                    print("📥 Backend NEXT response JSON:", backend_reply)
                    
                    next_question = backend_reply.get("data")
                    while next_question and next_question.get("type") == "file_type":
                        always_id = next_question.get("alwaysGoTo")
                        if not always_id:
                            print("❌ file_type has no alwaysGoTo, cannot skip!")
                            await self.session.say("File upload is required. Stopping flow.")
                            return True
                        force_next_payload = {
                            "assesmentId": self.state.assessment_id,
                            "questionnaireId": self.state.questionnaire_id,
                            "currentQuestionId": next_question["_id"],
                            "projectId": self.state.project_id,
                            "responses":{next_question["_id"]: []},
                        }
                        encrypted_skip = encrypt_payload(force_next_payload, SECRET_KEY)
                        async with aiohttp.ClientSession() as session2:
                            async with session2.post(evaluate_url, json={"payload": encrypted_skip}, headers=headers) as resp2:
                                try:
                                    skip_json = await resp2.json()
                                    if "payload" not in skip_json:
                                        print("❌ Invalid skip response:", skip_json)
                                        return True
                                    skip_reply = decrypt_payload(skip_json["payload"], SECRET_KEY)
                                    print("📥 Skip-evaluate reply:", skip_reply)
                                    next_question = skip_reply.get("data")
                                except:
                                    text = await resp2.text()
                                    print("⚠ Skip-evaluate TEXT:", text)
                                    return True
                        # Update state
                    if next_question:
                        self.state.update_question(next_question)
                        print("➡ Next question updated in state.")
                        
                        # Send next question to frontend
                        await self.send_data({
                            "type": "next_question",
                            "question": next_question
                        })
                        
                        q_text = next_question.get("text", "")
                        opts = ", ".join(c["value"] for c in next_question.get("choices", []))
                        speak_text = f"{q_text}. Options: {opts}" if opts else q_text
                        await self.session.say(speak_text)
                    
                    else:
                        await self.send_data({
                            "type": "completed",
                            "message": "No more questions available."
                        })
                        await self.session.say("Thank you for completing the questionnaire.")
                        
                except Exception as e:
                    backend_reply = await resp.text()
                    print("📥 Backend NEXT response TEXT:", backend_reply)
                    print("❌ Failed to decode next-question:", e)

        return True

    async def on_user_turn_completed(self, turn_ctx, *, new_message):
        if not new_message or not new_message.content:
            return

        raw = new_message.content[0]
        text = raw.text.strip() if hasattr(raw, "text") else str(raw)

        print(f"User said: {text}")

        # Send to frontend chat
        await self.send_data({
            "speaker": "user",
            "text": text
        })

        # Get current question
        question = self.state.get_current_question()

        # Push user answer to backend
        backend_response = await self.push_to_backend(question, text)

        # Process intent + next question
        intent_good = await self.process_backend_response(backend_response, question)

        if not intent_good:
            print("⛔ Waiting for user correction")
            return

        print("➡ Next question loaded successfully")

# ----------------------------------------------------
# Entrypoint
# ----------------------------------------------------
async def entrypoint(ctx: agents.JobContext):  
    print("🚀 Agent initialized")
    print(f"📍 Room: {ctx.room.name}")
    
    # ✅ Load from environment variables
    target_room = os.getenv("TARGET_ROOM")
    
    # Only proceed if this is the correct room
    if target_room and ctx.room.name != target_room:
        print(f"⚠ Skipping room {ctx.room.name} (target: {target_room})")
        return
    
    print("⏳ Loading questions and token...")
    
    questions = load_questions()
    auth_token = load_token()
    
    if not questions:
        print("❌ Failed to load questions")
        return
    
    if not auth_token:
        print("❌ Failed to load auth token")
        return
    
    q_state = QuestionnaireState(questions)
    agent = QuestionnaireAgent(q_state, ctx, auth_token)

    # Create session
    session = AgentSession(
        stt=deepgram.STT(model="nova-2", language="en"),
        tts=cartesia.TTS(
            model="sonic-3",
            voice="6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
            language="en",
        ),
        vad=silero.VAD.load(
            min_speech_duration=0.3,
            min_silence_duration=0.8,
        ),
        turn_detection=None,
    )

    agent.session = session

    print("🎤 Starting agent session...")
    await session.start(room=ctx.room, agent=agent)
    print("✅ Agent session started")

    await asyncio.sleep(1)

    # Check participants
    participants = list(ctx.room.remote_participants.values())
    print(f"👥 Participants in room: {len(participants)}")
    
    if participants:
        print(f"   - {[p.identity for p in participants]}")

    # Start questionnaire
    first_q = q_state.get_current_question()
    opts = ", ".join(c["value"] for c in first_q.get("choices", []))

    await agent.send_data({
        "type": "first_question",
        "question": first_q
    })

    speak_text = f"Let's begin. {first_q['text']}"
    if opts:
        speak_text += f". Options: {opts}"
    
    print(f"💬 Saying: {speak_text}")
    await session.say(speak_text)
    
    # Keep agent alive
    while True:
        await asyncio.sleep(1)

# ----------------------------------------------------
# Run Worker
# ----------------------------------------------------
if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url=os.getenv("LIVEKIT_WS_URL", "wss://cloud.livekit.io"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
            
        )
    )

