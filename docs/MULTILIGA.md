# Multiliga — um bot por liga, na mesma instância

> Como criar o bot de **outra liga** (outro grupo de WhatsApp, mesma instância do Evolution e mesmo container),
> passando só o **id da liga no Sleeper**, com tudo isolado da liga #1. Visão geral do sistema: [ARQUITETURA.md](ARQUITETURA.md).

## 1. Em 3 minutos

```bash
ops/liga.py groups                                   # JIDs dos grupos em que o número do bot está
ops/liga.py new minha-liga --league-id 1234567890123456789 \
    --group 120363XXXXXXXXXXXX@g.us --admin 5548999990000 --admin-name "Fulano"
ops/liga.py cron minha-liga --apply                  # agenda os jobs da liga no container
docker restart mr-irrelevant                         # o gateway passa a rotear o grupo para a liga
ops/liga.py doctor                                   # confere rotas, perfis, grupos repetidos...
```

O `new` lê a liga na Sleeper (o id aparece na URL da liga), cria o perfil `data/profiles/minha-liga/`, escreve a
persona com a **estrutura de elenco e as regras daquela liga**, constrói a base de conhecimento a partir do histórico
(temporadas anteriores via `previous_league_id`) e grava as rotas do gateway em `data/config.yaml`.
Depois é só colocar o número do bot no grupo e chamá-lo.

| Quer | Faça |
|---|---|
| ver as ligas, formatos e rotas | `ops/liga.py list` · `ops/liga.py show <slug>` |
| a liga mudou regra / managers trocaram de nome | `ops/liga.py refresh <slug>` (reescreve só as seções gerenciadas da persona) |
| refazer o histórico | `ops/liga.py bootstrap <slug>` |
| apelidos, horários, regras da casa | editar `data/profiles/<slug>/league.yaml` (§4) e `refresh` |
| tirar a liga | `ops/liga.py remove <slug> --yes` e `docker restart mr-irrelevant` |
| outro nome de container | `--container NOME` ou `LIGA_CONTAINER` |

## 2. Modelo

Uma liga = um **perfil do Hermes** (`data/profiles/<slug>/`: config, persona, segredos, sessões, cron e base de
conhecimento próprios). O gateway do perfil default (que já conecta o WhatsApp) atende todos os perfis
(`gateway.multiplex_profiles: true`) e despacha cada grupo para o seu perfil (`gateway.profile_routes`, por `chat_id`).
A liga #1 continua sendo o perfil default, com os dados onde sempre estiveram (`legacy_layout: true`).

```
 WhatsApp ─ Evolution (1 instância) ─ webhook ─▶ adapter evolution-whatsapp ── resolve a liga pelo JID do grupo
                                                        │  (travas de isolamento: §5)
                                                        ▼
                                Hermes gateway (perfil default) ── profile_routes ──┬─▶ perfil default  = liga #1
                                                                                    ├─▶ perfil minha-liga
                                                                                    └─▶ perfil outra-liga
        por turno: HERMES_HOME = home do perfil  →  persona (SOUL.md), sessões, ferramenta `liga` e cron da liga certa
```

```
data/                          perfil default = liga #1 (league.yaml com legacy_layout: true)
├── league.yaml  config.yaml  SOUL.md  .env  state.db  cron/ …
├── plugins/  scripts/  skills/mr-irrelevant/        código e skills — compartilhados (symlinks nos perfis)
└── profiles/<slug>/                                 uma liga
    ├── league.yaml     identidade, regras da casa, horários, pessoas       (versionado)
    ├── config.yaml     modelo, agente, toolsets (derivado do default)      (versionado)
    ├── SOUL.md         persona gerada com as regras DESTA liga             (versionado)
    ├── .env (600)      admins, telefones, alerta; chaves do modelo         (nunca versionado)
    ├── league/knowledge_base/   histórico, índice SQLite, notas do admin
    ├── league/state/            despacho dos slots, falhas, cache do perfil Sleeper
    └── sessions/ state.db cron/ image_cache/ …  (do Hermes, por perfil)
```

## 3. O que vem do Sleeper (você não configura)

`LeagueProfile` ([league_profile.py](../league/league_profile.py)) é lido de `GET /league/{id}` (cache de 6 h em disco; se a
Sleeper cair o bot segue com a última cópia e avisa no log). O motor **pergunta ao perfil** em vez de assumir a liga #1:

| Do Sleeper | Efeito no bot |
|---|---|
| `roster_positions`: titulares, FLEX / SUPER_FLEX / REC_FLEX / WRRB_FLEX / IDP_FLEX, banco, IR, taxi | texto da persona; **só se sugere jogador que cabe num slot** (sem K/DEF/IDP onde não há vaga); Max PF = melhor escalação possível *desta* liga; dossiê de elenco |
| pontuação (PPR, TE premium, TD de passe…) | descrição de formato; valores de troca (SuperFlex, nº de times, PPR) |
| `type` (redraft / keeper / dynasty), `best_ball` | persona, picks e draft só onde fazem sentido; "reserva não conta" × escalação automática |
| playoffs: times, semana de início, 1 ou 2 semanas por rodada, `trade_deadline` | rodapé da tabela, chances de playoff, rótulos de Toilet Bowl, quantos cards de confronto (N/2) |
| `waiver_type` / `waiver_budget` | FAAB × prioridade em radar, relatórios e persona |
| divisões e seus nomes | tabela por divisão |
| usuários e rosters (time, manager, dono) | nomes e apelidos automáticos; tag `[Nome \| Time]` de quem pergunta |
| temporadas anteriores (`previous_league_id`) | histórico: jogos, brackets, H2H, drafts, trocas, waivers |

## 4. O que NÃO vem do Sleeper: `league.yaml`

Só o que a Sleeper não sabe. Tudo é opcional.

```yaml
slug: minha-liga
sleeper_league_id: '1234567890123456789'
name: Minha Liga                       # senão, o nome na Sleeper
timezone: America/Sao_Paulo
group_jid: 120363XXXXXXXXXXXX@g.us     # (groups: [...] para mais de um)
persona: {name: Mr. Irrelevant, aka: "", mention_regex: "mr[\\s.]*irrelevant|irrelevant", admin_name: Fulano}
rules:                                 # regras da casa
  draft_order: max_pf                  # ordem do rookie draft pelo Max PF (liga a mensagem de terça)
  max_pf_rounds: [1, 2]                # rodadas decididas pelo Max PF
  playoff_odds_from_week: 8            # a partir de qual semana publicar "chances de playoff"
  toilet_bowl: true                    # força/zera o bracket de consolação (senão: se há jogos nele no histórico)
schedule: {tue_08h_power: "09:30"}     # move um slot semanal
disable_slots: [wed_08h_waiver_radar]  # remove um slot
people:                                # nomes/apelidos por roster (o resto vem da Sleeper)
  4: {name: Fulano de Tal, username: fulano, team: Time do Fulano, nicks: [fulano, ful]}
manager_aliases: {Handle_Antigo: HandleNovo}
division_icons: {'1': 🔥, '2': ❄️}     # sem isto: 📍
lore: [O comissário é o Beltrano.]     # fatos extras na persona
```

Slots semanais: `tue_08h_recap`, `tue_08h_standings`, `tue_08h_power`, `tue_08h_playoff_odds`, `tue_08h_draft_order`,
`tue_10h_round_injuries`, `wed_08h_waiver_radar`, `sat_20h_lineup_check`, `sun_08h_waivers_recap`,
`sun_09h_matchups_update`, `sun_10h_injuries`, `sun_11h_trashtalk`, `sun_11h30_inactives`, `sun_after_wave1/2/snf`,
`mon_08h_decisions` ([scheduler_service.py](../league/scheduler_service.py)).

Segredos e telefones ficam em `data/profiles/<slug>/.env`: `LEAGUE_ADMINS`, `LEAGUE_PEOPLE_PHONES` (`roster:fone,fone;…`),
`LEAGUE_DM_ALLOWED`, `ADMIN_ALERT_JID`. Admin de uma liga **não** é admin da outra.

## 5. Isolamento: o que é separado, o que é compartilhado e o que trava

| Por liga | Como |
|---|---|
| persona, regras, managers | `SOUL.md` do perfil + `LeagueProfile` + `league.yaml` |
| conversas | `state.db` / `sessions/` do perfil (chave de sessão `agent:<perfil>:…`) |
| base de conhecimento, notas do admin, índice SQLite | `<home>/league/knowledge_base/` |
| estado de despacho, falhas, cache de lesões/curiosidades, mídia | `<home>/league/state/`, `<home>/image_cache/` |
| segredos, admins, telefones, alerta | `<home>/.env` (a liga nova **nunca** lê o ambiente do processo) |
| cache da ferramenta `liga` | a chave inclui a liga |
| cron | `hermes -p <slug> cron …`: o job roda com `HERMES_HOME` do perfil |

Compartilhado de propósito (é NFL, não da liga): banco mestre de jogadores, notícias, ESPN, nflverse, valores de troca;
a conexão do WhatsApp (credenciais do Evolution ficam só no perfil default); a imagem, o código e as skills.

Como o isolamento é garantido (não é só convenção):

1. **Contexto por chamada** — a liga corrente é um `ContextVar` (`league_ctx.use`), então turnos simultâneos de ligas
   diferentes no mesmo processo nunca se enxergam; nenhum módulo guarda estado de liga em variável global.
