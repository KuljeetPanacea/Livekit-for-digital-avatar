
# agent.py
import asyncio, string, json, os , aiohttp
import dotenv

from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, silero, cartesia

dotenv.load_dotenv(".env")

def load_token():
    try:
        with open("token.txt", "r", encoding="utf-8") as f:
            return f.read().strip()
    except:
        print("⚠ Token file not found!")
        return None

# Load Questions
# ----------------------------------------------------
def load_questions(file_path="questions.json"):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

# ----------------------------------------------------
# Questionnaire State Machine
# ----------------------------------------------------
class QuestionnaireState:
    def __init__(self, questionnaire):
        self.questionnaire = questionnaire
        self.questions = questionnaire["questions"]
        self.current_question = self.questions[0]
        self.project_id = questionnaire["projectId"]
        self.assessment_id = questionnaire["assessmentId"]
        self.questionnaire_id = questionnaire["id"]

    def get_current_question(self):
        return self.current_question

    def update_question(self, new_question):
        self.current_question = new_question
        
   

# Agent Logic (Worker API)
# ----------------------------------------------------
class QuestionnaireAgent(Agent):
    def __init__(self, state, ctx: agents.JobContext):
        super().__init__(instructions="Ask only given questions.")
        self.state = state
        self.ctx = ctx
        self._session = None

    # LiveKit session binding
    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, value):
        self._session = value

    # Send JSON data to frontend
    async def send_data(self, payload: dict):
        await self.ctx.room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8")
        )

  # ---------------------------------------------------------
    # Push user answer to /response API
    # ---------------------------------------------------------
    async def push_to_backend(self, question, user_answer):
        if question["type"] == "multiple_choice" or question["type"] == "single_choice":
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
        url = "http://13.126.133.4:8001/response"
        #return {'response': [{'content': 'Hybrid.', 'intent': 'Good Response', 'role': 'user'}, {'content': 'Hybrid.', 'intent': 'Select and proceed', 'role': 'assistant'}]}
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

    # ---------------------------------------------------------
    # Handle /response output + next-question logic
    # ---------------------------------------------------------
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

        # ---------------- BAD INTENT ----------------
        if intent != "Good Response":
            print("⛔ Bad Intent: Sending clarification back to user")

            await self.send_data({
                "speaker": "assistant",
                "text": content
            })
            await self.session.say(content)
            return False
        
 
        # ---------------- GOOD INTENT ----------------
        print("✅ Good Intent → Calling next-question API")

        cleaned_answer = content.rstrip(".!?").strip()
        formatted = (
            [cleaned_answer] if question["type"] == "multiple_choice" or question["type"] == "single_choice" else [user_msg.get("content")]
        )

        saveresponsePayload = {
            "questionId": question["_id"],
            "choiceValue": [item.strip() for item in cleaned_answer.split(",") if item.strip()] if question["type"] == "multiple_choice" or question["type"] == "single_choice" else [user_msg.get("content")],
            "assessmentId": "",
        }
        
        print("📤 Sending /save-response payload:", saveresponsePayload)
        
        # SaveUrl = "https://13.126.133.4:8000/api/project/userresponse"
        SaveUrl = "https://pi-audit-app.radpretation.ai/api/api/project/userresponse"
        auth_token = load_token()
        headers = {
            "Authorization": f"Bearer {auth_token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.patch(SaveUrl, json=saveresponsePayload, headers=headers) as resp:
                try:
                    save_reply = await resp.json()
                except Exception as e:
                    save_reply = await resp.text()
                    print("📥 Backend SAVE response TEXT:", save_reply)
                    print("❌ Failed to decode save-response:", e)
                    
        current_q = self.state.get_current_question()
        next_payload = {
            "assesmentId": self.state.assessment_id,
            "questionnaireId": self.state.questionnaire_id,
            "currentQuestionId": current_q["_id"],
            "projectId": self.state.project_id,
            "responses": {current_q["_id"]: formatted},
        }

        print("📤 Sending /next-question payload:", next_payload)

        evaluate_url = os.getenv(
            "QUESTIONNAIRE_EVALUATE_URL",
            "https://pi-audit-app.radpretation.ai/api/api/assesment-task/evaluate"
        )
        
        # Call NEXT QUESTION API
        async with aiohttp.ClientSession() as session:
            async with session.post(evaluate_url, json=next_payload, headers=headers) as resp:
                try:
                    backend_reply = await resp.json()

                    print("📥 Backend NEXT response JSON:", backend_reply)
                    next_question = backend_reply.get("data")
                    if next_question:
                        # 1️⃣ Update state
                        self.state.update_question(next_question)

                        # 2️⃣ Send next question to frontend
                        await self.send_data({
                            "type": "next_question",
                            "question": next_question
                        })

                        # 3️⃣ Speak next question
                        q_text = next_question.get("text", "")
                        opts = ", ".join(
                            c["value"] for c in next_question.get("choices", [])
                        )
                        speak_text = f"{q_text}. Options: {opts}" if opts else q_text

                        await self.session.say(speak_text)
                    
                    else :
                        # 4️⃣ Frontend agent bubble
                        await self.send_data({
                            "type": "completed",
                            "message": "No more questions available."
                        })

                except Exception as e:
                    backend_reply = await resp.text()
                    print("📥 Backend NEXT response TEXT:", backend_reply)
                    print("❌ Failed to decode next-question:", e)

        return True

    # ---------------------------------------------------------
    # When user finishes speaking
    # ---------------------------------------------------------
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

        # 🔹 Step 1 — Push user answer to /response
        backend_response = await self.push_to_backend(question, text)

        # 🔹 Step 2 — Process intent + next question
        intent_good = await self.process_backend_response(backend_response, question)

        if not intent_good:
            print("⛔ Waiting for user correction")
            return

        print("➡ Next question loaded successfully")

# ----------------------------------------------------
# Entrypoint for Worker
# ----------------------------------------------------
async def entrypoint(ctx: agents.JobContext):
    print("🚀 Agent initialized inside LiveKit Cloud Worker")
    print(f"📍 Room: {ctx.room.name}")

    # Wait for participant to join
    print("⏳ Waiting for participant...")
    
    questions = load_questions("questions.json")
    
    q_state = QuestionnaireState(questions)

    # Create agent first
    agent = QuestionnaireAgent(q_state, ctx)

    # Create session with proper configuration
    session = AgentSession(
        stt=deepgram.STT(model="nova-2", language="en"),
        tts=cartesia.TTS(
            model="sonic-3",
            voice="6ccbfb76-1fc6-48f7-b71d-91ac6298247b",
            language="en",
            speed=1.2,
        ),
        vad=silero.VAD.load(
            min_speech_duration=0.3,  # Require at least 300ms of speech
            min_silence_duration=0.8,  # Wait 800ms of silence before ending
            ),
        turn_detection=None,
    )

    # Link session to agent
    agent.session = session

    # Start the session with the agent
    print("🎤 Starting agent session...")
    await session.start(room=ctx.room, agent=agent)
    
    print("✅ Agent session started")

   
    # Give a moment for everything to initialize
    await asyncio.sleep(1)

    # Check if anyone is in the room
    participants = list(ctx.room.remote_participants.values())
    print(f"👥 Participants in room: {len(participants)}")
    
    if participants:
        print(f"   - {[p.identity for p in participants]}")

    # Start the questionnaire
    first_q = q_state.get_current_question()
    opts = ", ".join(c["value"] for c in first_q["choices"])

    await agent.send_data({
        "type": "first_question",
        "question": first_q
    })

    print(f"💬 Saying: Let's begin. {first_q['text']}")
    await session.say(f"Let's begin. {first_q['text']}. Options: {opts}.")

    # Keep agent alive
    while True:
        await asyncio.sleep(1)

    print("✅ Questionnaire completed")
    await session.aclose()
    os._exit(0)


# ----------------------------------------------------
# Run Worker
# ----------------------------------------------------
if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            ws_url="wss://voicetest-lzl976kv.livekit.cloud",
            api_key="API7kNixTJ6heAg",
            api_secret="P6oxdtEtLwcUodBsziSl0JN685FNXVzjeeplBkuWAUd",
            # CRITICAL: Tell agent to join rooms automatically
           
        )
    )