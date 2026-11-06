# Mr. Irrelevant — Arquitetura e funcionamento

> Retrato do sistema em **04/10/2026** (Semana 4 da NFL 2026), já com as correções do diagnóstico aplicadas.
> Hermes Agent `0.21.3` (imagem fixada por digest no compose).
> Problemas encontrados e o que foi corrigido: [DIAGNOSTICO.md](DIAGNOSTICO.md).

## 1. Visão geral

Bot de WhatsApp da liga dynasty **High Stakes Fantasy** (Sleeper, 12 times, 3 divisões, PPR + Superflex,
FAAB $200). Desde a multiliga, o mesmo container/instância atende **outras ligas** em outros grupos, cada uma um
perfil isolado do Hermes, a partir só do id da liga na Sleeper: [MULTILIGA.md](MULTILIGA.md). Duas funções:

1. **Responder perguntas no grupo** quando é chamado (menção, nome, reply) — um agente LLM do Hermes com
   persona própria e **uma única ferramenta** (`liga`) que consulta o motor da liga.
2. **Publicar mensagens recorrentes** (calendário editorial semanal, anúncio de trades, Data Lab) via cron do Hermes.

```
 Grupo WhatsApp
      │  ▲
      │  │ sendText
      ▼  │
 Evolution API  (evolution-api:8080, instância "sleeper-bot")
      │ webhook MESSAGES_UPSERT + header X-Webhook-Token
      ▼
┌──────────────────── container mr-irrelevant ─────────────────────────────────────┐
│  plugin evolution-whatsapp (:8660)  ── gating: grupo permitido + menção / DM / admin│
│        │                                                                         │
│        ▼                                                                         │
│  Hermes gateway ── agente LLM (SOUL.md + contexto da rodada + notas do admin)     │
│        │   ferramenta única: liga (31 consultas)                                 │
│        ▼                                                                         │
│  /opt/league  (motor da liga, Python stdlib)                                     │
│        └──► Sleeper API · ESPN · RSS (Rotowire/FantasyPros/Yahoo/CBS) · nflverse │
│             · DynastyProcess · OpenRouter (Jev / jev-router)                      │
│                                                                                  │
│  Hermes cron (6 jobs) ── data/scripts/*.py ── motor ── Evolution sendText         │
└──────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Estrutura do repositório

| Caminho | Montado em | O que é |
|---|---|---|
| [docker-compose.yml](../docker-compose.yml) | — | container `mr-irrelevant` |
| `data/` | `/opt/data` | HERMES_HOME (config, persona, plugins, scripts de cron, sessões, logs, `.env`). Só `config.yaml`, `SOUL.md`, `plugins/` e `scripts/` são versionados |
| `league/` | `/opt/league` | motor da liga (código + JSON/Markdown da base de conhecimento) |
| `data/league.yaml` · `data/profiles/<slug>/` | — | liga #1 (perfil default) · uma pasta por liga extra (`league.yaml`, `config.yaml`, `SOUL.md` versionados; `.env`, sessões e dados não) — [MULTILIGA.md](MULTILIGA.md) |
| `ops/` | — | `liga.py` (CLI multiliga), `agy_shim.py`, `crons.sh`, `cutover.sh` / `rollback.sh` (migração a partir do bot legado `~/sleeper-bot`) |
| `tests/` | — | `test_*.py` (unitários offline: motor, base, multiliga), `e2e/` (gateway real num container descartável), `fire.py` (simula mensagem via webhook) e `bench.sh` (benchmark) |

## 3. Container

[docker-compose.yml](../docker-compose.yml):

- Comando `gateway run`; `env_file: ./data/.env`; `TZ=America/Sao_Paulo`; `LEAGUE_DIR=/opt/league`;
  `SLEEPER_LEAGUE_ID=1312694161610113024`.
- Rede externa `services_internal` (a mesma do `evolution-api`); webhook interno em `http://mr-irrelevant:8660`.
- Limites: 1 GB RAM, 1 CPU, 256 PIDs, `no-new-privileges`. Healthcheck `GET :8660/health`.
- Portas publicadas só em `127.0.0.1`: `8645→8642` (API server do Hermes) e `9121→9119` (dashboard; usuário e
  senha em `data/.env`).

## 4. Hermes

### 4.1 Configuração ([data/config.yaml](../data/config.yaml))

| Bloco | Valor | Efeito |
|---|---|---|
| `model` | `gemini-flash-latest` via provider `agy` | modelo do chat: o Gemini Flash mais novo, pelo Antigravity CLI atrás de um adaptador OpenAI-compatível no host (§4.3) |
| `fallback_providers` | OpenRouter `nvidia/nemotron-3-ultra-550b-a55b:free` → `google/gemma-4-31b-it:free` → `nvidia/nemotron-3.5-lightning:free` | cadeia gratuita quando o `agy` falha/estoura o tempo (o Gemini pago saiu da cadeia) |
| `agent` | `max_turns: 8`, `reasoning_effort: low` | até 8 chamadas ao modelo por resposta; o adaptador `agy` corta as ferramentas na 6ª rodada de consulta e pede a resposta final (§4.3), para uma busca longa nunca terminar em "no response was generated" |
| `platform_toolsets` | só `liga` em evolution / cron / cli / api_server | o modelo não tem terminal, arquivos nem browser |
| `group_sessions_per_user` | `false` | **uma sessão por grupo**, compartilhada por todos os membros |
| (reset de sessão) | cron `session-reset` às 05h | o Hermes 0.21 não reseta sessão por tempo; o cron encerra a sessão do grupo e a próxima mensagem abre uma nova |
| `compression` | limiar 35%, poda proativa de tool outputs > 12k tokens | segura o tamanho da sessão |
| `memory` | desligada | o bot não aprende sozinho |
| `display` / `streaming` | sem streaming, sem progresso, sem "digitando…" interino | 1 mensagem final por pergunta |
| `cron.wrap_response` | `false` | saída do cron vai "limpa" para o grupo |

