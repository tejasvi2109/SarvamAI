import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sarvamai import SarvamAI


ROOT = Path(__file__).resolve().parent
MAX_BODY_BYTES = 8 * 1024 * 1024
SUPPORTED_AUDIO_TYPES = {
    "audio/webm": ("webm", "audio/webm"),
    "audio/mp4": ("m4a", "audio/mp4"),
    "audio/ogg": ("ogg", "audio/ogg"),
    "audio/wav": ("wav", "audio/wav"),
    "audio/mpeg": ("mp3", "audio/mpeg"),
}
SUPPORTED_TTS_LANGUAGES = {
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN",
    "od-IN", "pa-IN", "ta-IN", "te-IN",
}

SYSTEM_PROMPT = """You are Money Guide, a calm and careful financial education assistant.

Help with financial concepts, practical budgeting, and understanding loan terms. You may calculate examples from numbers the user provides; state assumptions and distinguish estimates from lender offers or approvals. Ask for the user's country before discussing jurisdiction-specific rules, taxes, or products. Do not invent current rates, rules, or product terms.

For investing, explain general concepts, risks, diversification, time horizons, and fees. Do not give personalized buy, sell, or hold recommendations, select specific securities or funds for the user, or promise returns. For personalized investment decisions, suggest speaking with a qualified financial adviser in the user's jurisdiction.

Never guarantee loan approval, rates, or investment outcomes. Do not request passwords, account numbers, government ID numbers, or other credentials. Keep replies concise and natural to speak aloud, and reply in the language the user used when possible."""


def load_local_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "SARVAM_API_KEY":
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


load_local_env()
api_key = os.environ.get("SARVAM_API_KEY")
if not api_key:
    raise SystemExit("Add your Sarvam API key to .env before starting the app.")

client = SarvamAI(api_subscription_key=api_key)


class MoneyGuideHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        page = (ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self):
        if self.path != "/api/voice":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "Recording is empty or too large. Try a shorter recording."})
            return

        try:
            body = json.loads(self.rfile.read(length))
            audio_b64 = body["audio_base64"]
            audio_type = body["audio_type"].split(";", 1)[0].lower()
            if audio_type not in SUPPORTED_AUDIO_TYPES:
                self.send_json(415, {"error": "This browser's audio format is not supported."})
                return
            audio_bytes = base64.b64decode(audio_b64, validate=True)
            if not audio_bytes:
                self.send_json(400, {"error": "No audio was recorded."})
                return

            history = body.get("messages", [])
            if not isinstance(history, list):
                raise ValueError("Invalid conversation history")
            safe_history = []
            for message in history[-12:]:
                if not isinstance(message, dict):
                    raise ValueError("Invalid conversation history")
                role = message.get("role")
                content = message.get("content")
                if role not in ("user", "assistant") or not isinstance(content, str):
                    raise ValueError("Invalid conversation history")
                safe_history.append({"role": role, "content": content[:4000]})

            extension, mime_type = SUPPORTED_AUDIO_TYPES[audio_type]
            transcription = client.speech_to_text.transcribe(
                file=(f"recording.{extension}", audio_bytes, mime_type),
                model="saaras:v4",
                mode="transcribe",
                language_code="unknown",
            )
            transcript = transcription.transcript.strip()
            if not transcript:
                self.send_json(422, {"error": "I couldn't hear any words. Please try again."})
                return

            answer = client.chat.completions(
                model="sarvam-105b",
                temperature=0.2,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    *safe_history,
                    {"role": "user", "content": transcript},
                ],
            ).choices[0].message.content.strip()

            language_code = getattr(transcription, "language_code", None)
            if language_code not in SUPPORTED_TTS_LANGUAGES:
                language_code = "en-IN"
            speech = client.text_to_speech.convert(
                text=answer,
                model="bulbul:v4-flash",
                language_code=language_code,
                speaker="simran",
                output_audio_codec="wav",
                temperature=0.3,
            )
            self.send_json(
                200,
                {
                    "transcript": transcript,
                    "reply": answer,
                    "audio_base64": speech.audios[0],
                    "audio_type": "audio/wav",
                    "language_code": language_code,
                },
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_json(400, {"error": "The voice request was not valid. Please record again."})
        except Exception as error:
            print(f"Sarvam request failed: {type(error).__name__}")
            self.send_json(502, {"error": "Sarvam could not complete that turn. Please try again."})

    def send_json(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format_string, *args):
        # Keep request logs free of user audio, transcripts, and credentials.
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), MoneyGuideHandler)
    print("Money Guide is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
