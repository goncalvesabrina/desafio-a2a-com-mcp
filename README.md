# A Ponte: um agente A2A com MCP por dentro

Dois processos separados que falam HTTP entre si:

- **`servidor-mcp/`**: servidor MCP (Streamable HTTP, revisão `2026-07-28`) na porta `7301`, em `/mcp`. Tem três tools (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`), o resource `politica://uso` e o ciclo completo de MRTR na reserva.
- **`agente/`**: agente "Central de Salas" na porta `7300`. Por dentro é host MCP do servidor acima. Por fora é servidor A2A v1.0 (binding JSON-RPC em `/a2a`, Agent Card em `/.well-known/agent-card.json`).

Stack: Python 3.10+, SDK oficial `mcp==2.2.0` (v2) no servidor e no cliente, e Starlette/uvicorn no lado A2A. As versões estão travadas em [pyproject.toml](pyproject.toml). Não há LLM em nenhum ponto: o agente interpreta um pedido em formato fixo, e toda decisão de sala é do servidor MCP.

## Como rodar

A partir de um clone limpo, com Python 3.10 ou superior:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

O servidor MCP exige a variável `REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Ela é a chave que protege o `requestState`. Gere a sua e exporte no terminal do servidor (nunca coloque o valor no repositório):

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Terminal 1, servidor MCP (o stderr mostra cada request com método, id e traceparent):

```bash
. .venv/bin/activate
export REQUEST_STATE_SECRET=...   # o valor gerado acima
python servidor-mcp/servidor.py
```

Terminal 2, agente:

```bash
. .venv/bin/activate
python agente/agente.py
```

Terminal 3, validador (sem dependências, roda com o `python3` do sistema):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode o validador sempre com os dois processos recém-iniciados: as reservas ficam em memória, e uma execução muda o resultado da seguinte.

Para testar o `requestState` depois de um restart, reinicie o servidor MCP **com o mesmo `REQUEST_STATE_SECRET`**. Um estado emitido antes do restart continua valendo, porque tudo o que o servidor precisa viaja dentro dele.

Variáveis opcionais, com os padrões entre parênteses: `MCP_HOST` (`127.0.0.1`), `MCP_PORT` (`7301`), `MCP_PATH` (`/mcp`), `AGENTE_HOST` (`127.0.0.1`), `AGENTE_PORT` (`7300`), `AGENTE_URL` (URL pública usada no card, `http://127.0.0.1:7300`) e `MCP_URL` (onde o agente encontra o servidor MCP, `http://127.0.0.1:7301/mcp`).

## Onde a ponte acontece

A ponte está em [agente/ponte.py](agente/ponte.py). Quando o `tools/call` de `reservar_sala` volta como `InputRequiredResult`, [`_aplicar`](agente/ponte.py) chama [`_pausar`](agente/ponte.py). Essa função lê a única entrada de `inputRequests`, extrai o `enum` (ou o `const`) da propriedade `sala` do `requestedSchema` e guarda na Task uma `Pendencia` com a chave, as opções, os argumentos originais e o `requestState` opaco ([agente/tarefas.py](agente/tarefas.py)). Depois ela transita a Task para `TASK_STATE_INPUT_REQUIRED` com a mensagem exata `alternativas: a, b`. O caminho de volta está em [`continuar`](agente/ponte.py): `escolha=<id>` vira `{"action": "accept", "content": {"sala": id}}`, `escolha=recusar` vira `{"action": "decline"}`, e o agente repete o `tools/call` com os mesmos argumentos, `inputResponses` sob a mesma chave e o `requestState` ecoado sem modificação. Isso é feito por [`HostMCP.chamar`](agente/cliente_mcp.py), que usa `ClientSession.call_tool(..., allow_input_required=True)` e por isso sai com um id JSON-RPC novo. Uma escolha fora do `enum` não chega ao servidor: a Task continua pausada e a mesma linha de alternativas é repetida.

Do lado do servidor, o MRTR usa o caminho de primeira classe do SDK. Em [servidor-mcp/servidor.py](servidor-mcp/servidor.py), o parâmetro `escolha` de `reservar_sala` é anotado com `Resolve(escolha_de_sala)`. Quando o intervalo está ocupado e existem alternativas, o resolver devolve `Elicit(...)` com um schema cujo `enum` são as alternativas, e o SDK encerra a resposta com `resultType: input_required`. O servidor nunca inicia um request para o cliente. No retry, o SDK verifica o estado, reexecuta o resolver e injeta a resposta.

## Decisões técnicas

**Proteção do `requestState`.** Uso o `RequestStateBoundary` do próprio SDK, configurado com `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)`. O token é cifrado e autenticado com AES-256-GCM, com a chave derivada do segredo por HKDF-SHA256. Portanto é mais do que assinado: é ilegível e inadulterável. O envelope selado carrega `iat`/`exp`, o método, a tool e um digest dos argumentos originais, além da audiência (o nome do servidor). Um token com um caractere trocado, expirado ou apresentado com argumentos diferentes dos selados é rejeitado com `-32602` (`Invalid or expired requestState`), e o motivo real vai só para o log do servidor. Por isso, no retry com argumentos adulterados, a divergência não toma efeito: o servidor rejeita o estado. O servidor não sobe sem `REQUEST_STATE_SECRET` com pelo menos 32 bytes, e não há segredo no código.

**Validade.** O `requestState` vale 10 minutos, dentro da faixa exigida de 5 a 30.

**Nada guardado no servidor entre as rodadas.** O `requestState` carrega a resposta já dada e o digest da pergunta feita. Os argumentos são amarrados pelo digest e reenviados pelo cliente. No retry, o resolver recalcula as alternativas a partir das reservas atuais, e o SDK só aceita a resposta se a pergunta renderizada for idêntica à que foi feita. Se uma alternativa foi tomada no meio-tempo, a pergunta é refeita em vez de reservar uma sala ocupada. Como a chave vem do ambiente, um retry depois de reiniciar o processo funciona, como descrito em *Como rodar*.

**Capability de elicitation.** O `-32021` com `data.requiredCapabilities` é produzido pelo SDK quando o resolver precisa perguntar e o `_meta` não declara elicitation em form mode. Um conflito sem nenhuma alternativa não chega a perguntar e devolve `isError` com `Sem alternativas disponiveis no intervalo`. A validação do `_meta` obrigatório (`-32602`, HTTP 400) e dos headers espelhados (`-32020`) também é do transporte do SDK, em modo stateless com resposta JSON.

**O agente como host MCP.** Um `mcp.Client` vive durante todo o processo do agente, fixado na versão `2026-07-28`, sem handshake e sem sessão. O agente registra um `elicitation_callback` que apenas lança erro. Ele existe só para o SDK declarar `{"elicitation": {"form": {}}}` nas capabilities de cada request. Todas as chamadas passam por `session.call_tool(..., allow_input_required=True)`, então o SDK nunca responde a elicitation sozinho, e o `input_required` chega cru à ponte. As tools são descobertas por `tools/list` antes do primeiro `tools/call`, e a versão da política é lida do resource `politica://uso` e entra no artifact. O `traceparent` recebido no header A2A é guardado na Task, e todo request MCP daquela Task leva o mesmo trace-id com um span-id novo, inclusive o retry.

**Estado das Tasks.** Fica em memória no processo do agente ([agente/tarefas.py](agente/tarefas.py)), num dicionário por id. Cada Task tem um `asyncio.Lock` próprio, e a `Pendencia` (com o `requestState`) é por Task, então duas Tasks pausadas ao mesmo tempo nunca trocam de estado. A `Pendencia` fica fora de `para_wire()` e por isso nunca aparece em card, artifact, mensagem ou histórico. Estados terminais (`COMPLETED`, `FAILED`, `CANCELED`) são definitivos: um `SendMessage` para uma delas recebe o erro JSON-RPC `-32004`, e um `taskId` desconhecido recebe `-32001`. Um `isError` da tool termina a Task em `TASK_STATE_FAILED`, com o texto da tool na mensagem de status e no histórico.

**Reservas.** Ficam em memória no servidor MCP ([servidor-mcp/dominio.py](servidor-mcp/dominio.py)), carregadas de `dados/` na subida. As regras são aplicadas nesta ordem: sala existe, intervalo não invertido, janela 08:00–20:00 em `-03:00`, duração de no máximo 2 horas. As alternativas são as salas livres com capacidade maior ou igual à da pedida, ordenadas por capacidade e depois por id, no máximo três.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: 37a6f2f70e8f2d41003e1d7c957cac82
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
