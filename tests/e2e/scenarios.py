#!/usr/bin/env python3
"""Scenarios for the multi-league gateway (run after `e2e.py up`; see e2e.py). Prints PASS/FAIL per check, exit 1 on any
FAIL. Every question goes through the real Hermes gateway: webhook -> adapter -> profile route -> agent -> `liga` tool
-> reply, with the LLM, Sleeper and WhatsApp mocked (mocks.py)."""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import e2e  # noqa: E402

RUN, LOGS, NAME = e2e.RUN, e2e.LOGS, e2e.NAME
G1, G2 = e2e.GROUP_ONE, e2e.GROUP_TWO
ADMIN_B = "5548900000002"
PY = "/opt/hermes/.venv/bin/python"
HERMES = "/opt/hermes/.venv/bin/hermes"
PROFILE_B = "/opt/data/profiles/liga-falsa"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  -> {str(detail)[:600]}" if (detail and not ok) else ""), flush=True)


def dexec(*cmd, env=None, user=None, timeout=300):
    base = ["docker", "exec"]
    for k, v in (env or {}).items():
        base += ["-e", f"{k}={v}"]
    if user:
        base += ["-u", user]
    return subprocess.run(base + [NAME] + list(cmd), text=True, capture_output=True, timeout=timeout)


def post(jid, sender, push, text, mid=None):
    args = ["/mocks/post_webhook.py", jid, sender, push, text] + ([mid] if mid else [])
    return dexec(PY, *args, env={"EVOLUTION_WEBHOOK_TOKEN": "e2e-hook"}).stdout.strip()


