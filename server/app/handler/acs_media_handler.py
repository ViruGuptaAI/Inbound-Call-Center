"""Handles media streaming to Azure Voice Live API via WebSocket."""

import asyncio
import base64
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Data
from . import SystemPrompt

from .ambient_mixer import AmbientMixer

logger = logging.getLogger(__name__)

# Default chunk size in bytes (100ms of audio at 24kHz, 16-bit mono)
DEFAULT_CHUNK_SIZE = 4800  # 24000 samples/sec * 0.1 sec * 2 bytes
 

# Directory containing per-user info files (e.g. 9876543210.txt)
USER_INFO_DIR = Path(__file__).resolve().parent.parent.parent / "UserInfo"


def load_user_info(caller_id: str) -> str:
    """Load user info from a text file matching the caller's phone number."""
    if not caller_id:
        return ""
    digits = "".join(c for c in caller_id if c.isdigit())
    for candidate in (digits, digits[-10:] if len(digits) > 10 else None):
        if candidate:
            filepath = USER_INFO_DIR / f"{candidate}.txt"
            if filepath.is_file():
                return filepath.read_text(encoding="utf-8")
    return ""


def _enrich_instructions(instructions: str, caller_id: str) -> str:
    """Append caller info from CRM to instructions, replace {Name} placeholder."""
    user_info = load_user_info(caller_id)
    if user_info:
        caller_name = ""
        for line in user_info.splitlines():
            if line.startswith("Name:"):
                caller_name = line.split(":", 1)[1].strip().split()[0]
                break
        if caller_name:
            instructions = instructions.replace("{Name}", caller_name)
        logger.info("Returning user: %s (caller_id=%s)", caller_name or "unknown", caller_id)
        instructions += (
            "\n\nCALLER INFORMATION (from CRM):\n"
            f"{user_info}\n"
            "Use this information to personalise the call. "
            "IMPORTANT: Greet the caller by their first name. "
            "Proactively reference their account details when relevant. "
            "Do NOT read out sensitive details like card numbers unless the caller asks."
        )
    else:
        logger.info("New user (caller_id=%s)", caller_id)
    return instructions


def detect_script_language(text: str) -> str:
    """Detect language from Unicode script in transcript text.
    Returns 'kn' for Kannada, 'hi' for Devanagari (Hindi default),
    'mr' for Marathi (indistinguishable from Hindi by script alone — caller context needed),
    or 'en' if no Indic script is found.
    """
    kannada = 0
    devanagari = 0
    total = 0
    for ch in text:
        cp = ord(ch)
        if ch.isalpha():
            total += 1
            if 0x0C80 <= cp <= 0x0CFF:  # Kannada block
                kannada += 1
            elif 0x0900 <= cp <= 0x097F:  # Devanagari block
                devanagari += 1
    if total == 0:
        return "en"
    if kannada / total > 0.3:
        return "kn"
    if devanagari / total > 0.3:
        return "hi"  # Could be Hindi or Marathi — both use Devanagari
    return "en"


def get_locale_instructions(lang: str, caller_id: str = "") -> str:
    """Return the language-specific prompt enriched with caller info."""
    prompt = SystemPrompt.LOCALE_PROMPTS.get(lang, SystemPrompt.PROMPT_EN)
    return _enrich_instructions(prompt, caller_id)


