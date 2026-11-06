"""Cron (no_agent, a cada minuto em 4–6/10 e 25–27/10): apuração oficial do TSE → WhatsApp, sem IA.

Presidente a cada mudança do % apurado (no 2º turno, com a projeção do resultado final e o gráfico logo depois); governadores e senadores eleitos (mensagens separadas) a cada 20 min; deputados de SC: uma mensagem ao chegar a
100% apurado (ver league/eleicao_tse.py).
Destino: ELEICAO_JID (grupo ou número; default ADMIN_ALERT_JID), relido do data/.env a cada rodada — trocar o
destino não exige reiniciar o bot (o env do container só muda num `up -d`). Desliga sozinho no fim da eleição (ver league/eleicao_tse.py).
ELEICAO_DRY_RUN=1 imprime o quadro atual sem enviar nem gravar estado. ELEICAO=0 desliga. Silencioso (stdout vazio)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("eleicao")
if os.environ.get("ELEICAO", "1").lower() in ("0", "false", "no", "off"):
    sys.exit(0)

import eleicao_tse  # noqa: E402
from evolution_client import EvolutionClient, sent_ok  # noqa: E402

DRY = os.environ.get("ELEICAO_DRY_RUN") == "1"


def env_file(key: str) -> str:
    """Valor atual de `key` no .env do HERMES_HOME (o os.environ é o do boot do container)."""
    try:
        with open(os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), ".env"), encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == key:
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""


JID = env_file("ELEICAO_JID") or os.environ.get("ELEICAO_JID") or os.environ.get("ADMIN_ALERT_JID", "")
if not (JID or DRY):
    log.error("sem destino: defina ELEICAO_JID ou ADMIN_ALERT_JID")
    sys.exit(0)

evo = EvolutionClient()


def send(text: str) -> bool:
    if DRY:
        print("--- DRY RUN ---\n" + text + "\n")
        return True
    ok = sent_ok(evo.send_message(JID, text))
    log.info("envio %s → %s: %s", "ok" if ok else "FALHOU", JID.split("@")[0][-4:], text.splitlines()[0][:80])
    return ok


def send_image(path: str) -> bool:
    if DRY:
        print(f"--- DRY RUN (imagem) --- {path}\n")
        return True
    ok = sent_ok(evo.send_image(JID, path))
    log.info("gráfico %s → %s", "ok" if ok else "FALHOU", JID.split("@")[0][-4:])
    return ok


state = eleicao_tse.load_state()
if DRY:  # mostra o quadro atual, mesmo a 0%, sem tocar no estado real
    state.update(armed=True, sent_pst={}, cache={}, backoff_until=0)
    eleicao_tse.tick(state, lambda t: True, force=True)
    races = eleicao_tse.races_for(state)
    now = eleicao_tse.datetime.now(eleicao_tse.BRT)
    proj = eleicao_tse.compute_projection(state["turn"], state, summ=state["summ"].get("pres")) if state["turn"] == 2 else None
    if state["turn"] == 2 and [r.key for r in races] == ["pres"]:
        send(eleicao_tse.format_board_t2(state["summ"].get("pres"), state.get("reg"), proj, now))
        if proj:
            send_image(eleicao_tse.render_chart(state["summ"].get("pres"), proj, state.get("proj_hist"), now))
    else:
        send(eleicao_tse.format_update(state["turn"], races, state["summ"], now, state.get("reg"), proj))
    if state["turn"] == 1:
        allsum = eleicao_tse.load_many(eleicao_tse.GOV + eleicao_tse.SENADO + eleicao_tse.DEPUTADOS, state)
        send(eleicao_tse.format_governors(allsum, now))
        send(eleicao_tse.format_senators(allsum, now))
        for r in eleicao_tse.DEPUTADOS:
            if r.key in allsum:
                known = eleicao_tse.deputies_known(allsum[r.key])
                send(eleicao_tse.format_elected_deputies(r, allsum[r.key], now) if known
                     else f"(deputados {r.key}: TSE ainda não marcou os eleitos — {allsum[r.key]['pst']}%)")
        for uf in dict.fromkeys(r.uf for r in eleicao_tse.DEPUTADOS):
            send(eleicao_tse.format_projected_deputies(uf, allsum, [r for r in eleicao_tse.DEPUTADOS if r.uf == uf], now))
    sys.exit(0)
def send_link(text: str) -> bool:  # convite: com o card do link do grupo ("Ver grupo")
    ok = sent_ok(evo.send_message(JID, text, link_preview=True))
    log.info("convite %s → %s", "ok" if ok else "FALHOU", JID.split("@")[0][-4:])
    return ok


eleicao_tse.tick(state, send, send_image=send_image, send_link=send_link)
eleicao_tse.save_state(state)