### 4.2 Persona ([data/SOUL.md](../data/SOUL.md))

"Mr. Irrelevant / Brock Purdy": ácido, técnico, PT-BR. Regras principais:
- Fatos **só** da ferramenta `liga`; roteamento de pergunta → consulta (ex.: "lesões da rodada" → `lesoes_rodada`).
- Regras de fantasy (só titulares pontuam; rodada vai de quinta a segunda/MNF; playoffs > temporada regular).
- Pessoas: comissário = Brunão (Rodrigo Bayer); Paulo Jr = admin/criador; `✅ADMIN` só vem do número verificado.
- O bot não altera dados; estilo WhatsApp (asterisco simples, `•`, 3–12 linhas).

### 4.3 Sessões

Cada grupo tem uma sessão Hermes única (chave `agent:main:evolution:group:<jid>`). Mensagens chegam como
`[Nome] texto`. Histórico fica em `data/state.db` (SQLite) e é comprimido automaticamente.


### 4.3 Antigravity (`agy`) como modelo do chat

- **Como funciona:** [ops/agy_shim.py](../ops/agy_shim.py) é um servidor OpenAI-compatível (`/v1/chat/completions`) que roda **no host**, em `172.26.0.1:8787` (ponte da rede `services_internal`), exige `Authorization: Bearer $AGY_SHIM_TOKEN` e faz **uma execução isolada do `agy` por chamada** (`agy -p … --model <id> --output-format json --sandbox`, pasta temporária vazia, sem `--dangerously-skip-permissions`, no máximo 2 em paralelo). As ferramentas do Hermes entram no prompt com um protocolo `TOOL_CALL {…}`; o adaptador as converte em `tool_calls` e o Hermes as executa.
- **Modelo (sempre o Gemini Flash mais novo):** o `agy` só aceita os ids de `agy models` (ex.: `gemini-3.8-flash-low`) e não tem alias de "latest"; quando um modelo sai do catálogo (foi o caso do 3.6) a chamada falha. Por isso o adaptador resolve o alias `gemini-flash-latest` sozinho: lê `agy models`, escolhe o maior `gemini-<versão>-flash-<esforço>` (só Flash puro; Flash-Lite, Pro e outros fabricantes não entram), guarda o resultado por 1 h e, se o `agy` rejeitar o id guardado, consulta o catálogo de novo e repete a chamada uma vez. Esforço padrão `low`. O nome em `config.yaml` é só informativo (quem escolhe o modelo é o adaptador). Para fixar um modelo ou mudar o esforço: `AGY_SHIM_MODEL=<id de agy models>` / `AGY_SHIM_EFFORT=low|medium|high` (e `AGY_SHIM_MODEL_TTL`, em segundos) no `data/.env`, depois `systemctl --user restart agy-shim`. O id em uso aparece no log (`model gemini-flash-latest -> …`) e em `GET /v1/models`.
- **Limite de consultas por resposta:** o Hermes dá a cada resposta um orçamento de chamadas ao modelo (`agent.max_turns`, 8) e, quando ele acaba com uma ferramenta pendente, não faz a chamada de resumo (se houve uma resposta vazia antes, o `final_response` fica `""` e o passo de resumo é pulado): o grupo recebe "⚠️ Processing completed but no response was generated" (aconteceu em 13/10 às 13:08, num pedido de "todas as trocas do Bayer com o Jonatan + histórico dos jogadores" que varreu `trocas` ano a ano). Por isso, na 6ª rodada de ferramenta do mesmo turno (`AGY_SHIM_MAX_TOOL_ROUNDS`; mantenha ≤ `max_turns` − 2) o adaptador deixa de oferecer as ferramentas e pede a resposta final com o que já veio; se o modelo insistir em `TOOL_CALL`, o grupo recebe um aviso curto em vez do protocolo. Além disso, `trocas alvo=A alvo2=B` devolve só as trocas entre os dois numa chamada.
- **Persistência:** unit systemd de usuário `agy-shim.service` ([ops/agy-shim.service](../ops/agy-shim.service)), habilitada, com `Restart=always` e linger ligado: sobe no boot e volta sozinha se cair. O container sobe com `restart: unless-stopped` e lê o token do `data/.env` (`env_file`); trocar o token exige `docker compose -p mr-irrelevant up -d` (recria o container), não basta `docker restart`.
- **Operar:** `systemctl --user status|restart agy-shim`, `ops/agy_shim.sh logs`. Login do `agy`: rodar `agy` sem argumentos e seguir o link (credencial em `~/.config` do usuário do host).
- **Se cair:** o Hermes registra `Fallback activated: … → nvidia/nemotron-3-ultra…` e responde pela cadeia; `provider-watch` avisa o admin no DM.
- **Riscos conhecidos:** latência de ~6 a 20 s por resposta (cada ferramenta é uma chamada a mais); conta Google pessoal alimentando um bot de grupo (confira os termos e a cota); sem streaming.

## 5. Plugins