def session_config(caller_id: str = ""):
    """Returns the initial session configuration (English greeting).

    Voice Live API reference: https://learn.microsoft.com/azure/ai-services/speech-service/voice-live-api-reference-2025-10-01
    """
    instructions = _enrich_instructions(SystemPrompt.PROMPT_EN, caller_id)
    return {
        "type": "session.update",
        "session": {
            "instructions": instructions,  # System prompt guiding agent behaviour
            # "input_audio_format": "pcm16",       # Audio encoding from client (pcm16 | g711_ulaw | g711_alaw). Default: pcm16
            # "input_audio_sampling_rate": 24000,   # Input sample rate in Hz (16000 or 24000). Default: 24000
            # "output_audio_format": "pcm16",       # Audio encoding sent back to client. Also supports pcm16_8000hz, pcm16_16000hz
            "turn_detection": {
                "type": "azure_semantic_vad_multilingual",  # VAD type: server_vad | semantic_vad | azure_semantic_vad | azure_semantic_vad_multilingual
                "threshold": 0.6,              # Speech detection sensitivity (0.0–1.0). Lower = more sensitive. Default: 0.5
                "prefix_padding_ms": 400,      # Audio (ms) to keep before detected speech start, prevents word clipping. Default: 300
                "silence_duration_ms": 400,    # Silence (ms) before end-of-turn is triggered. Lower = faster response. Default: 500
                "speech_duration_ms": 120,     # Min speech (ms) before VAD fires, filters coughs/clicks. Default: 80 (azure_semantic_vad*)
                "remove_filler_words": True,   # Drop "umm", "uh", "hmm" etc. to reduce false barge-ins. Default: False
                "languages": ["en", "hi"],     # Languages for multilingual VAD. Supported: en, es, fr, it, de, ja, pt, zh, ko, hi
                "create_response": True,       # Auto-generate a model response when speech ends. Default: True
                "interrupt_response": True,    # Allow user speech to interrupt (barge-in) the agent's response. Default: True
                "auto_truncate": True,         # On interruption, trim agent's context to what user actually heard. Default: False
                # "end_of_utterance_detection": {  # Semantic EOU — reduces premature end-of-turn without adding latency
                #     "model": "semantic_detection_v1",  # EOU model: semantic_detection_v1 (EN) | semantic_detection_v1_multilingual
                #     "threshold_level": "medium",  # How confident EOU must be: low | medium | high | default. Lower = waits longer for user
                #     "timeout_ms": 400,         # Max wait (ms) for more speech before forcing end-of-turn. Default: 1000
                # },
            },
            "input_audio_transcription": {  # Async STT running alongside audio — transcript ≠ what model hears
                "model": "azure-speech",    # Transcription engine: azure-speech | whisper-1 | gpt-4o-transcribe | gpt-4o-mini-transcribe
                "language": "hi-IN,en-IN,kn-IN,mr-IN",  # BCP-47 locales for recognition (comma-separated for multi-language)
                "phrase_list": [            # Bias words to improve recognition of domain-specific terms
                    "Guptaji", "contoso","गुप्ताजी", "Kavya", "Sanjana","काव्या",
                    "credit card", "debit", "EMI", "CVV", "OTP",
                    "statement", "due date", "minimum due", "outstanding",
                    "late payment fee", "annual fee", "finance charge",
                    "forex markup", "reward points", "cashback",
                    "dispute", "chargeback", "auto-pay",
                    "card block", "replacement card", "add-on card",
                    "credit limit", "waiver", "RBI",
                ],
            },
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},  # Suppress background noise before VAD & model
            "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},    # Remove agent's own voice from mic input (no client AEC needed)
            "voice": {
                # "name": "en-IN-Meera2:DragonHDV2.3Neural",  # Azure HD voice name. See: https://learn.microsoft.com/azure/ai-services/speech-service/language-support
                "name": "en-IN-Diya:DragonHDLatestNeural",
                "type": "azure-standard",   # Voice type: openai | azure-standard | azure-custom | azure-personal
                "temperature": 0.7,         # Voice variability (0.0–1.0). Higher = more expressive intonation. HD voices only
                "rate": "1.1",              # Speaking speed (0.5–1.5). Higher = faster speech
            },
            "temperature": 0.1,            # Model sampling temperature (0.6–1.2). Higher = more creative responses. Default: 0.8
            "max_response_output_tokens": "750",  # Max tokens per response (1–4096 or "inf"). Caps agent verbosity per turn
        },
    }


