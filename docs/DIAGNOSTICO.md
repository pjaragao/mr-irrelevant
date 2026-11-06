# Mr. Irrelevant — Diagnóstico

> Análise feita em **04/10/2026** (domingo, Semana 4) sobre o código, a configuração, os logs, o `state.db`
> e consultas reais ao Sleeper/ESPN. Cada item traz a evidência para dar para conferir.
> Como o sistema funciona: [ARQUITETURA.md](ARQUITETURA.md).

## Resumo

A espinha dorsal está certa e funcionando: Hermes isolado em container, plugin próprio do Evolution com
gating por grupo/menção/admin, uma ferramenta só (`liga`) com 31 consultas, várias fontes além do Sleeper
(ESPN, RSS, nflverse, Jev) e calendário editorial rodando via cron do Hermes.

O que não está de acordo com a proposta:

- **A base "que atualiza sozinha" está congelada justamente no dado mais sensível:** nome, time e **status de lesão**
  dos jogadores vêm de arquivos locais de 14 e 16/09 que nada atualiza. O boletim médico que saiu hoje às 10h
  deu como OUT titulares que estão saudáveis e deixou de fora titulares que estão OUT.
- **Duas mensagens recorrentes nunca saíram:** o alerta de inativos de domingo e o Raio-X do Data Lab.
- **O modelo principal (Gemini) está sem crédito desde pelo menos 01/10**; toda resposta passa por uma
  tentativa que falha antes do fallback.
- **A sessão do grupo nunca reseta** (a opção usada no config não existe mais nesta versão do Hermes).
- **Os templates têm fatos fixos no código que já estão errados** (quem é o Brunão, quem ganhou a final de 2024,
  placares de H2H) e repetem as mesmas piadas toda semana.

Severidade: 🔴 crítico (informação errada publicada ou função quebrada) · 🟠 alto · 🟡 médio · ⚪ baixo.

## Status das correções (04/10/2026)

| Item | Status | O que foi feito |
|---|---|---|
| C1 | ✅ | `players_db.py` atualiza `/players/nfl` (6 h; 1 h em dia de jogo) e o master ao vivo sempre vence; frase "Sleeper atrasa" removida |
| C2 | ✅ | despacho por tabela com janela de 60 min por slot; inativos saem 90 min antes do kickoff principal |
| C3 | ⚠️ crédito | Gemini 3.5 Flash-Lite de volta como principal (aguarda recarga); reserva gratuita Nemotron 3.5 Lightning; `provider-watch` avisa no DM quando falha/volta. Antes: | **os dois provedores estão sem saldo**: Gemini (402 prepay) e OpenRouter (saldo não cobre a reserva de 65.536 tokens que o Hermes pede). Nada no código resolve: é preciso recarregar um deles. As chamadas diretas do motor ao OpenRouter agora pedem `max_tokens` baixo |
| C4 | ✅ | cron `session-reset` (05h) encerra a sessão do grupo; o Hermes abre outra na próxima mensagem |
| A1 | ✅ | Raio-X confere a rodada que acabou (não a do Sleeper) |
| A2 | ✅ | editorial, Data Lab e trades só marcam como feito com `key.id` do Evolution; alerta único ao admin; trades gravadas só depois do anúncio |
| A3 | ✅ | trash talk e recap só com fatos calculados (H2H, finais, campanha, recordes); frases rotativas; dossiê de manager com títulos/temporadas do `playoffs_history`; slot de sexta "Brunão = Ari" removido |
| A4 | ✅ | `league_time.py` usa a ESPN (jogo internacional, horário de verão dos EUA) com fallback por data |
| A5 | ✅ | FAAB lido de `settings.waiver_budget` em todo lugar |
| A6 | ✅ | waivers e plantões leem leg atual + anterior (últimas 30 h nos plantões) |
| A7 | ✅ | `people.py` (resolvedor único, palavra inteira, apelidos) + adapter injeta `[Nome \| Time]` |
| A8 | ✅ | `normalize_args` trata `0`/`""` como ausente |
| M1–M5, M7–M12 | ✅ | ver itens; M7 (regex) e M11 (senha do dashboard) estão no `data/.env` |
| M6 | 🟡 parcial | temporada do draft, dossiês, classificação e builders agora vêm do Sleeper; sobraram fallbacks `"2026"` inofensivos |
| B1 | ✅ | uma cópia só de cada arquivo, em `knowledge_base/` (etapa 2) |
| B2–B7 | ✅ | estado fora do git, docs atualizadas, código morto removido, `tests/test_engine.py`, `league-cron.log`, limite por pessoa |

