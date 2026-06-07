import os
import sys
import io
import wave
import requests
import json
import numpy as np
import sounddevice as sd
from azure.identity import DefaultAzureCredential

# Configuration
ENDPOINT = os.environ.get("FoundryEndpointForMAI", "https://centralindiafoundry.cognitiveservices.azure.com/")


def _get_token() -> str:
    """Get a bearer token for Azure Cognitive Services using DefaultAzureCredential."""
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    return token.token


def transcribe_audio(audio_file_path: str, language: str = None) -> dict:
    """
    Transcribe an audio file using the MAI-transcribe-1.5 model via Azure Speech Services.

    Args:
        audio_file_path: Path to the audio file (wav, mp3, ogg, flac, etc.)
        language: Optional BCP-47 language code (e.g., "en-IN", "hi-IN"). 
                  If None, the model auto-detects the language.

    Returns:
        dict with transcription results
    """
    url = f"{ENDPOINT.rstrip('/')}/speechtotext/transcriptions:transcribe?api-version=2025-10-15"

    headers = {
        "Authorization": f"Bearer {_get_token()}",
    }

    definition = {
        "enhancedMode": {
            "enabled": True,
            "model": "MAI-transcribe-1.5",
            "task": "transcribe"
        }
    }

    if language:
        definition["locales"] = [language]

    with open(audio_file_path, "rb") as audio_file:
        files = {
            "audio": (os.path.basename(audio_file_path), audio_file, "audio/wav"),
            "definition": (None, json.dumps(definition), "application/json"),
        }
        response = requests.post(url, headers=headers, files=files)

    response.raise_for_status()
    return response.json()


def transcribe_from_buffer(audio_buffer: io.BytesIO, language: str = None) -> dict:
    """Transcribe audio from an in-memory WAV buffer."""
    url = f"{ENDPOINT.rstrip('/')}/speechtotext/transcriptions:transcribe?api-version=2025-10-15"

    headers = {
        "Authorization": f"Bearer {_get_token()}",
    }

    definition = {
        "enhancedMode": {
            "enabled": True,
            "model": "MAI-transcribe-1.5",
            "task": "transcribe"
        }
    }

    if language:
        definition["locales"] = [language]

    audio_buffer.seek(0)
    files = {
        "audio": ("recording.wav", audio_buffer, "audio/wav"),
        "definition": (None, json.dumps(definition), "application/json"),
    }
    response = requests.post(url, headers=headers, files=files)
    response.raise_for_status()
    return response.json()


def record_from_mic(sample_rate: int = 16000) -> io.BytesIO:
    """Record audio from microphone until Enter is pressed. Returns a WAV buffer."""
    print("🎤 Recording... Press Enter to stop.")

    recording = []

    def callback(indata, frames, time_info, status):
        recording.append(indata.copy())

    stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16", callback=callback)
    stream.start()
    input()  # Wait for Enter
    stream.stop()
    stream.close()

    audio_data = np.concatenate(recording, axis=0)
    print(f"   Recorded {len(audio_data) / sample_rate:.1f}s of audio.")

    # Write to in-memory WAV
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(audio_data.tobytes())
    buffer.seek(0)
    return buffer


def translate_audio(audio_file_path: str, target_language: str = "en") -> dict:
    """
    Translate an audio file to a target language using MAI-transcribe-1.5.

    Args:
        audio_file_path: Path to the audio file
        target_language: Target language code for translation (default: "en")

    Returns:
        dict with translation results
    """
    url = f"{ENDPOINT.rstrip('/')}/speechtotext/transcriptions:transcribe?api-version=2025-10-15"

    headers = {
        "Authorization": f"Bearer {_get_token()}",
    }

    definition = {
        "enhancedMode": {
            "enabled": True,
            "model": "MAI-transcribe-1.5",
            "task": "translate",
            "targetLocale": target_language,
        }
    }

    with open(audio_file_path, "rb") as audio_file:
        files = {
            "audio": (os.path.basename(audio_file_path), audio_file, "audio/wav"),
            "definition": (None, json.dumps(definition), "application/json"),
        }
        response = requests.post(url, headers=headers, files=files)

    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    lang = sys.argv[1] if len(sys.argv) > 1 else None

    print("MAI-transcribe-1.5 — Live Mic Transcription")
    print("=" * 45)
    if lang:
        print(f"Language: {lang}")
    else:
        print("Language: auto-detect")
    print()

    try:
        while True:
            audio_buffer = record_from_mic()
            print("   Transcribing...")
            result = transcribe_from_buffer(audio_buffer, language=lang)

            if "combinedPhrases" in result:
                for phrase in result["combinedPhrases"]:
                    print(f"\n📝 {phrase.get('text', '')}\n")
            else:
                print(f"\n   Response: {json.dumps(result, indent=2, ensure_ascii=False)}\n")

            print("-" * 45)
    except KeyboardInterrupt:
        print("\n\nStopped.")