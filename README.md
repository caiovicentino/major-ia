# Major IA

Assistente de voz para o Mac que conversa com o [Claude Code](https://claude.com/claude-code). Você fala, o Mac
transcreve ali mesmo, a pergunta vai para o Claude Code e a resposta volta falada, frase por frase.

Dá para usar de dois jeitos:

- **sem anel**, só com o computador: Enter para começar a gravar pelo microfone do Mac, Enter para terminar; ou
  digitando a pergunta;
- **com um anel inteligente SR116** (Bluetooth): segure o botão do anel, fale e solte. Com o anel, o serviço também
  pode coletar batimentos, oxigenação e sono, e falar um resumo diário.

> **Projeto independente.** Este projeto não tem vínculo com o fabricante do anel, com a Anthropic, com o YouTube nem
> com os autores das ferramentas que usa. O protocolo do anel foi entendido por observação e está descrito com
> palavras próprias em [docs/protocolo.md](docs/protocolo.md); nenhum código do aplicativo oficial está aqui.
> **O anel não é um aparelho médico** e nada do que este projeto diz sobre saúde é diagnóstico ou orientação médica.

> **Atenção à segurança.** Um assistente de voz ligado ao Claude Code pode agir no seu computador. O padrão deste
> projeto é o modo restrito. Leia [Privacidade e segurança](#privacidade-e-segurança) antes de ligar o acesso total.

## O que ele faz

Uma conversa típica, no modo sem anel:

```
$ uv run major conversar
Modo restrito: o Claude só lê arquivos, pesquisa e abre apps e links.
Digite a pergunta e Enter, ou só Enter para gravar pelo microfone (Enter de novo para terminar). Para encerrar: sair.
>                                   (Enter: começa a gravar)
gravando… Enter para terminar>      (você fala e aperta Enter)
Você disse (transcrito em 0.6s): Que dia da semana cai o Natal este ano?
  Este ano o Natal cai numa sexta-feira.          (e a frase é falada)
> abre o site da previsão do tempo
  Abri a previsão do tempo no navegador.
```

- Transcrição local com o whisper.cpp: o áudio não sai do seu Mac.
- A resposta começa a ser falada antes de o texto terminar, porque sai frase por frase.
- Números, horas, datas e valores em reais são escritos por extenso antes de virar voz.
- Começar a gravar (ou apertar o botão do anel) corta a fala na hora, como numa conversa.
- No modo de acesso total, tarefas demoradas vão para subagentes em segundo plano; quando terminam, o assistente
  avisa sozinho.
- Opcional: ler o chat de uma live do YouTube para o assistente comentar.
- Opcional, com o anel: coleta de saúde, resumo diário falado e alertas.

## Requisitos

- Mac com Apple Silicon e macOS recente (foi desenvolvido e testado só nesse ambiente).
- [Python](https://www.python.org/) 3.11 ou mais novo e o [uv](https://docs.astral.sh/uv/) (`brew install uv`).
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) (`brew install whisper-cpp`, que traz o comando
  `whisper-cli`) e um modelo no formato ggml. O comando `major preparar` baixa o modelo `small` (uns 470 MB).
- [Claude Code](https://claude.com/claude-code) instalado e com a conta conectada: rode `claude` uma vez no terminal
  e entre na conta. O uso consome a sua assinatura ou os seus créditos.
- [Pocket TTS](https://github.com/kyutai-labs/pocket-tts) para a voz: vem como dependência do projeto; o modelo é
  baixado sozinho no primeiro uso. Se ele falhar, a voz Luciana do macOS assume (instale-a em Ajustes do Sistema,
  Acessibilidade, Conteúdo Falado, se ainda não tiver).
- Só para o anel: um anel SR116 e a biblioteca Opus (`brew install opus`).

## Instalação

```sh
git clone <endereço deste repositório> major-ia
cd major-ia
brew install uv whisper-cpp
uv sync                      # instala o essencial (modo sem anel)
uv run major preparar        # confere tudo e baixa o modelo de transcrição
```

Depois, opcionalmente:

```sh
cp .env.exemplo .env                          # configurações (pastas, voz, saúde); tudo tem padrão
cp instrucoes.exemplo.md instrucoes.md        # o seu jeito de o assistente responder
uv sync --extra anel                          # se você tem o anel (mais: brew install opus)
```

Na primeira gravação o macOS pede permissão de microfone para o aplicativo de terminal. Aceite, ou libere em Ajustes
do Sistema, Privacidade e Segurança, Microfone.

## Como usar sem o anel

```sh
uv run major conversar            # Enter grava pelo microfone do Mac; ou digite a pergunta
uv run major conversar --texto    # só digitar: o microfone nunca é aberto
uv run major conversar --mudo     # não fala; a resposta só aparece na tela
uv run major ponte "que horas são?"   # uma pergunta avulsa, e encerra
```

No `conversar`:

| Você faz | Acontece |
| --- | --- |
| Enter com a linha vazia | começa a gravar pelo microfone do Mac |
| Enter de novo | termina, transcreve e envia |
| digita um texto e Enter | envia o texto como se tivesse sido falado |
| `sair` ou Ctrl+D | encerra |

Começar a gravar ou enviar um texto novo corta a fala que estiver em curso. A gravação é apagada assim que é
transcrita.

Acrescente `--acesso-total` para o assistente poder agir no computador (veja os avisos abaixo) e `--modelo sonnet`,
por exemplo, para escolher o modelo.

## Como usar com o anel

1. Feche o aplicativo oficial do anel no celular (ou desligue o Bluetooth do celular): o anel só fala com um aparelho
   por vez.
2. Deixe o anel perto do Mac. O macOS costuma parear sozinho; se pedir, aceite.
3. Suba o serviço:

```sh
uv run major status               # teste rápido: bateria e gravações guardadas
uv run major servir --claude      # serviço de voz, modo restrito
```

Com o serviço no ar:

- **segure o botão do anel, fale e solte**: o serviço grava pelo microfone do Mac enquanto o botão está apertado (o
  anel só avisa "apertou" e "soltou"), transcreve e manda ao Claude. Um toque de som confirma que ouviu;
- **quatro toques, fale, mais quatro toques**: grava pelo microfone do próprio anel, mesmo longe do Mac. O anel guarda
  a gravação e o serviço a busca quando ele voltar a ficar perto. É mais lento e a qualidade é pior;
- apertar o botão durante uma resposta cala a voz.

Outros comandos do anel: `major baixar`, `major saude`, `major medir`, `major medicao-automatica`,
`major saude-historico`. Cada um explica as opções com `--help`.

Para o serviço ficar sempre no ar, o caminho usual no macOS é um agente do `launchd`; como isso depende da sua
máquina, não vem pronto aqui.

## Como funciona por dentro

```
botão do anel ou Enter ─▶ microfone do Mac ─▶ WAV 16 kHz ─▶ whisper.cpp ─▶ texto
                                                                           │
alto-falante ◀─ Pocket TTS ◀─ frases, uma a uma ◀─ Claude Code (claude -p, sessão contínua)
```

| Arquivo | Papel |
| --- | --- |
| `major/local.py` | modo sem anel: o laço do teclado |
| `major/microfone.py` | grava a entrada de áudio padrão do Mac |
| `major/transcricao.py` | chama o `whisper-cli` |
| `major/ponte.py` | a ponte com o Claude Code |
| `major/voz.py` | fila de fala, números por extenso, Pocket TTS, Kokoro e a voz de reserva |
| `major/servico.py` | serviço do anel: botão, gravações do anel, coleta de saúde |
| `major/link.py`, `major/protocol.py`, `major/audio.py` | Bluetooth, formato das mensagens e áudio Opus do anel |
| `major/saude.py`, `major/acompanhamento.py` | registro de saúde, resumo diário e alertas |
| `major/config.py` | pastas e opções, lidas do ambiente ou do `.env` |
| `live/coletor.py`, `live/ultimas.py` | chat da live do YouTube |

**A ponte.** Em vez de subir um `claude` a cada pergunta, o projeto mantém um processo do Claude Code aberto em modo
headless (`claude -p --input-format stream-json --output-format stream-json`). Cada pergunta é uma linha escrita na
entrada do processo; a resposta chega em pedaços e é cortada em frases, que vão para a fila de fala assim que fecham.
A conversa é retomada entre execuções por até seis horas de inatividade; depois disso começa uma nova. Cada mensagem
leva a data e a hora atuais.

**A voz.** Uma fila única toca as frases em ordem. O áudio é reamostrado para a taxa em que a placa de som já está, e
a saída é aberta só enquanto há o que falar.

**A pasta de trabalho.** O Claude Code roda na pasta `trabalho/` do projeto (mude com `MAJOR_PROJETO`). Um arquivo
`CLAUDE.md` nessa pasta vale como memória do projeto para o Claude Code, do jeito normal dele.

**As instruções.** O projeto já diz ao assistente para responder curto, sem formatação e com números por extenso. O
que você escrever em `instrucoes.md` é somado a isso. Veja [instrucoes.exemplo.md](instrucoes.exemplo.md).

## Opcional: chat da live do YouTube

Para o assistente ler e comentar o chat de uma transmissão sua:

```sh
python3 live/coletor.py ID_DO_VIDEO     # deixe rodando num terminal durante a live
python3 live/ultimas.py 30              # mostra as últimas trinta mensagens
```

O coletor lê o chat público sem login, do mesmo jeito que a página do chat faz, e grava uma mensagem por linha em
`live/chat.jsonl`. Ele não usa a API oficial: pode parar de funcionar se o YouTube mudar a página, e o uso é por sua
conta, respeitando os termos do YouTube. Use com as suas próprias lives.

Para o assistente consultar o chat, diga isso a ele em `instrucoes.md` (o exemplo traz o trecho) e, no modo restrito,
libere o comando no `.env`:

```
MAJOR_PERMITIR=Bash(python3 /caminho/completo/para/major-ia/live/ultimas.py *)
```

O arquivo do chat tem nomes e mensagens de outras pessoas. Ele já está no `.gitignore`; não publique. E lembre que o
texto do chat é escrito por desconhecidos: veja o aviso sobre instruções escondidas, logo abaixo.

## Opcional: saúde (só com o anel)

Com `major servir`, o serviço coleta de trinta em trinta minutos a bateria, os passos, uma medição de batimentos e
oxigenação e o que o anel mediu sozinho (inclusive o sono), e grava tudo em `dados/saude.jsonl`. Com `--claude`,
ele também fala um resumo diário (padrão: nove e meia da manhã) e alertas quando várias leituras seguidas ficam fora
de limites conservadores.

- Desligar a coleta: `--saude-minutos 0` ou `MAJOR_SAUDE_MINUTOS=0`.
- Desligar o resumo: `--resumo-saude nao`. Desligar os alertas: `--sem-alertas-saude`.
- Ver o que foi gravado: `uv run major saude-historico`.
- Os limites dos alertas estão no começo de `major/acompanhamento.py`.

Os dados de saúde ficam só no seu Mac, mas os números do resumo e dos alertas são enviados ao Claude para ele montar
a frase. Se isso não serve para você, deixe o resumo e os alertas desligados.

Repetindo: o anel não é um aparelho médico, as leituras erram, e a ausência de alerta não quer dizer que está tudo
bem. Em caso de sintoma, procure atendimento.

## Privacidade e segurança

**O que sai do seu computador.** O áudio é transcrito localmente e a voz é gerada localmente. O texto transcrito, e
tudo o que o Claude Code ler para responder, vai para a Anthropic pela sua conta do Claude Code, nas condições dela.
O coletor do chat fala com o YouTube.

**O que fica guardado.** Na pasta `dados/`: as gravações do serviço do anel (`voz/` e `gravacoes/`), o histórico do
que você disse (`comandos.jsonl`), a sessão da conversa e os dados de saúde. O modo sem anel apaga cada gravação
depois de transcrever. O Claude Code guarda as conversas dele na pasta de configuração dele. Nada disso vai para o
git: confira o `.gitignore` antes de publicar um fork e nunca publique a pasta `dados/`.

**Os dois modos.**

- *Restrito* (padrão): o Claude só pode ler arquivos, pesquisar na internet e abrir apps, arquivos e links. Não
  edita, não apaga e não roda outros comandos. Ainda assim ele **lê** arquivos do seu Mac e manda o conteúdo para a
  Anthropic quando acha necessário para responder.
- *Acesso total* (`--acesso-total`): roda com `--dangerously-skip-permissions`. O assistente pode executar qualquer
  comando, criar, alterar e apagar arquivos, instalar programas e usar as suas contas conectadas, **sem pedir
  permissão a ninguém**. É o modo usado na demonstração, e é perigoso.

**Riscos do acesso total, sem rodeios.**

- *Erro de transcrição.* O reconhecimento de voz troca palavras. "Apaga o rascunho" pode chegar como outra coisa.
- *Alguém falando perto do microfone*, ou o áudio de um vídeo, vira comando.
- *Instruções escondidas.* O assistente lê páginas da internet, arquivos e, se você ligar, o chat da live. Qualquer
  um pode escrever ali um texto mandando o assistente fazer algo. Com acesso total, uma instrução dessas pode virar
  ação no seu computador. Ler o chat de uma live com acesso total ligado é a combinação mais arriscada deste projeto.
- *Não há desfazer.* O que foi apagado ou enviado, foi.

**Como restringir.**

- Fique no modo restrito e libere só o que precisar com `MAJOR_PERMITIR`.
- Aponte `MAJOR_PROJETO` para uma pasta só com o que o assistente pode ver. Isso organiza, mas não é uma barreira: o
  Claude Code consegue ler fora dela.
- Para uma barreira de verdade, rode num usuário do macOS separado, sem os seus documentos e sem as suas contas, ou
  numa máquina dedicada.
- As instruções do projeto mandam o assistente confirmar antes de apagar, sobrescrever, enviar, publicar ou gastar.
  Isso é um pedido ao modelo, não uma trava: ajuda, mas pode falhar.
- Tenha backup. Não deixe o serviço com acesso total rodando sem ninguém por perto.
- Se usar numa transmissão, lembre que a tela e a voz podem mostrar dados seus.

## Limitações conhecidas

- Só macOS com Apple Silicon. A ligação com o anel usa recursos específicos do macOS.
- Só português do Brasil: instruções, números por extenso e voz.
- O anel SR116 não transmite o microfone ao vivo: com o botão, quem grava é o microfone do Mac.
- A ligação Bluetooth com o anel cai com frequência (a cada poucos minutos, no nosso uso); o serviço reconecta sozinho,
  mas um comando pode se perder.
- Uma gravação longa no anel o deixa mudo por minutos enquanto é transferida.
- O protocolo do anel foi entendido por observação de um único modelo e firmware; em outros pode ser diferente.
- Quatro bytes de cada registro de saúde do anel ainda não foram decifrados.
- No modo sem anel não há "segurar uma tecla": é Enter para começar e Enter para terminar.
- A voz Kokoro é opcional e traz dependências com licença GPL (veja abaixo).
- Interromper uma resposta não cancela tarefas que já estão em segundo plano.
- O coletor do chat depende do formato atual da página do YouTube.

## Solução de problemas

| Sintoma | O que fazer |
| --- | --- |
| "Não achei o comando claude" | Instale o Claude Code e rode `claude` uma vez para entrar na conta. |
| "Não consegui falar com o Claude." | Rode `claude -p "oi"` no terminal: se pedir login ou der erro, resolva lá primeiro. |
| "falta o whisper.cpp" ou "falta o modelo" | `brew install whisper-cpp` e `uv run major preparar`. |
| "O microfone não abriu" ou só silêncio | Libere o microfone para o seu terminal em Ajustes do Sistema, Privacidade e Segurança, e confira a entrada escolhida em Som. |
| "Não ouvi nenhuma fala" | Fale mais perto; gravações com menos de um segundo são descartadas. |
| A voz não sai ou sai a do macOS | Na primeira vez o Pocket TTS baixa o modelo (precisa de internet). A mensagem no terminal diz o motivo da troca. |
| A voz lê algo estranho | Diga em `instrucoes.md` como o assistente deve escrever aquilo. |
| "Faltam as bibliotecas do anel" | `uv sync --extra anel`. |
| "libopus não encontrada" | `brew install opus`. |
| "Não achei o anel" | Feche o aplicativo oficial no celular, aproxime o anel, confira a bateria. |
| O toque no anel mexe o cursor do Mac | Isso só para enquanto `major servir` está no ar. |
| "Já existe um serviço de voz no ar" | Só um `major servir` por vez; encerre o outro. |
| O assistente diz que não tem permissão | É o modo restrito. Libere a ferramenta com `MAJOR_PERMITIR` ou, ciente dos riscos, use `--acesso-total`. |
| Mudei `instrucoes.md` e nada mudou | Encerre e abra de novo: a conversa nova já começa com as instruções novas. |

## Licença

O código deste projeto está sob a [licença MIT](LICENSE). As dependências têm as licenças delas; as da instalação
padrão e do extra `anel` são permissivas ou LGPL (MIT, BSD, Apache 2.0; `num2words` é LGPL). O extra `kokoro` instala
`phonemizer-fork` e usa o `espeak-ng`, que são GPL versão 3: quem redistribuir um pacote com esse extra embutido
precisa seguir a GPL. Os modelos (Whisper, Pocket TTS, Kokoro) têm licenças próprias; confira antes de uso comercial.

## Testes

```sh
uv run python -m unittest discover -s testes -v
```

Os testes não usam o anel, o Bluetooth, o microfone, o alto-falante nem a sua conta do Claude.
