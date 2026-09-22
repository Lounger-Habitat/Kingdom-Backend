"""Short NPC voice drafts. Audio is transient; credentials never reach Unity."""
import base64
import binascii
import io
import wave

import httpx
from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from account.authentication import GameJWTAuthentication


class SpeechThrottle(UserRateThrottle):
    rate = "6/min"


def validate_wav(encoded):
    if not isinstance(encoded, str) or len(encoded) > 1_300_000:
        raise ValueError("录音过大，请控制在 30 秒内")
    try:
        data = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() != 16000 or audio.getcomptype() != "NONE"
                    or not 1600 <= frames <= 480000):
                raise ValueError("需要 0.1 至 30 秒的 16kHz 单声道 PCM WAV 录音")
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError("录音数据不完整")
    except (binascii.Error, wave.Error, EOFError) as exc:
        raise ValueError("录音格式无效") from exc
    return data


@api_view(["POST"])
@authentication_classes([GameJWTAuthentication])
@permission_classes([IsAuthenticated])
@throttle_classes([SpeechThrottle])
def transcribe(request):
    try:
        data = validate_wav(request.data.get("audio"))
    except ValueError as exc:
        return Response({"success": False, "message": str(exc)}, status=400)
    key = getattr(settings, "MIMO_API_KEY", "")
    if not key:
        return Response({"success": False, "message": "语音识别暂未配置，请使用文字输入"}, status=503)
    try:
        result = httpx.post(
            "https://api.xiaomimimo.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "mimo-v2.5-asr",
                "messages": [{"role": "user", "content": [{
                    "type": "input_audio",
                    "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")},
                }]}],
                "asr_options": {"language": "zh"},
                "stream": False,
            },
            timeout=httpx.Timeout(25, connect=5),
        )
        result.raise_for_status()
        text = result.json()["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            return Response({"success": False, "message": "没有识别到文字，请重新录音"}, status=422)
        return Response({"success": True, "text": text.strip()})
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        # Never echo upstream bodies, which may include credentials or audio.
        return Response({"success": False, "message": "语音识别暂时失败，请重试或使用文字输入"}, status=502)