### 5.1 `evolution-whatsapp` ([adapter.py](../data/plugins/evolution-whatsapp/adapter.py))

**Entrada** (`POST /webhook/evolution`):
1. Valida `X-Webhook-Token`; ignora `fromMe`, broadcast, newsletter; dedupe por `message.id` (10 min).
2. Extrai texto de `conversation`, `extendedTextMessage` ou legenda de mídia (áudio/figurinha são ignorados).
3. **Grupo**: só JIDs em `EVOLUTION_ALLOWED_GROUPS` ou no `league.yaml` de alguma liga, e só se o bot foi chamado — `mentionedJid` com o id do bot,
   reply a mensagem do bot, `@<id>` no texto ou `EVOLUTION_MENTION_REGEX`
   (`mr irrelevant` em qualquer lugar; `brock`/`purdy`/`irrelevant` só no início seguidos de `,` `:` `!` `?`; `!bot`/`/bot`).
   **DM**: só números em `EVOLUTION_DM_ALLOWED`.
4. Saudação vazia ("oi", "fala", "?") → resposta pronta, sem LLM.
5. Admin (`EVOLUTION_ADMINS`, por telefone/LID, nunca pelo nome) recebe a tag `✅ADMIN`.
   Comando determinístico `anota aí: <fato>` grava em `league/knowledge_base/ADMIN_NOTES.md` (sem LLM).
6. Quem pergunta é identificado ([people.py](../league/people.py): telefone em `LEAGUE_PEOPLE_PHONES`, senão nome do
   WhatsApp) e o nome chega ao modelo como `[Nome | Time (Manager)]`, o que faz "meu confronto" funcionar.
7. Membros comuns têm limite de `EVOLUTION_RATE_LIMIT` perguntas a cada 10 min (padrão 8); o admin não tem limite.
8. O resto vai para `handle_message` do gateway (fila se o agente estiver ocupado).

**Multiliga.** A liga da mensagem é a cujo `league.yaml` lista o grupo (`league_ctx.league_for_group`); admins, pessoas,
persona e notas são dela. O gateway atende a mensagem **pelo perfil** dessa liga (`gateway.profile_routes`); o adapter
confere isso antes (`_route_mismatch`) e **descarta** a mensagem se o gateway usaria outro perfil — nunca responde uma
liga com a persona e os dados de outra. Mensagem de grupo que nenhuma liga usa é ignorada e o JID aparece uma vez no log
(é como se descobre o `--group` de uma liga nova, junto com `ops/liga.py groups`).

**Saída**: `POST {EVOLUTION_API_URL}/message/sendText/{instância}`; converte Markdown → WhatsApp;
divide em mensagens separadas na linha `<<<MSG>>>` e em blocos de 3800 caracteres.
Também registra um `standalone_sender` para o cron entregar fora do processo do gateway.

### 5.2 `sleeper-league` ([__init__.py](../data/plugins/sleeper-league/__init__.py))

- Registra a ferramenta **`liga`** (`consulta` enum + `alvo`, `alvo2`, `semana`, `ano`, `limite`) → `liga_core.run_query`.
- Hook `pre_llm_call`: injeta a cada turno o **contexto temporal** ([round_context.py](../league/round_context.py):
  data/hora BRT, semana NFL atual via Sleeper, jogos de hoje via ESPN, "nunca consulte a semana seguinte antes do kickoff")
  e até 1500 caracteres das **notas do admin**.
- A liga do turno é a do perfil servido (`get_hermes_home()` por turno → `league_ctx.for_home`); ferramenta e hook rodam
  sob `league_ctx.use(...)`, então dados, notas e cache são os da liga certa.

## 6. Motor da liga (`league/`)

### 6.1 Módulos