---

## 🔴 Críticos

### C1. Status de lesão e dados de jogador vêm de um snapshot congelado

- **Onde:** `players_db` em [news_service.py:69](../league/news_service.py) e `get_players_db` em
  [sleeper_client.py:173](../league/sleeper_client.py) juntam `players_registry.json` → `master_nfl_players.json`
  (16/09, sem status de lesão) → `league_players.json` (14/09, 416 jogadores). O último arquivo **sobrescreve** os anteriores.
  Nenhum código atualiza esses dois arquivos.
- **Evidência (comparado com `GET /players/nfl` agora):** 90 divergências de `injury_status` entre os 434 jogadores
  com dono; 29 jogadores com dono nem existem em `league_players.json`; 8 com time NFL errado (ex.: J.J. McCarthy
  aparece no MIN, hoje está no NYG).
- **Impacto real — boletim de hoje (`injuries_registry.json`, 04/10 10:00):**
  - deu como **OUT** (todos titulares e todos saudáveis no Sleeper): Alvin Kamara, Sam Darnold (Ari), Zay Flowers (Bayer), Kyler Murray (Jonathan);
  - **omitiu** Keenan Allen (titular, OUT) e Mike Evans (titular, Questionable);
  - Brock Bowers: "Out → Sleeper desatualizado, vai pro jogo" — o Sleeper está certo; o desatualizado é o bot.
- **Efeito colateral no prompt:** a crença "o Sleeper só atualiza na terça/quarta" está escrita no
  [SOUL.md:12](../data/SOUL.md), em [round_injuries.py:310](../league/round_injuries.py) e em
  [sleeper_cli.py:133](../league/sleeper_cli.py). Ela é sintoma deste bug, e não um comportamento do Sleeper.
- **Correção:** atualizar diariamente (e no domingo de manhã) a base de jogadores a partir de `/players/nfl`
  — o [data_lab.py:104](../league/data_lab.py) já baixa esse endpoint — guardando `team`, `injury_status`,
  `injury_body_part`, `status`; tirar `league_players.json` do merge; remover a frase "Sleeper atrasa".

### C2. O alerta de inativos de domingo nunca dispara

- **Onde:** [scheduler_service.py:1356-1363](../league/scheduler_service.py). `trashtalk_hour` e `inactives_hour`
  são ambos 12. O `elif hour == trashtalk_hour and minute >= 0` é verdadeiro a hora toda, então o
  `elif` dos inativos (12h30) nunca é avaliado — nem depois de o trash talk já ter sido enviado.
- **Evidência:** `dispatched_slots.json` não tem `sun_11h30_inactives` em nenhuma semana (W2, W3, W4), mas o
  trash talk das 12h promete "daqui a exatamente 30 minutos soltaremos o ALERTA VERMELHO".
- **Correção:** avaliar os slots com `if` independentes (ou os inativos antes) e, junto com C1, usar status ao vivo.

### C3. Modelo principal sem crédito

- **Evidência:** `errors.log` — `HTTP 402 ... prepayment credits are depleted` do Gemini em 01/10; as duas
  respostas daquele dia levaram 8,6 s e 44 s, e todas caíram no fallback `typesafe/jev-router`. Não há sinal de recarga depois.
- **Impacto:** cada pergunta paga uma chamada que falha antes do fallback. O comentário no
  [config.yaml](../data/config.yaml) diz que o Gemini foi escolhido justamente por lidar melhor com semanas — hoje ele não está respondendo.
- **Correção:** recarregar o Gemini ou trocar a ordem (primário = o que funciona). Vale ter um alerta de 402/401 para o DM do admin.

### C4. A sessão do grupo nunca reseta

- **Onde:** `session_reset` no [config.yaml:32](../data/config.yaml). No Hermes 0.21.3 essa estrutura é
  "inert legacy" (`gateway/config.py`) e o roteamento diz explicitamente:
  *"Only explicit suspension replaces a routed conversation; time never does"* (`gateway/session_lifecycle.py`).
