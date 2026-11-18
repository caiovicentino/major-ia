#!/usr/bin/env python3
"""Mostra as últimas mensagens do chat em texto limpo. Uso: python3 ultimas.py [quantas, padrão 30]"""
import json, os, sys
from pathlib import Path

n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
arq = Path(os.environ.get("MAJOR_CHAT") or Path(__file__).parent / "chat.jsonl").expanduser()
if not arq.exists() or not arq.stat().st_size:
    print("(ainda não há mensagens no chat)")
    sys.exit()
for linha in arq.read_text(encoding="utf-8").splitlines()[-n:]:
    try:
        m = json.loads(linha)
    except Exception:
        continue
    marcas = f" [{', '.join(m['marcas'])}]" if m.get("marcas") else ""
    valor = f" ({m['valor']})" if m.get("valor") else ""
    print(f"{m['hora'][11:19]} {m['autor']}{marcas}{valor}: {m['texto']}")
