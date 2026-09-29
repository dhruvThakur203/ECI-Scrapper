"""Voice-captcha transcription tuned for ECI spoken letters/digits.

Earlier Whisper failures were mostly post-processing, not the model:
- raw text like "M. R. T. 3. U. K." or "double you why" was cleaned poorly
- CUDA without cuBLAS crashed; CPU int8 is the reliable default here
"""

from __future__ import annotations

import logging
import os
import re
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger("eci")

_whisper_model = None

# Spoken forms → single captcha character (lowercase).
_WORD_TO_CHAR: dict[str, str] = {
    "zero": "0",
    "oh": "o",
    "o": "o",
    "one": "1",
    "won": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
    "niner": "9",
    "aye": "a",
    "hey": "a",
    "be": "b",
    "bee": "b",
    "see": "c",
    "sea": "c",
    "si": "c",
    "dee": "d",
    "the": "d",  # occasional mishear of "dee"
    "ee": "e",
    "eff": "f",
    "if": "f",
    "gee": "g",
    "jee": "g",
    "aitch": "h",
    "h": "h",
    "eye": "i",
    "i": "i",
    "jay": "j",
    "kay": "k",
    "el": "l",
    "ell": "l",
    "em": "m",
    "am": "m",
    "en": "n",
    "and": "n",
    "pee": "p",
    "pea": "p",
    "cue": "q",
    "queue": "q",
    "are": "r",
    "or": "r",
    "ess": "s",
    "as": "s",
    "tee": "t",
    "tea": "t",
    "you": "u",
    "yu": "u",
    "vee": "v",
    "we": "v",
    "doubleyou": "w",
    "double": "w",  # often "double you" → handled in phrase pass
    "ex": "x",
    "eggs": "x",
    "why": "y",
    "wine": "y",
    "zed": "z",
    "zee": "z",
    "zebra": "z",
}


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    from faster_whisper import WhisperModel

    model_id = os.environ.get(
        "CAPTCHA_WHISPER_MODEL",
        "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    )
    # Prefer CPU: Windows often lacks cublas64_12.dll even when a GPU is present.
    device = os.environ.get("CAPTCHA_WHISPER_DEVICE", "cpu")
    compute = os.environ.get("CAPTCHA_WHISPER_COMPUTE", "int8")
    logger.info("Loading whisper %s (%s/%s)...", model_id, device, compute)
    try:
        _whisper_model = WhisperModel(model_id, device=device, compute_type=compute)
    except Exception as exc:
        if device != "cpu":
            logger.warning("Whisper %s failed (%s); falling back to cpu/int8", device, exc)
            _whisper_model = WhisperModel(model_id, device="cpu", compute_type="int8")
        else:
            raise
    logger.info("Whisper loaded")
    return _whisper_model


def _wav_to_float32(wav_bytes: bytes) -> np.ndarray:
    tmp = Path(os.environ.get("TEMP", ".")) / "_eci_captcha.wav"
    tmp.write_bytes(wav_bytes)
    with wave.open(str(tmp), "rb") as handle:
        sr = handle.getframerate()
        nch = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        samples = samples.reshape(-1, nch).mean(axis=1)
    if sr != 16000:
        from scipy.signal import resample

        samples = resample(samples, int(len(samples) * 16000 / sr)).astype(np.float32)
    # Mild gain helps quiet TTS clips.
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0:
        samples = samples * min(0.95 / peak, 3.0)
    return samples


def _normalize_phrase(raw: str) -> str:
    text = raw.lower()
    text = text.replace("double you", " w ")
    text = text.replace("double-u", " w ")
    text = text.replace("double u", " w ")
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_spoken_captcha(raw: str) -> str:
    """Turn Whisper transcript into a 4–6 char captcha string."""
    text = _normalize_phrase(raw)
    if not text:
        return ""

    # Already looks like "m r t 3 u k" or "m-r-t-3-u-k"
    spaced = re.findall(r"[a-z0-9]", text.replace("-", " "))
    # If Whisper returned a tight string of alnum only, keep letters/digits in order.
    compact = re.sub(r"[^a-z0-9]", "", text)

    chars: list[str] = []
    tokens = text.replace("-", " ").split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"for", "as", "is", "of", "the"} and i + 1 < len(tokens):
            # Skip "X for Xray" filler: take previous already added, skip next word name
            i += 2
            continue
        if len(tok) == 1 and tok.isalnum():
            chars.append(tok)
            i += 1
            continue
        if tok.isdigit():
            chars.extend(list(tok))
            i += 1
            continue
        mapped = _WORD_TO_CHAR.get(tok)
        if mapped:
            chars.append(mapped)
            i += 1
            continue
        # "em" glued etc.
        if len(tok) <= 3 and tok.isalpha():
            # Prefer whole-token map already tried; fall back to first letter only if single syllable junk
            chars.append(tok[0])
            i += 1
            continue
        i += 1

    result = "".join(chars)
    # Prefer spaced single-char parse when it looks like a spelled captcha.
    if 4 <= len(spaced) <= 6 and (not result or abs(len(spaced) - 5) <= abs(len(result) - 5)):
        # If spaced chars are all single alnum already collected via findall
        spaced_joined = "".join(spaced)
        if 4 <= len(spaced_joined) <= 6:
            result = spaced_joined

    if not result and 4 <= len(compact) <= 6:
        result = compact

    if len(result) > 6:
        result = result[:6]
    return result


def transcribe_captcha_wav(wav_bytes: bytes) -> tuple[str, str]:
    """Return (cleaned_answer, raw_transcript)."""
    model = _get_whisper_model()
    audio = _wav_to_float32(wav_bytes)

    prompts = [
        "Spell captcha characters one by one: A B C D E F G H I J K L M N O P Q R S T U V W X Y Z 0 1 2 3 4 5 6 7 8 9",
        "Individual letters and digits spoken one at a time:",
        None,
    ]
    best = ("", "")
    for prompt in prompts:
        kwargs = dict(
            language="en",
            beam_size=5,
            best_of=5,
            temperature=0.0,
            vad_filter=False,  # short clips; VAD often chops letters
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        if prompt:
            kwargs["initial_prompt"] = prompt
        segments, _info = model.transcribe(audio, **kwargs)
        raw = " ".join(seg.text.strip() for seg in segments).strip()
        cleaned = parse_spoken_captcha(raw)
        logger.debug("Whisper prompt=%r raw=%r → %r", prompt, raw, cleaned)
        if 4 <= len(cleaned) <= 6:
            return cleaned, raw
        if len(cleaned) > len(best[0]):
            best = (cleaned, raw)
    return best