| Módulo | Papel |
|---|---|
| [liga_core.py](../league/liga_core.py) | ponto de entrada da ferramenta: normaliza argumentos (`0`/`""` = ausente), exige `alvo` onde faz sentido, trava contra "semana futura", avisa quando o dado veio de cópia local, cache de 90 s, lock global, corte em 6000 caracteres |
| [players_db.py](../league/players_db.py) | base de jogadores **ao vivo** (time + status oficial de lesão) a partir de `GET /players/nfl`; todo o motor lê por aqui |
| [league_profile.py](../league/league_profile.py) | regras e estrutura de elenco **lidas da Sleeper** (slots titulares/FLEX/SUPER_FLEX/IDP, banco, K/DEF, FAAB, playoffs, dynasty/redraft): o motor pergunta ao perfil ("cabe K?", "quantos cards?", "melhor escalação") em vez de assumir a liga #1 |
| [league_ctx.py](../league/league_ctx.py) | qual liga atende a chamada (`use` / `$HERMES_HOME`), onde estão seus dados e segredos, regras da casa (`league.yaml`), pessoas, resolução por grupo/DM |
| [kb_bootstrap.py](../league/kb_bootstrap.py) | base de conhecimento de uma liga nova a partir só do id (temporadas, jogos, brackets, H2H, drafts, trocas, waivers, elencos) |
| [people.py](../league/people.py) | cadastro pessoa/apelido/telefone → roster, por liga (curado no `league.yaml`; o resto vem da Sleeper; sem falso positivo por substring) |
| [msg_format.py](../league/msg_format.py) | layout único das mensagens agendadas (título, seções, rodapé) com quebra automática em partes de até 28 linhas |
| [flavor.py](../league/flavor.py) / [llm.py](../league/llm.py) | frase de "veneno" do LLM sobre fatos já calculados (mesma cadeia de modelos do chat, lida do `config.yaml` por `llm.chain()`: `agy` → Nemotron 3 Ultra → Gemma 4 31B → Nemotron 3.5 Lightning); rejeita número inventado; sempre há texto fixo de reserva |
| [waiver_report.py](../league/waiver_report.py) | todos os movimentos de waiver/FA, lances perdidos e disputas |
| [analytics.py](../league/analytics.py) | all-play, power ranking, sorte, pontos no banco, simulação de chances de playoff |
| [trade_values.py](../league/trade_values.py) | avaliação de troca com valores dynasty do FantasyCalc (Superflex, 12 times, PPR) |
| [league_time.py](../league/league_time.py) | temporada/semana, jogos da semana pela ESPN e horários reais de domingo (jogo internacional, horário de verão dos EUA) |
| [sleeper_cli.py](../league/sleeper_cli.py) | CLI legado que formata as respostas (o `liga_core` captura o `stdout` dele) |
| [sleeper_client.py](../league/sleeper_client.py) | cliente da API Sleeper (cache em memória com TTL; em falha usa a cópia em disco e registra o aviso), tabela, confrontos, análise ao vivo, probabilidades, stats, draft, franquia |
| [knowledge_service.py](../league/knowledge_service.py) | leitura da base histórica: H2H, dossiês, recordes, trocas, waivers, playoffs, perfis psicológicos |
| [h2h_builder.py](../league/h2h_builder.py) | H2H: baixa os brackets oficiais do Sleeper (`brackets_history.json`) e reconstrói `h2h_matrix.json` a partir dos jogos brutos; recortes **reg** / **playoffs do título** (Wild Card, Semi, Final, 3º, 5º) / **Toilet Bowl**; `--verify` confere tudo contra o Sleeper; `--rivalries-md` regenera `H2H_RIVALRIES.md` |
| [news_service.py](../league/news_service.py) | notícias (ESPN + 4 RSS), triagem Jev, classificação de lesão por palavras-chave, boletins médicos |
| [round_injuries.py](../league/round_injuries.py) | "lesões da rodada": relatórios pós-jogo da ESPN × quem jogou (Sleeper) + snapshot pré-kickoff |
| [data_lab.py](../league/data_lab.py) | métricas avançadas (nflverse PBP, Next Gen Stats, snaps, PFR) + frases geradas por LLM |
| [scheduler_service.py](../league/scheduler_service.py) | calendário editorial: templates determinísticos e despacho por horário |
| [transaction_monitor.py](../league/transaction_monitor.py) | trades (anúncio), ingestão de waivers, consolidação semanal pós-MNF (H2H/histórico) |
| [draft_order_service.py](../league/draft_order_service.py) / [draft_evaluator.py](../league/draft_evaluator.py) | ordem do próximo draft (Max PF), picks trocadas, notas de draft |
| [kb_db.py](../league/kb_db.py) / [kb_queries.py](../league/kb_queries.py) / [kb_stats.py](../league/kb_stats.py) | índice SQLite `knowledge_base/league.db` (trocas, jogos, draft, waivers, jogadores, perfis), leitores em SQL e a consulta `estatisticas` (§8) |
| [charts.py](../league/charts.py) / [chart_service.py](../league/chart_service.py) | gráficos genéricos (mapa de calor, linhas, colunas agrupadas/empilhadas, barras, rosca, dispersão, destaques) a partir de estatísticas SQL ou de `dados` próprios: uma especificação validada vira HTML interativo (SVG inline, claro/escuro, tooltips, tabela) e PNG (Pillow) da mesma cena; `chart_service.py ... --send` posta no grupo; skill em `data/skills/mr-irrelevant/league-charts/` |
| [song.py](../league/song.py) | músicas: `musica` monta a ficha de pesquisa (histórico, H2H, marcas, trocas, waivers, perfil, lesões/notícias) e `cantar` transforma a letra em nota de voz (Edge TTS pt-BR Antonio + base de rap sintetizada); skill em `data/skills/mr-irrelevant/league-songs/` |
| `build_*.py` | regeneram dossiês de jogadores, elencos e waivers |
| [jev.py](../league/jev.py) | cliente do Jev (OpenRouter "decisions") para julgamentos tipados baratos |
| [evolution_client.py](../league/evolution_client.py) | envio direto ao Evolution + `sent_ok()` (confirmação de entrega) |
| [admin_notes.py](../league/admin_notes.py) | comando "anota aí" e leitura das notas |
| [nfl_game_watcher.py](../league/nfl_game_watcher.py) | placar da ESPN |

### 6.2 Ferramenta `liga` — 35 consultas