- **Evidência:** a sessão do grupo principal `20260928_210704_577da002` está aberta desde 28/09, com 250 mensagens
  (29/09 → 01/10 ficou 44 h parada e continuou a mesma).
- **Impacto:** respostas antigas continuam no contexto (ex.: "a Semana 4 nem começou" reaparece como histórico em 01/10),
  o que alimenta confusão de rodada; o custo por mensagem cresce até a compressão agir.
- **Correção:** um cron diário (05h) que suspende/reseta a sessão do grupo (o `/new` já funciona — usado em `tests/bench.sh`),
  ou o mecanismo equivalente que a versão atual do Hermes oferecer.

---

## 🟠 Altos

### A1. Raio-X do Data Lab nunca foi publicado

- **Onde:** [data_lab_slot.py:32-33](../data/scripts/data_lab_slot.py) usa a semana atual do Sleeper para checar se o
  nflverse já tem a rodada completa. Na terça o Sleeper já virou para a semana seguinte (os slots de 29/09 são
  `2026_W4_*`), então ele checa uma semana que ainda não foi jogada → sempre falso.
- **Evidência:** 22 execuções em 29/09, todas "silent"; `data_lab_dispatched.json` não tem nenhum `raio_x`.
- **Correção:** checar `semana_atual - 1` (ou `last_complete_week`).

### A2. Falha de envio é registrada como "enviado"

- **Onde:** `try_dispatch` em [scheduler_service.py:1252-1257](../league/scheduler_service.py) chama
  `save_dispatched_slot` sem olhar a resposta; [evolution_client.py](../league/evolution_client.py) engole erros
  HTTP e devolve `{"error": ...}`. Desde o commit `420e5b4` os templates saem direto pelo Evolution, sem o
  rastreio de entrega do Hermes (o log do cron mostra "silent run" mesmo quando enviou).
- **Mesmo padrão em trades:** a trade é marcada como sincronizada antes de o LLM escrever o veredito; se o LLM falhar, o anúncio se perde.
- **Correção:** só marcar como enviado com HTTP 2xx e `key.id`; logar o resultado em stderr; avisar o admin em caso de falha.

### A3. Fatos fixos no código, já errados

[scheduler_service.py](../league/scheduler_service.py):

| Linha | Texto | Realidade (base da liga) |
|---|---|---|
| 341 | "Comissário Brunão (@aribruno / Ari Bruno)" | Brunão = Rodrigo Bayer (Ayahuasquers); Ari Bruno é outro manager |
| 607 | "Final de 2024! Yan levou o troféu em cima do Riedel" | Yan **é** o Riedel; a final de 2024 foi Bayer campeão sobre Riedelots |
| 627, 671, 839 | "Yan (Bayer Kaxinawás)", "Yan (Bayer)" | a confusão que o próprio SOUL.md proíbe |
| 617-621 | "tabu 7 x 1 Marinho × Paulo", "8x1" | H2H atual: 7 x 2 |
| 635 | "Riedel lidera por 5 a 3" contra TITOs | 6 x 3 |
| 657 | "Léo 6 x 3 Jonathan" | 7 x 3 |
| 627 | "Chevarria 1 x 7 Bayer" | 1 x 8 |
| 642, 650 | "acabou de comprar Chris Godwin", "Kamara Out" | eventos pontuais congelados no texto |

Além disso, as "Leis do Mr. Irrelevant" de quinta e o trash talk de domingo são **o mesmo texto toda semana**.
O dossiê de manager também tem títulos/vices e temporadas 2022–2025 fixos ([knowledge_service.py:158-175](../league/knowledge_service.py)).

- **Correção:** os placares de H2H já existem em `kb.get_h2h_segregated_summary` — usar sempre. Para o tom, aplicar o mesmo
  padrão do `trades-watch`: o script monta os fatos e o LLM escreve 1–2 linhas de veneno.

### A4. Horários de domingo fixos no código

- **Onde:** `get_sunday_schedule_times` em [scheduler_service.py:92](../league/scheduler_service.py) (e cópia em
  [news_service.py](../league/news_service.py)): kickoff 14h até a semana 8, 15h a partir da 9.
- **Evidência (ESPN):** semanas 4–7 têm jogo internacional às **10h30** BRT (hoje inclusive); na semana 8 (01/11)
  o horário de verão dos EUA já acabou e o primeiro jogo é **15h**, não 14h.
