"""Decodifica os quadros Opus do anel (40 bytes, 20 ms, 16 kHz mono) para WAV."""
import array
import ctypes
import ctypes.util
import wave
from pathlib import Path

RATE = 16000


def _libopus() -> ctypes.CDLL:
    for candidate in ("/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib", ctypes.util.find_library("opus")):
        if candidate and Path(candidate).exists():
            lib = ctypes.CDLL(candidate)
            lib.opus_decoder_create.restype = ctypes.c_void_p
            lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
            lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                        ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
            return lib
    raise RuntimeError("libopus não encontrada (instale com: brew install opus)")


def decode(frames: list[bytes]) -> array.array:
    lib = _libopus()
    error = ctypes.c_int(0)
    decoder = lib.opus_decoder_create(RATE, 1, ctypes.byref(error))
    if error.value != 0:
        raise RuntimeError(f"opus_decoder_create falhou ({error.value})")
    pcm = array.array("h")
    buffer = (ctypes.c_int16 * 5760)()
    try:
        for frame in frames:
            n = lib.opus_decode(decoder, frame, len(frame), buffer, 5760, 0)
            if n > 0:
                pcm.extend(buffer[:n])
    finally:
        lib.opus_decoder_destroy(decoder)
    return pcm


def has_speech(path: Path, min_seconds: float = 1.0, min_rms: float = 250) -> bool:
    """Descarta toques acidentais e silêncio, que o transcritor preenche com texto inventado."""
    with wave.open(str(path), "rb") as src:
        pcm = array.array("h", src.readframes(src.getnframes()))
    if len(pcm) < RATE * min_seconds:
        return False
    loudest = max((sum(x * x for x in pcm[i:i + RATE]) / RATE) ** 0.5 for i in range(0, len(pcm) - RATE + 1, RATE))
    return loudest >= min_rms


def duracao(path: Path) -> float:
    """Segundos de áudio no WAV."""
    with wave.open(str(path), "rb") as src:
        return src.getnframes() / src.getframerate()


def write_wav(frames: list[bytes], path: Path) -> float:
    """Grava o WAV e devolve a duração em segundos."""
    pcm = decode(frames)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())
    return len(pcm) / RATE