| Consulta | Implementação | Fonte de dados | Grava arquivos? |
|---|---|---|---|
| `tabela [ano]` | `format_standings_whatsapp` (2026) / `get_standings_formatted` (anos anteriores) | Sleeper / `playoffs_history.json` | não |
| `confrontos [semana]` | `format_matchups_whatsapp` | Sleeper matchups + projeções, ESPN | não |
| `analise_confronto alvo [semana]` | `format_matchup_analysis_whatsapp` | Sleeper stats/proj, ESPN | não |
| `lesoes_rodada [semana]` | `round_injuries.format_report` | ESPN pós-jogo + Sleeper stats | cache |
| `boletim_pre_jogo [semana]` | `NewsService.get_matchup_injury_report` | status oficial ao vivo + notícias recentes | não |
| `noticias_jogador alvo` | `SleeperClient.get_player_news` | status ao vivo + RSS + ESPN athlete | não |
| `stats_jogador alvo [semana] [ano]` | `get_player_stats` | Sleeper stats/proj | não |
| `jogador alvo` | `get_player_dossier` | `players_registry.json` | não |
| `h2h alvo alvo2` | `get_h2h` | `h2h_matrix.json` (+ `brackets_history.json`) | não |
| `manager alvo` | `get_manager_dossier` | `franchises_history.json` + H2H | não |
| `perfil_psicologico alvo` | `get_manager_psychological_profile` | `league.db` (tabela `profiles`, de `MANAGERS_PSYCHOLOGICAL_PROFILES.md`) | não |
| `time alvo` / `roster_dna alvo` | `get_team_roster_analysis` / `get_roster_dna_report` | `teams_rosters_analysis.json` / `players_registry.json` | não |
| `franquia alvo` | `format_franchise_history_whatsapp` | `seasons_history.json` + Sleeper | não |
| `recordes` | `kb_queries.records_report` | `league.db` (tabela `games`) | não |
| `playoffs [ano]` | `get_playoffs_dossier` | `playoffs_history.json` | não |
| `trocas [alvo] [alvo2] [ano] [semana] [limite] [detalhe]` (com `alvo2`: só as trocas entre os dois managers) / `trocas_picks alvo` | `kb_queries.trades_report` / `trade_picks_report` | `league.db` (de `trades_history.json`) | não — filtros em SQL, cabeçalho "N de TOTAL" |
| `estatisticas entidade agrupar_por metrica …` | `kb_stats.query` | `league.db` | não — contagens/rankings exatos (trocas por semana, quem mais trocou, aproveitamento, FAAB…) |
| `grafico [entidade] [agrupar_por] [serie] [metrica] [metrica2] [forma] [dados] …` | `chart_service.generate` | `league.db` (via `kb_stats.fetch`) | gera PNG + HTML em `$HERMES_HOME/image_cache/liga_graficos/` (apaga >7 dias) e devolve a linha `MEDIA:<png>`: o gateway sobe a imagem ao chat (`send_image_file` do adaptador → Evolution `sendMedia`) |
| `ajuda [alvo]` | `liga_core.HELP` | — | uso detalhado de cada consulta (mantém o schema da ferramenta curto) |
| `draft_historico alvo [ano]` / `draft_picks [ano]` / `picks_manager alvo` / `notas_draft [ano]` | vários | `league.db` (tabela `draft_picks`, de `draft_history.json` + `exact_draft_slots.json`) / `draft_history.json` | não |
| `ordem_draft [ano]` / `max_pf [semana]` | `DraftOrderService` | Sleeper (picks trocadas, lineups ótimos) | não |
| `waivers [alvo] [ano] [limite]` | `kb_queries.waivers_report` | `league.db` (tabela `waiver_moves`, de `waivers_market_classified.json`) | não |
| `radar_waivers` | `generate_wednesday_waiver_radar` | Sleeper trending + rosters | não |
| `transacoes [semana]` | `sleeper_cli transactions` (nomes, só concluídas, semana atual + anterior) | Sleeper transactions | não |
| `placar_nfl [semana]` | `fetch_espn_scoreboard` | ESPN | não |
| `noticias_nfl` | `get_league_injury_bulletin` + `get_latest_news` | status ao vivo + notícias | não |
| `data_lab alvo [semana]` | `data_lab.query` | nflverse | cache em `nfl_data/` |
| `notas` | `admin_notes.read_notes` | `ADMIN_NOTES.md` | não |
| `avaliar_troca alvo` | `trade_values.evaluate` | FantasyCalc | cache em `nfl_data/` |
| `power_ranking` | `WeeklyScheduler.generate_tuesday_power` | Sleeper | não |
| `chances_playoff` | `analytics.playoff_odds` | Sleeper (calendário futuro) | não |
| `waivers_semana [limite=horas]` | `waiver_report.build` | Sleeper (concluídas + falhas) | não |

## 7. Mensagens recorrentes (cron do Hermes)

Os jobs ficam em `data/cron/jobs.json` (não versionado); a fonte da verdade é [ops/crons.sh](../ops/crons.sh).
Todos são `no-agent` com entrega `local`: os scripts enviam as mensagens pelo `EvolutionClient` e só marcam como
enviado quando o Evolution confirma (`key.id`). Falha → nova tentativa no próximo tick enquanto a janela do slot
estiver aberta, e um aviso único no DM do admin (`ADMIN_ALERT_JID`). Logs em `data/logs/league-cron.log`.

| Job | Agenda | O que faz |
|---|---|---|
| `editorial-slots` | `*/5 * * * *` | `WeeklyScheduler.check_and_dispatch()` — calendário abaixo |
| `trades-watch` | `*/15 * * * *` | trade nova → veredito via OpenRouter (fallback determinístico) → anúncio → só então grava em `trades_history.json` |
| `league-sync` | `*/30 * * * *` | refresh da base de jogadores (6 h; 1 h qui/dom/seg), snapshot de lesões pré-kickoff, waivers (qui/dom 03–10h, leg atual + anterior), consolidação da semana (terça, só com todos os jogos finalizados na ESPN), correções de stats (qua/qui) |
| `data-lab-raio-x` | `*/30 11-21 * * 2` | Raio-X da rodada que acabou, quando o nflverse tiver todos os jogos |
| `data-lab-tendencias` | `0 12 * * 3` | tendências de uso |
| `data-lab-matchup` | `0 10 * * 5` | laboratório do confronto da semana |
| `session-reset` | `0 5 * * *` | encerra a sessão de cada grupo parada há 2 h+ (a próxima mensagem abre uma nova) |
| `live-watch` | `*/5 * * * *` | com jogo ao vivo: 🚑 titular da liga que sai machucado e 🔄 viradas de confronto (limite diário `LIVE_MAX_PER_DAY`) |
| `provider-watch` | `17 * * * *` | testa o modelo do chat e avisa o admin quando falha (402/401/429) e quando volta |