- **Impacto:** o card das 9h diz "bola voa às 14:00" com jogo começando às 10h30; o boletim e os inativos
  chegam depois do primeiro kickoff; na semana 8 o "faltam 2 horas" erra por uma hora.
- **Correção:** derivar os horários do placar da ESPN da semana (primeiro kickoff real do domingo).

### A5. FAAB calculado com orçamento de $100 (a liga usa $200)

- **Onde:** [scheduler_service.py:275](../league/scheduler_service.py) `100 - waiver_budget_used`; o
  [build_teams_rosters_analysis.py:93](../league/build_teams_rosters_analysis.py) usa 200 → o radar de quarta e a consulta `time` se contradizem.
- **Evidência:** `settings.waiver_budget = 200`. O radar mostra os "mais ricos" com $100 (são $200) e o Floripa com $45 (são $145).
- **Correção:** ler `league.settings.waiver_budget`.

### A6. Waivers do "leg" anterior não são registrados nem anunciados

- **Onde:** [transaction_monitor.py:558](../league/transaction_monitor.py) e o Plantão de quinta
  ([scheduler_service.py:329](../league/scheduler_service.py)) leem só as transações da semana atual. No Sleeper,
  o claim processado na madrugada de quinta fica no leg da semana anterior.
- **Evidência:** 8 claims concluídos nunca ingeridos (W1: 1, W2: 5 — os do waiver atrasado de sexta 25/09 —, W3: 2).
- **Correção:** ler a semana atual **e** a anterior (o dedupe por `transaction_id` já existe).

### A7. Resolução de nomes inconsistente, com falso positivo

- **Onde:** `analyze_matchup` faz substring sobre nomes do Sleeper ([sleeper_client.py:852](../league/sleeper_client.py));
  `KnowledgeBaseService.resolve_roster` tem outra lista de apelidos.
- **Evidência:**
  - `analise_confronto alvo="Ari"` → devolve o confronto do **MOC** ("ari" está dentro de "m**ari**nh012");
  - "Paulo José Aragão Júnior", "Yan Riedel" e "Léo" → "nenhum confronto encontrado";
  - "Brunão", "Lula", "Mário", "Chico", "Jony" → não resolvem em nenhum dos dois resolvedores.
- **Impacto:** o SOUL manda usar "o time de quem pergunta", mas o modelo só recebe `[Nome do WhatsApp]` e nenhum mapa nome→time.
- **Correção:** um cadastro único de pessoas (telefone/LID, nome no WhatsApp, apelidos → roster_id) usado por todas
  as consultas, e o adapter injetando "quem pergunta: Floripa Astros (roster 4)" na mensagem.

### A8. Parâmetros zerados quebram consultas

- **Evidência:** nos logs o modelo preenche tudo (`"semana":0, "limite":0, "alvo":""`). Testado:
  `confrontos semana=0` → "SEMANA 0 — nenhum confronto encontrado"; `trocas limite=0` → despeja as 141 trocas (cortado em 6000 caracteres).
- **Onde:** [liga_core.py:60](../league/liga_core.py) (`_argv`) e o guard em [liga_core.py:107](../league/liga_core.py) tratam 0 como valor.
- **Correção:** normalizar `0`/`""` como ausente antes de montar o argv.

---

## 🟡 Médios

