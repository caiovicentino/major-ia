"""Configuração: tudo o que depende da máquina de quem usa vem de variáveis de ambiente ou do arquivo .env.

O arquivo .env fica na raiz do projeto (copie de .env.exemplo) e nunca vai para o git. Uma variável já definida no
ambiente vale mais que a do arquivo.
"""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _carregar_env(arquivo: Path) -> None:
    try:
        linhas = arquivo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        nome, _, valor = linha.partition("=")
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        os.environ.setdefault(nome.strip(), valor)


_carregar_env(RAIZ / ".env")


def _pasta(variavel: str, padrao: Path) -> Path:
    valor = os.environ.get(variavel, "").strip()
    return Path(valor).expanduser() if valor else padrao


# Onde ficam os dados de quem usa: áudios, histórico de comandos, sessão da conversa, saúde. Nunca vai para o git.
DATA = _pasta("MAJOR_DADOS", RAIZ / "dados")

# Pasta em que o Claude Code roda. É dela que ele lê o CLAUDE.md e a memória do projeto, e é nela que ele trabalha.
PROJETO = _pasta("MAJOR_PROJETO", RAIZ / "trabalho")

# Modelo do whisper.cpp (formato ggml). Baixe com: major preparar
WHISPER_MODELO = _pasta("MAJOR_WHISPER_MODELO", DATA / "modelos" / "ggml-small.bin")
WHISPER_MODELO_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"

# Instruções suas para o assistente, somadas às do projeto (ver instrucoes.exemplo.md). Opcional.
INSTRUCOES = _pasta("MAJOR_INSTRUCOES", RAIZ / "instrucoes.md")

# Modelo que os subagentes devem usar no modo de acesso total (ex.: sonnet). Vazio: o padrão da sua conta.
MODELO_SUBAGENTE = os.environ.get("MAJOR_MODELO_SUBAGENTE", "").strip()

# Regras a mais de ferramentas liberadas no modo restrito, separadas por vírgula. Ex.: Bash(python3 live/ultimas.py *)
PERMITIR = [regra.strip() for regra in os.environ.get("MAJOR_PERMITIR", "").split(",") if regra.strip()]