class ACSMediaHandler:
    """Manages audio streaming between client and Azure Voice Live API."""

    def __init__(self, config, caller_id: str = ""):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        self.model = config["VOICE_LIVE_MODEL"]
        self.api_key = config["AZURE_VOICE_LIVE_API_KEY"]
        self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.caller_id = caller_id
        self.send_queue = asyncio.Queue()
        self.ws = None
        self.send_task = None
        self.incoming_websocket = None
        self.is_raw_audio = True
        self._user_speech_end_ts = None  # timestamp when user stops speaking
        self._transcription_done_ts = None  # timestamp when STT completes
        self._response_created_ts = None  # timestamp when Voice Live starts generating a response
        self._first_audio_latency_logged = False  # ensures we log latency only once per turn
        self._current_lang = "en"  # tracks the active prompt language

        # TTS output buffering for continuous ambient mixing
        self._tts_output_buffer = bytearray()
        self._tts_buffer_lock = asyncio.Lock()
        self._max_buffer_size = 480000  # 10 seconds of audio - large enough for long responses
        self._buffer_warning_logged = False
        self._tts_playback_started = False  # Track if we've started playing TTS
        self._min_buffer_to_start = 9600  # 200ms buffer before starting TTS playback
        
        # Ambient mixer initialization
        self._ambient_mixer: Optional[AmbientMixer] = None
        ambient_preset = config.get("AMBIENT_PRESET", "none")
        if ambient_preset and ambient_preset != "none":
            try:
                self._ambient_mixer = AmbientMixer(preset=ambient_preset)
            except Exception as e:
                logger.error(f"Failed to initialize AmbientMixer: {e}")

    def _generate_guid(self):
        return str(uuid.uuid4())

    async def connect(self):
        """Connects to Azure Voice Live API via WebSocket."""
        endpoint = self.endpoint.rstrip("/")
        model = self.model.strip()
        url = f"{endpoint}/voice-live/realtime?api-version=2025-05-01-preview&model={model}"
        url = url.replace("https://", "wss://")

        headers = {"x-ms-client-request-id": self._generate_guid()}

        if self.client_id:
            async with ManagedIdentityCredential(client_id=self.client_id) as credential:
                token = await credential.get_token(
                    "https://cognitiveservices.azure.com/.default"
                )
                headers["Authorization"] = f"Bearer {token.token}"
        elif self.api_key:
            headers["api-key"] = self.api_key
        else:
            async with DefaultAzureCredential() as credential:
                token = await credential.get_token(
                    "https://cognitiveservices.azure.com/.default"
                )
                headers["Authorization"] = f"Bearer {token.token}"

        self.ws = await ws_connect(url, additional_headers=headers, family=socket.AF_INET)
        logger.info("Voice Live connected (caller_id=%s)", self.caller_id)

        await self._send_json(session_config(self.caller_id))
        await self._send_json({"type": "response.create"})

        asyncio.create_task(self._receiver_loop())
        self.send_task = asyncio.create_task(self._sender_loop())

    async def init_incoming_websocket(self, socket, is_raw_audio=True):
        """Sets up incoming ACS WebSocket."""
        self.incoming_websocket = socket
        self.is_raw_audio = is_raw_audio

    async def audio_to_voicelive(self, audio_b64: str):
        """Queues audio data to be sent to Voice Live API."""
        await self.send_queue.put(
            json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64})
        )

    async def _send_json(self, obj):
        """Sends a JSON object over WebSocket."""
        if self.ws:
            await self.ws.send(json.dumps(obj))

    async def _sender_loop(self):
        """Continuously sends messages from the queue to the Voice Live WebSocket."""
        try:
            while True:
                msg = await self.send_queue.get()
                if self.ws:
                    await self.ws.send(msg)
        except Exception:
            logger.exception("Sender loop error")

    async def _receiver_loop(self):
        """Handles incoming events from the Voice Live WebSocket."""
        try:
            async for message in self.ws:
                event = json.loads(message)
                event_type = event.get("type")

                match event_type:
                    case "session.created":
                        pass

                    case "session.updated":
                        pass

                    case "input_audio_buffer.cleared":
                        pass

                    case "input_audio_buffer.speech_started":
                        await self.stop_audio()

                    case "input_audio_buffer.speech_stopped":
                        self._user_speech_end_ts = time.monotonic()
                        self._first_audio_latency_logged = False

                    case "conversation.item.input_audio_transcription.completed":
                        self._transcription_done_ts = time.monotonic()
                        transcript = event.get("transcript", "")
                        stt_language = event.get("language", "")
                        detected_lang = detect_script_language(transcript)
                        logger.info("USER [script=%s stt=%s]: %s", detected_lang, stt_language, transcript)
                        logger.debug("STT event keys: %s", list(event.keys()))

                        # Determine effective language: prefer STT language tag, fall back to script detection
                        # STT returns "hi-IN", "en-IN", etc. — extract prefix
                        effective_lang = detected_lang
                        if stt_language:
                            stt_prefix = stt_language.split("-")[0].lower()
                            if stt_prefix in SystemPrompt.LOCALE_PROMPTS:
                                effective_lang = stt_prefix

                        # Switch prompt whenever user's language changes
                        # To regional: any utterance with Indic script
                        # Back to English: only if ≥5 English words (avoids false triggers from "okay", "yes", numbers)
                        should_switch = False
                        if effective_lang != self._current_lang and effective_lang in SystemPrompt.LOCALE_PROMPTS:
                            if effective_lang == "en":
                                word_count = len(transcript.strip().split())
                                should_switch = word_count >= 5
                            else:
                                should_switch = True

                        if should_switch:
                            new_instructions = get_locale_instructions(effective_lang, self.caller_id)
                            await self._send_json({
                                "type": "session.update",
                                "session": {"instructions": new_instructions}
                            })
                            logger.info("Switched prompt %s → %s (effective), active for next turn", self._current_lang, effective_lang)
                            self._current_lang = effective_lang

                    case "conversation.item.input_audio_transcription.failed":
                        logger.error("Transcription failed: %s", event.get("error"))

                    case "response.created":
                        self._response_created_ts = time.monotonic()

                    case "response.output_item.added" | "response.audio_transcript.delta":
                        pass

                    case "response.done":
                        response = event.get("response", {})
                        # Log full response.done payload to discover available metrics
                        logger.info("response.done payload: %s", json.dumps(response, indent=2, default=str))
                        if response.get("status") != "completed":
                            logger.error("Response error: %s", json.dumps(response.get("status_details", {})))

                    case "response.audio_transcript.done":
                        transcript = event.get("transcript")
                        logger.info("AGENT: %s", transcript)
                        await self.send_message(
                            json.dumps({"Kind": "Transcription", "Text": transcript})
                        )

                    case "response.audio.delta":
                        # Latency breakdown:
                        #   server_processing = speech_stopped → response.created (EOU + model intake + cold start)
                        #   first_audio       = response.created → first audio delta (LLM generation + TTS, inseparable)
                        #   stt (informational) = speech_stopped → transcription.completed (parallel async pipeline, does NOT gate response)
                        if self._user_speech_end_ts and not self._first_audio_latency_logged:
                            now = time.monotonic()
                            e2e_ms = int((now - self._user_speech_end_ts) * 1000)
                            server_ms = int(((self._response_created_ts or now) - self._user_speech_end_ts) * 1000)
                            first_audio_ms = int((now - (self._response_created_ts or now)) * 1000)
                            stt_ms = int(((self._transcription_done_ts or now) - self._user_speech_end_ts) * 1000) if self._transcription_done_ts else -1
                            logger.info(
                                "Latency breakdown: [e2e=%dms, server_processing=%dms, first_audio=%dms, stt_async=%dms]",
                                e2e_ms, server_ms, first_audio_ms, stt_ms
                            )
                            self._first_audio_latency_logged = True
                        delta = event.get("delta")
                        audio_bytes = base64.b64decode(delta)
                        
                        # Check if ambient mixing is enabled
                        if self._ambient_mixer is not None and self._ambient_mixer.is_enabled():
                            # Buffer TTS for continuous output mixing
                            async with self._tts_buffer_lock:
                                self._tts_output_buffer.extend(audio_bytes)
                                # Warn if buffer is getting large, but NEVER drop audio
                                if len(self._tts_output_buffer) > self._max_buffer_size:
                                    if not self._buffer_warning_logged:
                                        logger.warning(
                                            f"TTS buffer large: {len(self._tts_output_buffer)} bytes. "
                                            "Speech may be delayed but will not be cut."
                                        )
                                        self._buffer_warning_logged = True
                                elif self._buffer_warning_logged and len(self._tts_output_buffer) < self._max_buffer_size // 2:
                                    self._buffer_warning_logged = False  # Reset warning flag
                        else:
                            # No ambient - send immediately (original behavior)
                            if self.is_raw_audio:
                                await self.send_message(audio_bytes)
                            else:
                                await self.voicelive_to_acs(delta)

                    case "error":
                        logger.error("Voice Live error: %s", json.dumps(event))

                    case _:
                        pass
        except Exception:
            logger.exception("Receiver loop error")

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        try:
            await self.incoming_websocket.send(message)
        except Exception:
            logger.exception("Failed to send message")

    async def voicelive_to_acs(self, base64_data):
        """Converts Voice Live audio delta to ACS audio message."""
        try:
            data = {
                "Kind": "AudioData",
                "AudioData": {"Data": base64_data},
                "StopAudio": None,
            }
            await self.send_message(json.dumps(data))
        except Exception:
            logger.exception("Error in voicelive_to_acs")

    async def stop_audio(self):
        """Sends a StopAudio signal to ACS."""
        stop_audio_data = {"Kind": "StopAudio", "AudioData": None, "StopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))
        
        # Clear TTS buffer when user starts speaking
        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def stop_audio_output(self):
        """Cleanup when WebSocket disconnects."""
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

    async def _send_continuous_audio(self, chunk_size: int) -> None:
        """
        Send continuous audio (ambient + TTS if available) back to client.
        
        Called for every incoming audio frame, ensuring continuous output.
        Uses buffered TTS with minimum buffer threshold to prevent mid-word cuts.
        
        Args:
            chunk_size: Size of audio chunk to send (matches incoming frame size)
        """
        if self._ambient_mixer is None or not self._ambient_mixer.is_enabled():
            return  # Ambient disabled, skip
            
        try:
            async with self._tts_buffer_lock:
                buffer_len = len(self._tts_output_buffer)
                
                # Always get a consistent ambient chunk first
                ambient_bytes = self._ambient_mixer.get_ambient_only_chunk(chunk_size)
                
                # Determine if we should play TTS
                should_play_tts = False
                if self._tts_playback_started:
                    # Already playing - continue until buffer empty
                    if buffer_len >= chunk_size:
                        should_play_tts = True
                    elif buffer_len > 0:
                        # Partial buffer but still playing - use what we have
                        should_play_tts = True
                    else:
                        # Buffer empty - stop playback mode
                        self._tts_playback_started = False
                else:
                    # Not yet playing - wait for minimum buffer
                    if buffer_len >= self._min_buffer_to_start:
                        self._tts_playback_started = True
                        should_play_tts = True
                
                if should_play_tts and buffer_len >= chunk_size:
                    # Full TTS chunk available - add TTS on top of ambient
                    tts_chunk = bytes(self._tts_output_buffer[:chunk_size])
                    del self._tts_output_buffer[:chunk_size]
                    
                    # Mix: ambient (constant) + TTS
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mixed = ambient + tts
                    mixed = np.clip(mixed, -0.95, 0.95)  # Soft limit
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                elif should_play_tts and buffer_len > 0:
                    # Partial TTS remaining at end of speech - drain it
                    tts_chunk = bytes(self._tts_output_buffer[:])
                    self._tts_output_buffer.clear()
                    self._tts_playback_started = False
                    
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    
                    # Only mix TTS for the portion we have
                    tts_samples = len(tts_chunk) // 2
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    ambient[:tts_samples] += tts
                    mixed = np.clip(ambient, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                else:
                    # No TTS ready - just send constant ambient
                    output_bytes = ambient_bytes
            
            # Send to client
            if self.is_raw_audio:
                # Web browser - raw bytes
                await self.send_message(output_bytes)
            else:
                # Phone call - JSON wrapped
                output_b64 = base64.b64encode(output_bytes).decode("ascii")
                data = {
                    "Kind": "AudioData",
                    "AudioData": {"Data": output_b64},
                    "StopAudio": None,
                }
                await self.send_message(json.dumps(data))
                
        except Exception:
            logger.exception("Error in _send_continuous_audio")

    async def acs_to_voicelive(self, stream_data):
        """Processes audio from ACS and forwards to Voice Live if not silent."""
        try:
            data = json.loads(stream_data)
            if data.get("kind") == "AudioData":
                audio_data = data.get("audioData", {})
                incoming_data = audio_data.get("data", "")
                
                # Determine chunk size from incoming audio
                if incoming_data:
                    incoming_bytes = base64.b64decode(incoming_data)
                    chunk_size = len(incoming_bytes)
                else:
                    chunk_size = DEFAULT_CHUNK_SIZE
                
                # Send continuous audio back to caller (ambient + TTS mixed)
                await self._send_continuous_audio(chunk_size)
                
                # Forward non-silent audio to Voice Live (existing logic)
                if not audio_data.get("silent", True):
                    await self.audio_to_voicelive(audio_data.get("data"))
        except Exception:
            logger.exception("Error processing ACS audio")

    async def web_to_voicelive(self, audio_bytes):
        """Encodes raw audio bytes and sends to Voice Live API."""
        chunk_size = len(audio_bytes)
        
        # Send continuous audio back to browser (ambient + TTS mixed)
        await self._send_continuous_audio(chunk_size)
        
        # Forward to Voice Live
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self.audio_to_voicelive(audio_b64)