| # | Problema | Onde / evidência | Correção |
|---|---|---|---|
| M1 | Sem `alvo`, a CLI escolhe um alvo padrão em silêncio (Tomorrowland, eduohira, FloripaAstros; `draft_picks`→2024, `notas_draft`→2023) | [sleeper_cli.py:89-261](../league/sleeper_cli.py) | devolver "alvo obrigatório" |
| M2 | `transacoes` mostra IDs em vez de nomes e inclui claims que falharam (o mesmo jogador "adicionado" por 3 times) | [sleeper_cli.py:209](../league/sleeper_cli.py); testado | resolver nomes, filtrar `status == complete` |
| M3 | `trocas blockbusters` não ordena por impacto (lista cronológica) | testado | critério de impacto (picks de 1ª, jogadores titulares) |
| M4 | Falhas viram dado velho sem aviso: timeout de 4 s no Sleeper devolve o último cache em disco; `get_nfl_state` cai para `week: 2`; se a ESPN falhar, todo titular conta como "já jogou" → todos os confrontos "ENCERRADO" | [sleeper_client.py:58-85, 490](../league/sleeper_client.py) | cache com TTL + marcação "dado de X min atrás"; em falha da ESPN, considerar "pendente" |
| M5 | A consolidação semanal pode gravar placar parcial: roda a cada 30 min na terça, só exige soma > 100 pts, não confere se o MNF acabou e nunca revisita (correções de stats) | [transaction_monitor.py:665-690](../league/transaction_monitor.py) | exigir todos os jogos da semana `post` na ESPN; reconciliar na quarta |
| M6 | ~120 referências fixas a 2026 (e "2027" no draft); a virada de temporada vai quebrar dossiês, draft e defaults | `grep 2026 league/*.py` | temporada sempre do `state/nfl` |
| M7 | A regex de chamada pega "brock", "purdy" e "irrelevant" soltos — Brock Bowers (titular do Paulo) e Brock Purdy são jogadores reais | `EVOLUTION_MENTION_REGEX` | exigir @menção/reply ou "mr irrelevant"/"purdy," no início |
| M8 | Classificação de lesão por palavras-chave genéricas ("starts", "return", "perna", "mri") + sobrenome no título → falsos "liberado/vai pro jogo" | [news_service.py:368](../league/news_service.py) | priorizar status oficial ao vivo; notícia só como complemento |
| M9 | Consultas de leitura gravam arquivos e são lentas: `noticias_nfl`/`boletim_pre_jogo` refazem a varredura de lesões (2× dentro de `get_league_injury_bulletin`); `confrontos` levou 8,3 s; lock global serializa tudo | [news_service.py:818](../league/news_service.py), [liga_core.py](../league/liga_core.py) | separar "sync" (cron) de "consulta" (só lê) |
| M10 | Persona contradiz o adapter: o SOUL diz que o bot nunca anota nada, mas `anota aí: …` do admin grava nota — só com pontuação; "anota aí que…" cai no LLM, que recusa | [SOUL.md:25](../data/SOUL.md), [admin_notes.py](../league/admin_notes.py) | alinhar o texto do SOUL e aceitar "anota aí que" |
| M11 | Exposição: na working tree o compose tem a senha do dashboard em texto puro e publica `0.0.0.0:9121`; o API server já está em `0.0.0.0:8645` com terminal backend `local` (o próprio Hermes avisa no log). O README diz "no host ports" | [docker-compose.yml:20-30](../docker-compose.yml) | `127.0.0.1:` nas portas, senha no `.env` |
| M12 | Imagem do Hermes sem versão fixa; o plugin importa internos (`gateway.platforms.*`) — um `pull` pode quebrar tudo | [docker-compose.yml](../docker-compose.yml) | fixar tag/digest |

## ⚪ Baixos

- **B1. Dados duplicados:** quase todo arquivo existe em `league/` e `league/knowledge_base/`; `playoffs_history.json`
  já diverge em esquema. ~10 arquivos da base (incluindo o histórico do WhatsApp de 14 MB) não são lidos por nenhum código.
- **B2. Estado de runtime versionado:** `dispatched_slots.json`, `synced_transactions.json`, registries e caches estão no git →
  `git status` sempre com ~20 arquivos modificados, o que mascara mudanças reais de código.
- **B3. Documentação desatualizada:** o README fala em "collect mode", "no host ports", 29 consultas e 3 crons (são 31 e 6);
  a docstring do scheduler lista 13 slots que não batem com o despacho; o `liga_core` cita um "Jev fast path" que não existe mais.
- **B4. Código morto:** `generate_opening_day_injuries/trashtalk/matchups`, `dispatch_all_matchups`, `run_scheduler_loop`,
  o caminho `docker exec hermes-agent` do veredito de trade, `_sync_to_hermes` (`copy_to_hermes.py` não existe),
  `_calculate_team_projection` (prioriza half-PPR numa liga PPR).
- **B5. Sem testes automatizados.** Os defaults de `WeeklyScheduler`/`TransactionMonitor` apontam para o **grupo principal** —
  rodar `scheduler_service.py check` fora do container publica no grupo real. `tests/fire.py` tem telefone/LID fixos.
- **B6. Diagnóstico difícil:** o stderr dos scripts de cron não aparece em `data/cron/output/` (só "silent run").
- **B7.** Sem limite de chamadas por pessoa (qualquer membro pode gastar tokens à vontade).

---

## Conformidade com a proposta