### 7.1 Calendário editorial (horário de Brasília)

Chave de dedupe: `{temporada}_W{semana Sleeper}_{slot}` em `dispatched_slots.json`. Cada slot pode sair até 60 min
depois do horário de início. Os horários de domingo vêm da ESPN ([league_time.py](../league/league_time.py)).

| Dia | Hora | Slot | Conteúdo |
|---|---|---|---|
| Ter | 08h | `tue_08h_recap` → `standings` → `power` → `playoff_odds` → `draft_order` | fechamento, classificação, power ranking (all-play, sorte, banco), chances de playoff (a partir da semana `PLAYOFF_ODDS_FROM_WEEK`=8), ordem do próximo draft |
| Ter | 10h | `tue_10h_round_injuries` | lesões da rodada (o relatório médico saiu do bloco das 08h para não empilhar mensagens) |
| Ter | 11–21h | Data Lab Raio-X | |
| Qua | 08h | `wed_08h_waiver_radar` | adds em alta livres na liga (top 100 da Sleeper, só QB/RB/WR/TE: não há vaga para K, DEF nem IDP) + FAAB (orçamento lido da liga) |
| Qua | 12h | Data Lab Tendências | |
| Qui | 08h | `opening_08h_waivers` | todas as movimentações das últimas 30 h, gasto por time, disputas e lances perdidos |
| Qui | 09h | `opening_09h_standings` | classificação por divisão |
| Qui | 10h–15h | `opening_matchup_1..6` | card por confronto: projeção/chance, campanha atual, H2H, boletim médico (status ao vivo), clima do confronto (só fatos), palpite |
| Sex | 10h | Data Lab Matchup | |
| Sáb | 20h | `sat_20h_lineup_check` | jogos cedo de domingo com os titulares envolvidos + pendências por time (OUT/IR, bye, vaga vazia, Questionable com sugestão do banco); DM a quem tem telefone em `LEAGUE_PEOPLE_PHONES` (`LINEUP_DM`) |
| Dom | 08h | `sun_08h_waivers_recap` | waivers das últimas 30 h + cronograma real (avisa jogo internacional/cedo) |
| Dom | 09h | `sun_09h_matchups_update` | parciais + projeções + jogo cedo |
| Dom | 10h | `sun_10h_injuries` | boletim médico por confronto + horários de inativos de cada janela |
| Dom | jogo cedo −90 min | `sun_inactives_HHMM` | alerta de inativos só dos times do jogo cedo/internacional (não sai se não houver titular OUT) |
| Dom | kickoff principal −120 min | `sun_11h_trashtalk` | aquecimento com fatos (duelo mais parelho, maior favorito, maior vantagem) |
| Dom | kickoff principal −90 min | `sun_11h30_inactives` | titulares com status oficial Out/IR/Doubtful/Sus cujo jogo ainda não começou + sugestão do banco |
| Dom | fim da 1ª onda (~16h30–17h00; 17h30–18h00 sem horário de verão nos EUA) | `sun_after_wave1` | balanço de todos os confrontos: definidos, encaminhados/abertos com quem falta jogar (e quando), destaques do dia |
| Dom | fim da 2ª onda (~20h–21h15) | `sun_after_wave2` | idem, antes do Sunday Night |
| Dom/Seg | fim do SNF (~00h–01h50) | `sun_after_snf` | idem; "decide no Monday Night" |
| Seg | 08h | `mon_08h_decisions` | especial MNF |

Os três balanços do domingo usam as ondas da ESPN pelo horário de Nova York (1ª onda 13h ET, 2ª 16h ET, SNF 20h ET):
cada um sai assim que **todos os jogos da onda estão finalizados**, nunca antes de 2h30 após o último kickoff da onda e,
no máximo, 5 min antes da onda seguinte (o pós-SNF tem prazo de 4h30 após o kickoff). Isso ajusta sozinho semana a semana,
inclusive na troca de horário dos EUA (semana 8 em diante, uma hora mais tarde em Brasília).

Todas as mensagens usam o layout de `msg_format.Msg` e saem em várias partes quando longas; um slot enviado pela
metade retoma da parte que falhou. Enquete por duelo na quinta: implementada e **desligada** (`POLLS_ENABLED=0`;
a contagem de acertos depende de eventos de voto do Evolution e não foi implementada).

Prévia de qualquer slot sem enviar: `python3 scheduler_service.py <recap|standings|power|playoffs|draft_order|radar|waivers|termometro|sabado|sun_waivers|sun_matchups|sun_injuries|trashtalk|inactives|onda1|onda2|pos_snf|monday|matchup N>`.

## 8. Base de conhecimento

Uma cópia só de cada arquivo da base, em `league/knowledge_base/` (as cópias antigas da raiz foram removidas).
Isto vale para a liga #1; cada liga extra tem a sua em `data/profiles/<slug>/league/knowledge_base/` (gerada por
`kb_bootstrap`, ver [MULTILIGA.md](MULTILIGA.md) §6) e o estado em `.../league/state/`.