2. **Trava de rota no adapter** — uma mensagem de uma liga extra só é respondida se o gateway a servir *pelo perfil
   dela*. Rota faltando, multiplex desligado ou gateway sem reiniciar → a mensagem é **descartada** (log
   `message dropped to keep leagues isolated`) em vez de ser respondida com a persona e os dados da liga #1.
3. **Rotas de DM** cobrem todas as grafias do chat (dígitos, JID, 9º dígito brasileiro, LID); DM de número não
   listado é bloqueada.
4. **Falha fechada**: `ops/liga.py doctor` aponta grupo repetido, liga repetida, rota ausente, `.env` aberto, perfil estimado.
5. **Testes**: unitários offline (`tests/test_isolation.py`, `test_league_ctx.py`, `test_kb_bootstrap.py`, `test_liga_cli.py`)
   e o teste ponta a ponta no gateway real do Hermes (§7).

## 6. Base de conhecimento de uma liga nova

`kb_bootstrap` ([kb_bootstrap.py](../league/kb_bootstrap.py)) escreve, a partir só da Sleeper, os mesmos arquivos-fonte que o
índice e os leitores usam na liga #1: temporadas, franquias, jogos (temporada regular e playoffs), brackets oficiais,
H2H, drafts (e a ordem real dos slots), trocas (jogadores, picks, FAAB), waivers e o dossiê de elencos. As transações
passadas entram já como "conhecidas": o monitor ao vivo **nunca reanuncia histórico**. É idempotente.

Não gera (na liga #1 são curados à mão ou vêm do histórico do chat; os leitores degradam sem erro): perfis psicológicos
dos managers, registro de linhagem de jogadores e as faixas de qualidade de waiver. Para ter perfis, escreva o
`MANAGERS_PSYCHOLOGICAL_PROFILES.md` na pasta `league/knowledge_base/` da liga, com uma seção `### N. Nome` por manager.

## 7. Teste ponta a ponta (rode ao subir a versão do Hermes ou mexer em adapter/plugin/CLI)

```bash
python3 tests/e2e/e2e.py run     # ~5 min; precisa de docker; nada de rede, nada de produção
```

Sobe um container descartável **com a mesma imagem fixada** do compose (`--network none`), com WhatsApp, LLM e Sleeper
falsos, cria uma segunda liga com `ops/liga.py new` e confere pelo gateway real (~50 verificações): cada grupo recebe a
persona e os dados da *sua* liga; DM e LID roteados; rota ausente = descarta; notas, conversas, gráficos, cron e
agenda editorial por liga; a liga #1 com saída idêntica à de antes (rodapé de playoffs, ícones). Os testes unitários
não enxergam o roteamento do Hermes — este sim (foi ele que achou a DM que cairia na liga errada).

## 8. Limites e decisões

- **Um número de WhatsApp / uma instância** servem todas as ligas; cada liga precisa do **seu** grupo. O mesmo grupo
  em duas ligas é recusado.
- **Habilitar o multiplex é uma mudança de comportamento do gateway**: ocorre no primeiro `new` (bloco gerenciado em
  `data/config.yaml`, que `sync` regenera) e exige `docker restart`. A liga #1 mantém chaves de sessão, cron e dados.
- **Chaves do modelo** são copiadas para o `.env` do perfil (o Hermes isola segredos por perfil); as do Evolution não.
- **Mesma cadeia de modelos** para todas as ligas (vem do `config.yaml` do default quando o perfil é criado; depois
  o `config.yaml` do perfil é seu).
- Jobs globais (`provider-watch`) existem uma vez por instalação, não por liga.
- O bot é de NFL: ligas de outros esportes da Sleeper não são suportadas.
- Imagem do Hermes **fixada por digest**: upgrade é deliberado e termina rodando o §7.

## 9. Mapa de arquivos

| Arquivo | Papel |
|---|---|
| [league/league_profile.py](../league/league_profile.py) | regras e estrutura de elenco lidas da Sleeper (slots, FLEX, K/DEF/IDP, FAAB, playoffs) |
| [league/league_ctx.py](../league/league_ctx.py) | qual liga atende a chamada; onde estão seus dados/segredos; resolução por grupo/DM |
| [league/people.py](../league/people.py) | managers, apelidos, telefones — por liga |
| [league/kb_bootstrap.py](../league/kb_bootstrap.py) | base de conhecimento a partir do id da liga |
| [ops/liga.py](../ops/liga.py) · [ops/templates/SOUL.md.tmpl](../ops/templates/SOUL.md.tmpl) | CLI de operação e molde da persona |
| [data/league.yaml](../data/league.yaml) | liga #1 (perfil default) |
| [tests/e2e/](../tests/e2e) | teste ponta a ponta em container descartável |