| Proposta | Situação |
|---|---|
| Hermes ligado ao Evolution API | ✅ plugin próprio, gating correto, entrega e divisão de mensagens funcionando |
| Base de conhecimento que se atualiza sozinha | 🟡 H2H, recordes, trades, waivers e ordem do draft sim; jogadores/lesões congelados (C1), waivers com lacuna (A6), dossiês de manager estáticos (A3) |
| Várias fontes além do Sleeper | ✅ ESPN, 4 RSS, nflverse/NGS, DynastyProcess, Jev — mas o status oficial ao vivo do Sleeper, que é a fonte mais confiável para lesão, não é usado |
| Mensagens padronizadas recorrentes | 🟡 terça/quarta/quinta/domingo/segunda saem; inativos (C2) e Raio-X (A1) nunca; falhas somem (A2); conteúdo fixo e repetitivo (A3); horários de domingo errados (A4) |
| Respostas factuais via ferramenta | 🟡 a arquitetura induz bem ("fatos só da `liga`"), mas a ferramenta devolve dado velho (C1), aceita parâmetros zerados (A8) e resolve nomes mal (A7); modelo principal fora do ar (C3) e sessão eterna (C4) |

## Melhorias sugeridas (em ordem)

1. **Correções rápidas, de baixo risco:** C2, A1, A5, A8, M1, M2, A2; tirar a frase "Sleeper atrasa"; resolver C3 (crédito/ordem dos modelos);
   cron de reset diário da sessão (C4).
2. **Dados vivos:** refresh diário de `/players/nfl` (C1); cadastro único de pessoas + "quem pergunta" injetado pelo adapter (A7);
   waivers lendo dois legs (A6); horários de domingo pela ESPN (A4); temporada sempre do Sleeper (M6).
3. **Conteúdo editorial:** trocar as piadas fixas por fatos calculados (H2H, sequência, recordes da semana) + 1–2 linhas do LLM
   no padrão do `trades-watch`; variar o texto semana a semana.
4. **Operação:** marcar envio só com sucesso e avisar o admin em falha; capturar stderr dos crons; fixar a imagem; portas em `127.0.0.1`;
   testes para a lógica de horários do scheduler e para a normalização de parâmetros do `liga_core`.
5. **Estrutura:** uma cópia de cada dado; estado de runtime fora do git; `liga_core` chamando funções diretamente em vez de
   capturar o `stdout` do `sleeper_cli`; separar "sincronizar" (cron) de "consultar" (ferramenta).

Ideias de funcionalidade, para depois do básico: "meu confronto" sem precisar dizer o time; alerta ao vivo quando um titular da liga sai
machucado no meio do jogo (o `round_injuries` já tem metade disso); power ranking semanal com all-play (alguém já pediu no grupo em 29/09).

## Etapa 2 (implementada em 04/10/2026)

| Pedido | Implementação |
|---|---|
| Listar todos os movimentos de waiver | `waiver_report.py`: todas as transações, gasto por time, disputas, lances perdidos; quinta 08h, domingo 08h e consulta `waivers_semana` |
| Domingo com jogo de manhã | sábado 20h: jogos cedo + titulares envolvidos; domingo: alerta de inativos 90 min antes de cada janela cedo; cronograma e boletim com horários reais |
| Layout das mensagens | `msg_format.Msg` em todas as mensagens agendadas, quebra automática em partes, retomada sem duplicar |
| Alerta de crédito | cron `provider-watch` (DM ao admin) |
| Checagem de escalação | sábado 20h no grupo + DM opcional (`LINEUP_DM`) |
| Alerta ao vivo | cron `live-watch` (lesão de titular em jogo, viradas; limite diário) |
| Terça mais rica | power ranking, all-play, sorte, pontos no banco |
| Chance de playoff | simulação Monte Carlo a partir da semana 8 + consulta `chances_playoff` |
| Tom dos textos | `flavor.py`: 1 frase do LLM por mensagem sobre fatos calculados, com validação e texto de reserva |
| Avaliar troca | consulta `avaliar_troca` (FantasyCalc) |
| Enquetes | implementadas e desligadas (`POLLS_ENABLED=0`); placar de acertos não implementado |
| Arrumação interna | base unificada em `knowledge_base/`; `liga_core` chama `sleeper_cli.run()` sem mexer em `sys.argv` global |