**Camada SQLite.** As consultas de histórico não leem mais Markdown: os arquivos-fonte abaixo (versionados, escritos pelos crons) são
indexados em `league.db` e todo leitor usa SQL. O índice se reconstrói sozinho (atômico, com `flock`) quando um fonte muda, então os
produtores não precisam de gancho. Tabelas: `franchises`, `franchise_seasons`, `trades`/`trade_sides`/`trade_assets`, `games`
(2 linhas por confronto), `draft_picks`, `waiver_moves`, `players`, `profiles`. A consulta `estatisticas` monta o SQL a partir de
fragmentos fixos (entidade × agrupar_por × metrica) — o modelo nunca escreve SQL e nunca conta linhas de uma lista.

| Arquivo | Quem atualiza | Quando | Situação |
|---|---|---|---|
| `matchups_history.json` | `check_and_sync_weekly_results` / `reconcile_week` | terça (todos os jogos finais) / qua-qui (correções) | automático; **fonte dos jogos** (conferida 1:1 com `/matchups/{semana}` do Sleeper) |
| `brackets_history.json` | `h2h_builder.refresh_brackets` (chamado no fechamento de cada semana de playoff) | semana ≥ 15 | automático; `winners_bracket`/`losers_bracket` do Sleeper: define quem jogou Wild Card, Semi, Final, 3º/5º lugar e Toilet Bowl (só jogos decididos) |
| `h2h_matrix.json`, `H2H_RIVALRIES.md` | `h2h_builder.build_matrix` (a cada `_save_history`) | junto com `matchups_history.json` | **derivados** — nunca editar nem incrementar à mão; `python3 league/h2h_builder.py --rebuild --write` regenera |
| `trades_history.json` | `check_and_process_trades` (`kb_db.append_trade`) | trade nova | **fonte da verdade das trocas** (uma por linha) |
| `league.db` | `kb_db.ensure_fresh()` (qualquer consulta) | quando uma fonte muda (tamanho/mtime) | índice SQLite reconstruível, não versionado; `python3 kb_db.py rebuild` força, `python3 kb_db.py export` gera `TRADE_HISTORY.md` legível |
| `waivers_activity.json`, `waivers_market_classified.json`, `WAIVERS_AND_FA_DOSSIER.md` | `check_and_process_waivers` + `build_full_waivers_database` | qui/dom 03–10h | automático (leg atual + anterior) |
| `teams_rosters_analysis.json`, `TEAMS_AND_ROSTERS_ANALYSIS.md`, `players_registry.json`, `PLAYERS_DOSSIER.md` | `build_team_analysis`, `build_players_knowledge_base` | após trades/waivers/semana | automático (pontos da temporada inteira, FAAB da liga, status ao vivo) |
| `injuries_registry.json`, `INJURY_REPORT.md` | `sync_injury_intelligence(persist=True)` | boletim de domingo 10h | automático (status ao vivo) |
| `draft_order_projected.json`, `DRAFT_ORDER_AND_MAX_PF.md` | `sync_draft_knowledge_base` | terças | automático |
| `news_cache.json`, `injury_snapshots.json`, `round_injuries_cache.json`, `jev_cache.json`, `nfl_data/` | serviços | sob demanda | caches |
| `master_nfl_players.json` | `players_db.refresh()` (league-sync) | 6 h / 1 h em dia de jogo | **ao vivo** — nome, time e status oficial de lesão (não versionado) |
| `league_players.json` (14/09) | ninguém | — | legado; só preenche dados antigos, nunca sobrescreve o master |
| `franchises_history.json`, `playoffs_history.json`, `seasons_history.json`, `draft_history.json`, `exact_draft_slots.json`, `DRAFT_HISTORY.md` (curadoria, só leitura humana), `MANAGERS_PSYCHOLOGICAL_PROFILES.md` | manual | — | histórico estático |
| `LEAGUE_DOSSIER.md`, `MANAGERS_AND_TEAMS.md`, `DIVISIONS_AND_STANDINGS.md`, `PLAYOFFS_AND_TOILET_BOWL.md`, `standings_and_divisions.json`, `chat_history_whatsapp.txt` (14 MB), `chat_stats_summary.json`, `deep_profiles_raw.json` | — | — | **não lidos por nenhum código** (herança do bot legado) |

Arquivos de estado (fora do git): `dispatched_slots.json`, `dispatch_failures.json`, `data_lab_dispatched.json`,
`knowledge_base/synced_transactions.json`, `knowledge_base/synced_weeks.json`, caches de notícias/lesões e o master de jogadores.

## 9. Fontes externas

| Fonte | Uso |
|---|---|
| Sleeper API | estado da NFL, liga, usuários, rosters, matchups, transações, stats/projeções, trending, picks trocadas |
| ESPN (site.api) | placar/estado dos jogos, resumos pós-jogo (lesões), notícias, overview por atleta |
| RSS | Rotowire, FantasyPros, Yahoo NFL, CBS Sports |
| nflverse (GitHub releases) | play-by-play, Next Gen Stats, snaps, PFR |
| DynastyProcess | mapa de IDs entre plataformas |
| OpenRouter | fallback do chat, veredito de trades, frases do Data Lab (`jev-router`) e triagem de notícias (`Jev decisions`) |
| Antigravity (`agy`, via adaptador) + OpenRouter (gratuitos) | modelo do chat e das chamadas do motor (veredito de troca, frases, Data Lab) |

