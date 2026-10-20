import asyncio

from seruen.services.llm_gateway import transcribe


def test_transcribe_rejects_bad_audio_without_calling_openai():
    assert asyncio.run(transcribe({"audio_b64": "", "mime": "audio/webm"}))["error"] == "bad_audio"
    assert asyncio.run(transcribe({"audio_b64": "not base64 !!!", "mime": "audio/webm"}))["error"] == "bad_audio"
