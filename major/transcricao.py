"""Transcrição no próprio Mac com o whisper.cpp (comando whisper-cli). O áudio não sai da máquina."""
import shutil
import subprocess
from pathlib import Path

from . import config


def problema() -> str | None:
    """O que falta para transcrever, em uma frase, ou None se está tudo pronto."""
    if not shutil.which("whisper-cli"):
        return "falta o whisper.cpp (instale com: brew install whisper-cpp)"
    if not config.WHISPER_MODELO.exists():
        return f"falta o modelo de transcrição em {config.WHISPER_MODELO} (baixe com: major preparar)"
    return None


def transcrever(path: Path, idioma: str = "pt") -> str | None:
    """Texto falado no WAV (16 kHz, mono), ou None se nada foi reconhecido ou o transcritor não está instalado."""
    falta = problema()
    if falta:
        print(f"Não dá para transcrever: {falta}.", flush=True)
        return None
    run = subprocess.run(["whisper-cli", "-m", str(config.WHISPER_MODELO), "-f", str(path), "-l", idioma, "-nt", "-np"],
                         capture_output=True, text=True, check=False)
    if run.returncode != 0:
        print(f"O whisper-cli falhou: {run.stderr.strip()[-300:]}", flush=True)
        return None
    return " ".join(line.strip() for line in run.stdout.splitlines() if line.strip()) or None


def baixar_modelo() -> Path:
    """Baixa o modelo de transcrição (cerca de 470 MB) para a pasta de dados, se ainda não estiver lá."""
    destino = config.WHISPER_MODELO
    if destino.exists():
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(".parcial")
    subprocess.run(["curl", "-L", "--fail", "--progress-bar", "-o", str(parcial), config.WHISPER_MODELO_URL], check=True)
    parcial.replace(destino)
    return destino