## 10. Variáveis de ambiente (`data/.env`)

`EVOLUTION_API_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE`, `EVOLUTION_WEBHOOK_TOKEN`, `EVOLUTION_WEBHOOK_PORT`,
`EVOLUTION_ALLOWED_GROUPS`, `EVOLUTION_HOME_CHANNEL`, `EVOLUTION_DM_ALLOWED`, `EVOLUTION_ADMINS`, `EVOLUTION_BOT_IDS`,
`EVOLUTION_MENTION_REGEX`, `EVOLUTION_ALLOW_ALL_USERS`, `WHATSAPP_GROUP_JID` (destino dos templates do cron),
`EVOLUTION_RATE_LIMIT`, `ADMIN_ALERT_JID` (DM que recebe alertas de falha), `LEAGUE_PEOPLE_PHONES` (`roster:fone,fone;…`),
`GEMINI_API_KEY`/`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `JEV_MODEL`, `COMMENT_MODEL`, `TRADE_MODEL` (opcional),
`API_SERVER_HOST`, `API_SERVER_KEY`, `HERMES_DASHBOARD_*`, `SESSION_RESET_IDLE_MIN` (opcional),
`LLM_MODEL`/`LLM_FALLBACK_MODEL` (chamadas do motor), `FLAVOR_DISABLED`, `PLAYOFF_ODDS_FROM_WEEK`, `LINEUP_DM`,
`LIVE_ALERTS`/`LIVE_MAX_PER_DAY`/`LIVE_LEAD_MIN`/`LIVE_DRY_RUN`, `POLLS_ENABLED` (padrão desligado).

Por liga extra (`data/profiles/<slug>/.env`, 600): `LEAGUE_ADMINS`, `LEAGUE_PEOPLE_PHONES`, `LEAGUE_DM_ALLOWED`,
`ADMIN_ALERT_JID` e as chaves do modelo (o Hermes isola segredos por perfil). Desenvolvimento/CLI: `LEAGUE_HOME`
(liga corrente), `SLEEPER_API_BASE` (Sleeper alternativa), `LIGA_DATA` / `LIGA_CONTAINER` (`ops/liga.py`).

## 11. Operação

```bash
C="docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v $PWD:$PWD -w $PWD docker:cli docker compose -p mr-irrelevant"
$C up -d                                   # aplica mudanças do compose / recarrega .env (recria)
docker restart mr-irrelevant               # recarrega config.yaml / SOUL.md / plugins / código do motor
docker exec -u hermes mr-irrelevant /opt/hermes/.venv/bin/hermes cron list
tail -f data/logs/agent.log                # log principal (gateway.log só tem o resumo)
tail -f data/logs/league-cron.log          # o que os crons enviaram / falharam
python3 -m unittest discover -s tests      # testes offline
python3 tests/e2e/e2e.py run               # ponta a ponta no gateway real (container descartável, sem rede)
ops/liga.py list | doctor                  # ligas, formatos, rotas e consistência (outra liga: docs/MULTILIGA.md)
docker run --rm --network services_internal -v $PWD:$PWD -w $PWD python:3.12-slim \
  python tests/fire.py "pergunta"          # simula uma mensagem no grupo de teste
docker exec -u hermes -w /opt/league mr-irrelevant /opt/hermes/.venv/bin/python -c \
  'import liga_core; print(liga_core.run_query("tabela", {}))'   # testa uma consulta da ferramenta
```

## Alertas pessoais por DM (escalação e placar ao vivo)

Só para o admin que tem time (`EVOLUTION_ADMINS` ∩ `LEAGUE_PEOPLE_PHONES`), sempre por DM — [watch_prefs.py](../league/watch_prefs.py).

| Job (cron 5 min) | Script | O que faz |
|---|---|---|
| `lineup-advice` | [lineup_tick.py](../data/scripts/lineup_tick.py) → [lineup_advisor.py](../league/lineup_advisor.py) | Análise de escalação **incremental** (1 chamada ao modelo por mensagem): dias com jogo exceto dom/seg → 08h e 1h antes de cada janela de kickoff que envolve o elenco (qui, Thanksgiving, Natal…); domingo → 08h e 1h antes do 1º jogo; segunda → igual, só se houver o que mexer. Inclui trocas, os `max_subs` AutoSubs do Sleeper e o que mudou desde a análise anterior (estado em `lineup_state.json`) |
| `live-score` | [live_score_tick.py](../data/scripts/live_score_tick.py) → [live_score.py](../league/live_score.py) | **Sem IA.** Compara pontos/estatísticas dos titulares do meu time e do adversário a cada tick; junta as jogadas (um jogador por linha, com jardas/TDs do intervalo) e manda a cada ≥ 10 min (jogada de ≥ 6 pts sai na hora); placar sozinho a cada 30 min enquanto houver jogo; mensagem final quando todos terminam |

Parar/continuar (determinístico no adapter, sem LLM, só DM do admin): `parar placar` / `continuar placar`, `parar escalação` / `continuar escalação`, `parar notificações` / `continuar notificações`. A instrução vai no rodapé de cada mensagem. Variáveis: `LINEUP_ADVICE=0`, `LIVE_SCORE=0`, `LIVE_SCORE_MIN_GAP` (min), `LIVE_SCORE_HEARTBEAT` (min), `LIVE_SCORE_BIG` (pts), `LINEUP_DRY_RUN=1` / `LIVE_SCORE_DRY_RUN=1`.
