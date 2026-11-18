# O protocolo do anel SR116, como este projeto o entende

Este texto descreve, com palavras próprias, como o projeto conversa com o anel. É o resultado de observar o
comportamento de um único anel SR116 e de experimentar com ele. Não é documentação oficial, não contém código do
aplicativo do fabricante e pode estar errado ou incompleto. O projeto não tem vínculo com o fabricante. O anel não é
um aparelho médico.

Mande ao seu anel só o que você entende. Em especial, existe um comando de operação do aparelho que reinicia, desliga
ou restaura de fábrica (apagando tudo); este projeto conhece o número dele só para nunca enviá-lo.

## Ligação

- Bluetooth de baixa energia (BLE), sem criptografia na camada de aplicação.
- Um serviço com identificador curto `56FF`, com duas características: `33F3`, onde se escreve, e `33F4`, por onde o
  anel responde com notificações.
- Nos dados de anúncio, o identificador de fabricante corresponde às letras "JY".
- O macOS também enxerga o anel como dispositivo de entrada (HID), porque ele serve de controle por gestos. Enquanto
  o sistema mantém essa ligação, o anel para de se anunciar; por isso o projeto primeiro tenta reaproveitar a ligação
  que o sistema já tem e só depois procura por anúncio. O serviço também abre o dispositivo de entrada em modo
  exclusivo, para o toque no anel não mexer o cursor.
- O anel atende um aparelho por vez: com o aplicativo oficial conectado no celular, o Mac não consegue.

## Formato das mensagens

Toda mensagem, nos dois sentidos, tem um cabeçalho de dez bytes seguido do conteúdo. Os campos têm dois bytes cada,
com o byte menos significativo primeiro:

| Campo | Significado |
| --- | --- |
| marca | sempre o mesmo valor (`0xFCFE`) |
| comando | o número do comando |
| total | quantos pacotes a resposta inteira tem |
| atual | o número deste pacote |
| tamanho | bytes de conteúdo que vêm a seguir |

Respostas grandes (histórico de saúde, gravações) vêm em vários pacotes numerados. Ao conectar, o anel manda uma
notificação fora desse formato, que o projeto ignora.

Datas e horas ocupam seis bytes, em segundos. O relógio do anel guarda a hora local como se fosse UTC.

## Comandos usados

Os números estão em `major/protocol.py`. Em resumo:

- **Relógio**: acertar a hora. A hora atual do anel vem junto com a contagem de passos.
- **Bateria**: porcentagem.
- **Botão**: o anel avisa quando o botão é pressionado e quando é solto. É isso que o serviço usa para gravar pelo
  microfone do Mac.
- **Batimentos e oxigenação ao vivo**: ligar e desligar cada sensor; as leituras chegam em mensagens próprias. Pedidos
  juntos, só a oxigenação responde; por isso o projeto mede um de cada vez e sempre desliga o sensor no fim.
- **Medição automática**: hora de início, hora de fim, ligada ou não, e intervalo em minutos. Não há como ler esse
  ajuste de volta, então o serviço o reaplica a cada conexão e anota o que mandou.
- **Passos**: contadores do dia (passos, calorias, distância).
- **Histórico de saúde**: por dia (zero é hoje). Registros de catorze bytes: hora, batimentos, oxigenação,
  temperatura em décimos de grau e quatro bytes que ainda não entendemos. Registros preenchidos com `FF` são vazios.
- **Sono**: por dia. Registros de oito bytes: hora, fase (acordado, REM, leve, profundo) e movimento.
- **Estado de áudio**: bits que dizem se o anel está gravando na memória e se há gravações pendentes.
- **Gravações guardadas**: contar, pedir a mais antiga, confirmar o recebimento (o anel então a apaga) e parar a
  transferência.

## Áudio

- O áudio vem em Opus: quadros de quarenta bytes, vinte milissegundos cada, dezesseis quilohertz, mono. Cada pacote
  de áudio começa com seis bytes de hora e segue com os quadros. O projeto decodifica com a biblioteca libopus.
- Neste modelo, segurar o botão não faz o anel gravar nem transmitir o microfone ao vivo: ele só avisa o aperto.
- Quatro toques seguidos começam uma gravação na memória do anel, e mais quatro terminam. O gesto é do firmware e não
  dá para trocar. Não achamos comando para começar uma gravação à distância, só para parar e para baixar.
- Enquanto grava, o anel não responde a comandos e a ligação costuma cair; a gravação não se perde.
- O anel entrega primeiro a gravação mais antiga, sempre do começo, e só a apaga depois de transmiti-la inteira e de
  receber a confirmação. A transferência rende por volta de treze pacotes por segundo, cada um com cerca de um quarto
  de segundo de áudio: uma gravação de trinta minutos leva uns dez minutos para chegar, e nesse tempo o anel não
  atende mais nada.
- Existe um comando de "sinal de vida" periódico. Nos nossos testes o anel não respondeu a ele, a ligação caiu do mesmo
  jeito e a transferência ficou bem mais lenta; o projeto não o usa.

## Comportamentos observados

- A ligação cai com frequência com o anel no dedo, mesmo a poucos metros do Mac. Com o anel parado ao lado do
  computador ela dura bem mais. O serviço foi escrito contando com as quedas: reconecta, retoma a coleta e aproveita
  o que já tinha sido gravado.
- Logo depois de reconectar, o macOS às vezes ainda não listou os serviços do anel; o projeto tenta de novo.
- Escritas simultâneas na mesma ligação travam; o projeto envia uma de cada vez.
- Se o relógio do anel estiver errado, os registros guardados ficam com a hora errada. O serviço recolhe o histórico
  antes de acertar o relógio e corrige as horas impossíveis.
