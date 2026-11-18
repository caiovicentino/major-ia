"""Coletor do chat da live do YouTube: grava cada mensagem como uma linha em chat.jsonl.
Leitura anônima do chat público (página live_chat + endpoint get_live_chat), sem cookies nem credenciais.
Usa a visão "Chat ao vivo" (todas as mensagens), não a "Principais mensagens". Só a biblioteca padrão do Python.

Uso: python3 coletor.py ID_DO_VIDEO     (o ID é o que vem depois de "v=" no endereço da live)
O arquivo fica ao lado deste script; para mudar, defina a variável MAJOR_CHAT com o caminho desejado.
O chat traz nomes e mensagens de outras pessoas: não publique o arquivo."""
import json, os, re, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

ARQUIVO = Path(os.environ.get("MAJOR_CHAT") or Path(__file__).parent / "chat.jsonl").expanduser()
if len(sys.argv) < 2:
    sys.exit(__doc__)
VIDEO = sys.argv[1]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
CABECALHOS = {"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"}


def log(texto):
    print(f"{datetime.now().isoformat(timespec='seconds')} {texto}", flush=True)


def baixar(url, corpo=None):
    cab = dict(CABECALHOS)
    if corpo is not None:
        cab["Content-Type"] = "application/json"
        corpo = json.dumps(corpo).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=corpo, headers=cab), timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def proxima(conts):
    for c in conts or []:
        for v in c.values():
            if isinstance(v, dict) and v.get("continuation"):
                return v["continuation"], int(v.get("timeoutMs", 3000))
    return None, 3000


def iniciar():
    """Lê a página do chat e devolve (chave, versão, continuação de todas as mensagens, ações iniciais)."""
    html = baixar(f"https://www.youtube.com/live_chat?is_popout=1&v={VIDEO}")
    m = re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});\s*</script>', html, re.S)
    if not m:
        raise RuntimeError("página do chat sem ytInitialData")
    dados = json.loads(m.group(1))
    lc = dados.get("contents", {}).get("liveChatRenderer")
    if not lc:
        raise RuntimeError("chat indisponível nesta página (live sem chat, privada ou encerrada)")
    chave = (re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html) or [None, ""])[1]
    versao = (re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html) or re.search(r'"clientVersion":"([^"]+)"', html) or [None, "2.20261101.00.00"])[1]
    cont, _ = proxima(lc.get("continuations"))
    try:
        itens = lc["header"]["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"]
        todas = itens[-1]["continuation"]["reloadContinuationData"]["continuation"]   # último item = "Chat ao vivo" (tudo)
        cont = todas or cont
    except Exception:
        log("não achei o seletor 'Chat ao vivo'; ficando com a visão padrão")
    if not cont:
        raise RuntimeError("página do chat sem continuação")
    return chave, versao, cont, lc.get("actions", [])


def texto_de(msg):
    if not msg:
        return ""
    if "simpleText" in msg:
        return msg["simpleText"]
    partes = []
    for r in msg.get("runs", []):
        if "text" in r:
            partes.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            partes.append((e.get("shortcuts") or [""])[0] if e.get("isCustomEmoji") else e.get("emojiId", ""))
    return "".join(partes)


TIPOS = {
    "liveChatTextMessageRenderer": None,
    "liveChatPaidMessageRenderer": "super chat",
    "liveChatPaidStickerRenderer": "super sticker",
    "liveChatMembershipItemRenderer": "novo membro",
    "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer": "presente de assinaturas",
}


def registros(acoes):
    for acao in acoes or []:
        item = (acao.get("addChatItemAction") or {}).get("item") or {}
        for tipo, marca in TIPOS.items():
            r = item.get(tipo)
            if not r:
                continue
            if "header" in r and "liveChatSponsorshipsHeaderRenderer" in r.get("header", {}):
                h = r["header"]["liveChatSponsorshipsHeaderRenderer"]
                r = {**h, "id": r.get("id"), "timestampUsec": r.get("timestampUsec"), "message": h.get("primaryText")}
            marcas = []
            for b in r.get("authorBadges", []):
                b = b.get("liveChatAuthorBadgeRenderer", {})
                icone = b.get("icon", {}).get("iconType", "")
                if icone == "OWNER": marcas.append("dono")
                elif icone == "MODERATOR": marcas.append("moderador")
                elif icone == "VERIFIED": marcas.append("verificado")
                elif "customThumbnail" in b: marcas.append("membro")
            if marca:
                marcas.append(marca)
            texto = texto_de(r.get("message")) or texto_de(r.get("headerSubtext")) or texto_de(r.get("headerPrimaryText"))
            try:
                hora = datetime.fromtimestamp(int(r["timestampUsec"]) / 1e6).astimezone()
            except Exception:
                hora = datetime.now().astimezone()
            reg = {"hora": hora.isoformat(timespec="seconds"), "autor": texto_de(r.get("authorName")),
                   "texto": texto, "marcas": marcas, "tipo": tipo, "id": r.get("id")}
            valor = texto_de(r.get("purchaseAmountText"))
            if valor:
                reg["valor"] = valor
            yield reg


vistos = set()
if ARQUIVO.exists():
    for linha in ARQUIVO.read_text(encoding="utf-8").splitlines():
        try:
            vistos.add(json.loads(linha).get("id"))
        except Exception:
            pass
total = 0


def gravar(acoes):
    global total
    novos = [r for r in registros(acoes) if r["id"] not in vistos]
    novos.sort(key=lambda r: r["hora"])
    if novos:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        with ARQUIVO.open("a", encoding="utf-8") as f:
            for r in novos:
                vistos.add(r["id"])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        total += len(novos)


log(f"coletor iniciado para o vídeo {VIDEO}; {len(vistos)} mensagens já gravadas")
while True:
    try:
        chave, versao, cont, iniciais = iniciar()
        gravar(iniciais)
        log(f"conectado ao chat (todas as mensagens); {total} gravadas até agora")
        aviso, falhas = time.time(), 0
        url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false" + (f"&key={chave}" if chave else "")
        while True:
            try:
                resp = json.loads(baixar(url, {"context": {"client": {"clientName": "WEB", "clientVersion": versao, "hl": "pt", "gl": "BR"}}, "continuation": cont}))
                falhas = 0
            except Exception as erro:
                falhas += 1
                log(f"falha na consulta ({erro!r})")
                if falhas >= 5:
                    break
                time.sleep(3)
                continue
            lc = (resp.get("continuationContents") or {}).get("liveChatContinuation")
            if not lc:
                log("o chat não devolveu continuação; reconectando")
                break
            gravar(lc.get("actions"))
            nova, espera = proxima(lc.get("continuations"))
            if not nova:
                log("sem próxima continuação; reconectando")
                break
            cont = nova
            if time.time() - aviso > 60:
                log(f"vivo; {total} mensagens gravadas nesta execução")
                aviso = time.time()
            time.sleep(max(1.0, min(espera / 1000, 3.0)))
    except Exception as erro:
        log(f"falha ao conectar ({erro!r})")
    time.sleep(10)