def read(name):
    p = os.path.join(LOGS, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def sends(since=0, kind="sendText"):
    return [r for r in read("evolution.jsonl")[since:] if kind in r["path"]]


def ask(jid, text, sender="5548900000011", push="Marcio", timeout=75, to=None):
    """Webhook in, wait for the reply sent to `to` (default: the same chat). -> (reply text or None, llm records)."""
    n_e, n_l = len(read("evolution.jsonl")), len(read("llm.jsonl"))
    post(jid, sender, push, text)
    to = to or jid
    t0 = time.time()
    while time.time() - t0 < timeout:
        got = [r for r in sends(n_e) if r["body"].get("number") == to]
        if got:
            time.sleep(0.5)
            got = [r for r in sends(n_e) if r["body"].get("number") == to]
            return "\n".join(r["body"]["text"] for r in got), read("llm.jsonl")[n_l:]
        time.sleep(0.5)
    return None, read("llm.jsonl")[n_l:]


def sysprompt(recs):
    return next((r["system"] for r in recs if r["tools"]), "")


def slurp(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def transcript_hits(db, needle):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("select count(*) from messages where content like ?", (f"%{needle}%",)).fetchone()[0]
    finally:
        con.close()


def main():
    notes_b = os.path.join(RUN, "data/profiles/liga-falsa/league/knowledge_base/ADMIN_NOTES.md")
    notes_a = os.path.join(RUN, "league/knowledge_base/ADMIN_NOTES.md")
    # ------------------------------------------------------------ 1. each group is answered by ITS league
    txt_a, rec_a = ask(G1, "Mr Irrelevant, mostra a tabela")
    check("A: reply delivered to group A", txt_a is not None)
    check("A: tool data is league #1", txt_a and "HIGH STAKES FANTASY" in txt_a, txt_a)
    check("A: persona is league #1's SOUL.md", "High Stakes Fantasy" in sysprompt(rec_a) and "Liga Falsa" not in sysprompt(rec_a))
    check("A: league #1 keeps its curated division icon", txt_a and "🇪🇸" in txt_a, txt_a)
    # league #1 runs on its cached real profile (6 playoff teams from week 15, consolation 7th-12th): the footer is
    # byte-for-byte what the old hardcoded text said
    check("A: league #1 post-season footer unchanged", txt_a and "SEMANAS 15 A 17" in txt_a and
          "Top 6 da Geral disputam o anel e taça de campeão" in txt_a and
          "7º ao 12º disputam o título de consolação e a fuga da lanterna" in txt_a, txt_a)

    txt_b, rec_b = ask(G2, "Mr Irrelevant, mostra a tabela")
    check("B: reply delivered to group B", txt_b is not None)
    check("B: tool data is Liga Falsa", txt_b and "LIGA FALSA" in txt_b and "HIGH STAKES" not in txt_b, txt_b)
    check("B: persona is Liga Falsa's SOUL.md", "Liga Falsa" in sysprompt(rec_b) and "High Stakes" not in sysprompt(rec_b))
    check("B: nothing of league #1 (flags, Top 6, 7º ao 12º)", txt_b and "🇪🇸" not in txt_b and "SEMANAS 15" not in txt_b
          and "Top 6" not in txt_b and "7º ao 12º" not in txt_b, txt_b)
    check("B: post-season footer comes from Sleeper (4 teams, weeks 3-4)", txt_b and "Top 4" in txt_b and
          "SEMANAS 3 A 4" in txt_b and "Toilet" not in txt_b, txt_b)
    txt, _ = ask(G2, "Mr Irrelevant, me mostra meu time")
    check("B: roster dossier follows B's starting slots (9) and K/DEF",
          txt and "9 Titulares" in txt and "*Ks*" in txt and "*DEFs*" in txt, txt)

    # ------------------------------------------------------------ 2. unknown group / DM gating and routing
    n_l = len(read("llm.jsonl"))
    r, _ = ask("120363999999999999@g.us", "Mr Irrelevant, tabela", timeout=6)
    check("unknown group: ignored (no reply, no model call)", r is None and len(read("llm.jsonl")) == n_l)
    r, _ = ask("5548900000555@s.whatsapp.net", "Mr Irrelevant, tabela", sender="5548900000555", timeout=6)
    check("DM from an unlisted number: blocked", r is None)
    dm = f"{ADMIN_B}@s.whatsapp.net"
    r, rec = ask(dm, "tabela", sender=ADMIN_B, push="Ana", to=dm)
    check("DM from league B's admin: answered by league B", r is not None and "LIGA FALSA" in r and "HIGH STAKES" not in r, r)
    check("DM from league B's admin: B's persona", "Liga Falsa" in sysprompt(rec))
    r, _ = ask(dm, "anota aí: nota enviada por DM", sender=ADMIN_B, push="Ana", to=dm)
    check("DM note by B's admin (admin of ITS league only): acknowledged and stored in B, not in #1",
          r is not None and "Anotado" in r and "nota enviada por DM" in slurp(notes_b) and
          "nota enviada por DM" not in slurp(notes_a), r)
    n_e = len(read("evolution.jsonl"))
    body = {"event": "messages.upsert", "data": {"key": {"remoteJid": "98765432109876@lid", "remoteJidAlt": dm,
                                                          "fromMe": False, "id": f"E2ELIDDM{int(time.time())}"},
                                                 "pushName": "Ana", "message": {"conversation": "tabela"}}}
    dexec(PY, "-c", "import json,urllib.request;urllib.request.urlopen(urllib.request.Request("
          "'http://127.0.0.1:8660/webhook/evolution',data=json.dumps(%r).encode(),"
          "headers={'Content-Type':'application/json','X-Webhook-Token':'e2e-hook'}))" % (body,))
    t0 = time.time()
    while time.time() - t0 < 60 and not sends(n_e):
        time.sleep(0.5)
    reply = "\n".join(r["body"]["text"] for r in sends(n_e))
    check("DM through a LID (phone in remoteJidAlt): answered by league B", "LIGA FALSA" in reply and "HIGH STAKES" not in reply, reply)

    # ------------------------------------------------------------ 3. fail closed when the route is missing
    cfg_path = os.path.join(RUN, "data", "config.yaml")
    original = slurp(cfg_path)
    try:
        broken = re.sub(r"(?m)^    - \{name: liga-falsa, .*\n", "", original)
        check("(setup) the managed block had B's group route", broken != original)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(broken)
        time.sleep(1.1)
        n_l = len(read("llm.jsonl"))
        r, _ = ask(G2, "Mr Irrelevant, mostra a tabela", timeout=8)
        check("group B without a route: dropped, never answered by league #1", r is None and len(read("llm.jsonl")) == n_l, r)
    finally:
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(original)
        time.sleep(1.1)
    r, _ = ask(G2, "Mr Irrelevant, mostra a tabela")
    check("route restored: group B answers again", r is not None and "LIGA FALSA" in r, r)

    # ------------------------------------------------------------ 4. admin notes: per league, per admin
    r, _ = ask(G2, "Irrelevant, anota aí: o prêmio da Liga Falsa é uma cerveja gelada", sender=ADMIN_B, push="Ana")
    check("B admin note acknowledged", r is not None and "Anotado" in r, r)
    check("B note stored in B's knowledge base", "cerveja" in slurp(notes_b))
    check("B note NOT in league #1's knowledge base", "cerveja" not in slurp(notes_a))
    r_a, _ = ask(G1, "Mr Irrelevant, quais as notas?")
    check("A does not see B's note", r_a is not None and "cerveja" not in r_a, r_a)
    r_b, rec = ask(G2, "Irrelevant, quais as notas?")
    check("B sees its own note (tool and injected context)", r_b is not None and "cerveja" in r_b, r_b)
    check("B note is injected into B's prompt context only", any("cerveja" in json.dumps(x) for x in rec))
    ask(G1, "Irrelevant, anota aí: hackeado pela liga B", sender=ADMIN_B, push="Ana", timeout=20)
    check("B's admin is NOT an admin in league A", "hackeado" not in slurp(notes_a))

    # ------------------------------------------------------------ 5. concurrency
    out = {}

    def worker(key, jid, who):
        out[key] = ask(jid, f"Mr Irrelevant, tabela {who}", sender=f"55489000010{who}")[0]
    ts = [threading.Thread(target=worker, args=(f"{g}{i}", jid, i)) for i in range(3) for g, jid in (("A", G1), ("B", G2))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = all(out.get(f"A{i}") and "HIGH STAKES" in out[f"A{i}"] and "LIGA FALSA" not in out[f"A{i}"] and
             out.get(f"B{i}") and "LIGA FALSA" in out[f"B{i}"] and "HIGH STAKES" not in out[f"B{i}"] for i in range(3))
    def tag(v):
        return None if v is None else ("A" if "HIGH STAKES" in v else "B" if "LIGA FALSA" in v else "?")
    check("6 simultaneous questions (3 per group): every answer from the right league", ok,
          {k: tag(v) for k, v in sorted(out.items())})

    # ------------------------------------------------------------ 6. charts: own home, own group
    for tag, jid, home in (("A", G1, "data"), ("B", G2, "data/profiles/liga-falsa")):
        n_e = len(read("evolution.jsonl"))
        post(jid, "5548900000011", "Marcio", "Mr Irrelevant, faz um grafico de trocas por semana")
        t0 = time.time()
        while time.time() - t0 < 90 and not sends(n_e, "sendMedia"):
            time.sleep(1)
        media = sends(n_e, "sendMedia")
        check(f"{tag}: chart image delivered to its own group", bool(media) and all(m["body"]["number"] == jid for m in media))
        pngs = [f for _r, _d, fs in os.walk(os.path.join(RUN, home, "image_cache")) for f in fs if f.endswith(".png")]
        check(f"{tag}: chart written under its own home", bool(pngs), home)

    # ------------------------------------------------------------ 7. persisted conversations
    db_a = os.path.join(RUN, "data/state.db")
    db_b = os.path.join(RUN, "data/profiles/liga-falsa/state.db")
    check("B transcripts live in B's own session DB", transcript_hits(db_b, "LIGA FALSA") > 0 and transcript_hits(db_b, "cerveja") > 0)
    check("B transcripts are NOT in the default session DB", transcript_hits(db_a, "LIGA FALSA") == 0 and transcript_hits(db_a, "cerveja") == 0)
    check("A transcripts are NOT in B's session DB", transcript_hits(db_a, "HIGH STAKES") > 0 and transcript_hits(db_b, "HIGH STAKES") == 0)

    # ------------------------------------------------------------ 8. the weekly editorial schedule
    for state in (os.path.join(RUN, "league/dispatched_slots.json"),
                  os.path.join(RUN, "data/profiles/liga-falsa/league/state/dispatched_slots.json")):
        if os.path.exists(state):
            os.remove(state)
    n_e = len(read("evolution.jsonl"))
    ticks = {}
    for tag, home in (("A", "/opt/data"), ("B", PROFILE_B)):
        r = dexec(PY, "/mocks/run_slots.py", "2026-10-13", "08:05", user="hermes",
                  env={"HERMES_HOME": home, "LEAGUE_DIR": "/opt/league"})
        ticks[tag] = r.stdout
    sent = [(r["body"]["number"], r["body"]["text"]) for r in sends(n_e)]
    to_a = [t for n, t in sent if n == G1]
    to_b = [t for n, t in sent if n == G2]
    check("Tuesday 08h: league #1 sends its draft order (house rule max_pf)", any("ORDEM PROJETADA DO DRAFT" in t for t in to_a), ticks["A"])
    check("Tuesday 08h: league B has no draft-order slot (no such house rule)", not any("DRAFT" in t.upper() and "ORDEM" in t.upper() for t in to_b) and bool(to_b), ticks["B"])
    check("Tuesday 08h: each league's slots reached only its own group", all(n in (G1, G2) for n, _ in sent) and bool(to_a) and bool(to_b))
    check("Tuesday 08h: B's power ranking counts B's opponents (3), not 11",
          any("contra os 3 times" in t for t in to_b) and not any("contra os 11" in t for t in to_b))
    done_a = slurp(os.path.join(RUN, "league/dispatched_slots.json"))
    done_b = slurp(os.path.join(RUN, "data/profiles/liga-falsa/league/state/dispatched_slots.json"))
    check("dispatch state is written in each league's own directory", "tue_08h_recap" in done_a and "tue_08h_recap" in done_b)

    # ------------------------------------------------------------ 9. operator CLI against the container
    cli = [sys.executable, os.path.join(e2e.REPO, "ops", "liga.py"), "--data", os.path.join(RUN, "data"), "--container", NAME]
    g = subprocess.run(cli + ["groups"], text=True, capture_output=True)
    check("`liga.py groups` lists the instance's groups and marks the used ones",
          g.returncode == 0 and "[em uso]" in g.stdout and "Terceira Liga" in g.stdout and
          not re.search(r"Terceira Liga.*\[em uso\]", g.stdout), g.stdout + g.stderr)
    c = subprocess.run(cli + ["cron", "liga-falsa", "--apply"], text=True, capture_output=True)
    listing = dexec(HERMES, "-p", "liga-falsa", "cron", "list", user="hermes").stdout
    check("`liga.py cron --apply` registers the league's jobs in ITS profile",
          c.returncode == 0 and "liga-falsa-editorial-slots" in listing and "liga-falsa-session-reset" in listing, c.stderr[-300:])
    check("global jobs are not duplicated per league", "provider-watch" not in listing and "eleicao-watch" not in listing)

    # ------------------------------------------------------------ 10. Hermes' own cron ticks every profile
    open(os.path.join(LOGS, "cron_probe.jsonl"), "w").close()
    for prof in ([], ["-p", "liga-falsa"]):
        dexec(HERMES, *prof, "cron", "create", "* * * * *", "--name", "e2e-probe", "--script", "e2e_probe.py",
              "--no-agent", "--deliver", "local", "--failure-deliver", "local", user="hermes")
    t0 = time.time()
    while time.time() - t0 < 150 and len(read("cron_probe.jsonl")) < 2:
        time.sleep(5)
    probes = read("cron_probe.jsonl")
    pa = next((p for p in probes if p["HERMES_HOME"] == "/opt/data"), {})
    pb = next((p for p in probes if p["HERMES_HOME"].endswith("liga-falsa")), {})
    check("cron tick, default profile: league #1", pa.get("league_id") == e2e.LEAGUE_ONE_ID and pa.get("group") == G1, pa)
    check("cron tick, profile liga-falsa: its league, group and directories", pb.get("league_id") == "9002" and
          pb.get("group") == G2 and "profiles/liga-falsa" in pb.get("kb_dir", "") and
          "profiles/liga-falsa" in pb.get("state_dir", ""), pb)
    check("cron job of a profile: Evolution from the default env, model key from its own .env",
          bool(pb.get("evolution_url")) and bool(pb.get("has_model_key")), pb)
    for prof in ([], ["-p", "liga-falsa"]):                       # leave the scheduler quiet
        dexec(HERMES, *prof, "cron", "remove", "e2e-probe", user="hermes")

    # ------------------------------------------------------------ 11. daily session reset is per profile
    for home in ("/opt/data", PROFILE_B):
        r = dexec(PY, f"{home}/scripts/session_reset.py", user="hermes",
                  env={"HERMES_HOME": home, "SESSION_RESET_IDLE_MIN": "0", "LEAGUE_DIR": "/opt/league"})
        check(f"session reset runs in {home}", r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-300:])

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
