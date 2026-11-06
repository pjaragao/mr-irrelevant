#!/usr/bin/env python3
"""Songs about the league: research brief (`musica`) + audio (`cantar`) — a native skill of the bot.

Flow used by the model when someone asks for a rap/music/parody:
  1. liga consulta=musica alvo=<manager|jogador> [alvo2=<rival>] [tema=derrotas]
        -> a fact sheet built from everything the bot knows (history DB, H2H, records, trades, waivers, draft,
           profile, injuries/news) + writing rules;
  2. the model writes the lyrics (one verse per line);
  3. liga consulta=cantar letra="<verses>"  -> Edge TTS (pt-BR Antonio) line by line, each verse fitted to two bars
           of a synthesized boom-bap beat -> Ogg/Opus voice note; the tool returns a `MEDIA:` line.
CLI:  python3 song.py brief eduohira [--tema derrotas] | python3 song.py sing letra.txt [--send] [--estilo lento]
"""

from __future__ import annotations

import array
import asyncio
import concurrent.futures
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import wave
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import kb_db  # noqa: E402
import kb_queries  # noqa: E402

SR = 22050
STYLES = {"rap": 110, "lento": 92}          # BPM; one verse = two bars
MAX_LINES, MAX_LINE_CHARS, MAX_TOTAL_CHARS = 40, 140, 2400
KEEP_DAYS = 7
DEFAULT_VOICE = "pt-BR-AntonioNeural"


# --------------------------------------------------------------------------- brief
def _games(con, rid: int):
    return con.execute("SELECT * FROM games WHERE roster_id=? AND result IS NOT NULL ORDER BY season, week", (rid,)).fetchall()


def _fmt(g) -> str:
    return f"{g['season']} S{g['week']} vs {g['opp_manager']}: {g['pf']:.1f} x {g['pa']:.1f}"


def _manager_brief(con, rid: int, tema: str) -> List[str]:
    from knowledge_service import KnowledgeBaseService
    f = con.execute("SELECT * FROM franchises WHERE roster_id=?", (rid,)).fetchone()
    name = f["manager"]
    gs = _games(con, rid)
    if not gs:
        return [f"{name}: sem jogos registrados."]
    W = sum(g["result"] == "W" for g in gs)
    L = sum(g["result"] == "L" for g in gs)
    lines = [f"## {name} ({f['real_name']}) — time {f['current_team']}",
             f"Campanha total (com playoffs): {len(gs)} jogos, {W} vitórias e {L} derrotas ({W / len(gs) * 100:.0f}%)."]
    try:
        m = KnowledgeBaseService().get_managers_summary().get(name.lower(), {})
        if m:
            lines.append(f"Temporada regular {m['seasons_range']}: {m['wins']}V-{m['losses']}D; títulos: {m['titles']}; vices: {m['runner_ups']}.")
    except Exception:
        pass
    per = []
    for yr in sorted({g["season"] for g in gs}):
        reg = [g for g in gs if g["season"] == yr and not g["is_playoff"]]
        po = [g for g in gs if g["season"] == yr and g["bracket"] == "winners"]
        tb = [g for g in gs if g["season"] == yr and g["bracket"] == "losers"]
        s = f"{yr}: {sum(g['result'] == 'W' for g in reg)}-{sum(g['result'] == 'L' for g in reg)}"
        if po:
            s += f" (+{sum(g['result'] == 'W' for g in po)}-{sum(g['result'] == 'L' for g in po)} no playoff)"
        if tb:
            s += f" (+{sum(g['result'] == 'W' for g in tb)}-{sum(g['result'] == 'L' for g in tb)} no Toilet Bowl)"
        per.append(s)
    lines.append("Por temporada (V-D): " + " | ".join(per))

    rivals = con.execute("""SELECT opp_manager o, SUM(result='W') w, SUM(result='L') l, COUNT(*) n FROM games
                            WHERE roster_id=? AND result IS NOT NULL GROUP BY 1""", (rid,)).fetchall()
    beat_me = sorted(rivals, key=lambda r: (-r["l"], r["w"]))[:4]
    i_beat = sorted(rivals, key=lambda r: (-r["w"], r["l"]))[:3]
    never = [r for r in rivals if r["w"] == 0 and r["n"] >= 3]
    lines.append("Quem mais venceu " + name + ": " + "; ".join(f"{r['o']} {r['l']}x{r['w']} em {r['n']} jogos" for r in beat_me))
    lines.append("Quem ele mais venceu: " + "; ".join(f"{r['o']} {r['w']}x{r['l']}" for r in i_beat))
    if never:
        lines.append("Nunca venceu (3+ jogos): " + "; ".join(f"{r['o']} (0x{r['l']})" for r in never))

    big_w = max((g for g in gs if g["result"] == "W"), key=lambda g: g["pf"] - g["pa"], default=None)
    big_l = max((g for g in gs if g["result"] == "L"), key=lambda g: g["pa"] - g["pf"], default=None)
    low = min(gs, key=lambda g: g["pf"])
    high = max(gs, key=lambda g: g["pf"])
    close_l = min((g for g in gs if g["result"] == "L"), key=lambda g: g["pa"] - g["pf"], default=None)
    close_w = min((g for g in gs if g["result"] == "W"), key=lambda g: g["pf"] - g["pa"], default=None)
    marks = [("Maior vitória", big_w), ("Maior derrota", big_l), ("Menor pontuação", low), ("Maior pontuação", high),
             ("Derrota mais apertada", close_l), ("Vitória mais apertada", close_w)]
    lines.append("Marcas: " + " | ".join(f"{k}: {_fmt(g)}" for k, g in marks if g))
    po_l = [g for g in gs if g["bracket"] == "winners" and g["result"] == "L"]  # title bracket only, not Toilet Bowl
    if po_l:
        lines.append("Derrotas de playoff: " + "; ".join(_fmt(g) for g in po_l[:4]))
    last = gs[-3:]
    lines.append("Últimos jogos: " + "; ".join(f"{g['season']} S{g['week']} {g['result']} ({g['pf']:.0f}x{g['pa']:.0f})" for g in last))

    tr = con.execute("SELECT COUNT(*) FROM trade_sides WHERE roster_id=?", (rid,)).fetchone()[0]
    r1 = con.execute("SELECT COUNT(*) FROM trade_assets WHERE roster_id=? AND kind='pick' AND pick_round=1", (rid,)).fetchone()[0]
    lines.append(f"Trocas: participou de {tr} (recebeu {r1} picks de 1ª rodada no total).")
    w = con.execute("""SELECT SUM(type='waiver' AND status='complete') won, SUM(type='waiver' AND status='failed') lost,
                       SUM(CASE WHEN status='complete' THEN bid ELSE 0 END) faab FROM waiver_moves WHERE roster_id=?""", (rid,)).fetchone()
    steal = con.execute("SELECT add_label, bid, season FROM waiver_moves WHERE roster_id=? AND tier=5 AND status='complete' ORDER BY bid, ts DESC LIMIT 1", (rid,)).fetchone()
    burn = con.execute("SELECT add_label, bid, season FROM waiver_moves WHERE roster_id=? AND tier=0 AND status='complete' ORDER BY bid DESC LIMIT 1", (rid,)).fetchone()
    lines.append(f"Waivers: {w['won'] or 0} ganhos, {w['lost'] or 0} lances perdidos, ${w['faab'] or 0} de FAAB."
                 + (f" Melhor achado: {steal['add_label']} (${steal['bid']}, {steal['season']})." if steal else "")
                 + (f" Maior queima: {burn['add_label']} (${burn['bid']}, {burn['season']})." if burn else ""))
    first = kb_queries._first_season(con)    # the first draft is the startup: only rookie drafts have a "first round"
    picks = con.execute("SELECT season, player_name FROM draft_picks WHERE roster_id=? AND round=1 AND season>? ORDER BY season, pick_no", (rid, first)).fetchall()
    if picks:
        lines.append("1ªs rodadas de draft: " + "; ".join(f"{p['season']} {p['player_name']}" for p in picks[:8]))
    prof = con.execute("SELECT body FROM profiles WHERE roster_id=?", (rid,)).fetchone()
    if prof:
        keep = [l for l in prof[0].splitlines() if l.strip() and not l.lstrip().startswith("###")
                and not re.search(r"ID Sleeper|Nome Can|Franquia\(s\)|Volume no Chat|Texto\*|Caps Lock", l)]
        body = re.sub(r"[*`_#]+", "", " ".join(keep))
        lines.append("Perfil no grupo (brinque com leveza, não invente além disto): " + " ".join(body.split())[:520])
    try:  # live state: injured starters
        from news_service import NewsService
        risk = [s for s in NewsService()
                .get_league_injury_bulletin()["starters_at_risk"] if str(s.get("manager", "")).lower() == name.lower()]
        if risk:
            lines.append("Agora: titulares em risco — " + ", ".join(f"{s['name']} ({s['official_status']})" for s in risk[:4]))
    except Exception:
        pass
    return lines


_STAGE_SHORT = {"Wild Card": "WC", "Semi PO": "Semi", "FINAL 🏆": "Final", "3º Lugar 🥉": "3º lugar", "5º Lugar": "5º lugar"}


def _h2h_brief(con, a: int, b: int) -> List[str]:
    na, nb = kb_queries.manager_name(con, a), kb_queries.manager_name(con, b)
    gs = con.execute("SELECT * FROM games WHERE roster_id=? AND opp_roster_id=? AND result IS NOT NULL ORDER BY season, week", (a, b)).fetchall()
    if not gs:
        return [f"Confronto {na} x {nb}: nunca se enfrentaram."]
    wa = sum(g["result"] == "W" for g in gs)
    big = max(gs, key=lambda g: abs(g["pf"] - g["pa"]))
    close = min(gs, key=lambda g: abs(g["pf"] - g["pa"]))
    po = [g for g in gs if g["bracket"] == "winners"]
    tb = [g for g in gs if g["bracket"] == "losers"]
    out = [f"## Confronto direto {na} x {nb}: {wa}x{len(gs) - wa} para {na} em {len(gs)} jogos.",
           f"Maior surra: {_fmt(big)} (visto por {na}). Mais apertado: {_fmt(close)}.",
           "Últimos: " + "; ".join(f"{g['season']} S{g['week']} {g['result']} {g['pf']:.0f}x{g['pa']:.0f}" for g in gs[-4:])]
    if po:
        out.append("Em playoff (mata-mata do título): " + "; ".join(
            f"{g['season']} {_STAGE_SHORT.get(g['stage'], g['stage'])} {g['result']}" for g in po)
            + f" — {sum(g['result'] == 'W' for g in po)}x{sum(g['result'] == 'L' for g in po)} para {na}")
    if tb:
        out.append("No Toilet Bowl: " + "; ".join(f"{g['season']} S{g['week']} {g['result']}" for g in tb))
    return out


def _league_brief(con) -> List[str]:
    import league_ctx
    lctx = league_ctx.current()
    since = kb_queries._first_season(con)
    lines = [f"## A liga {lctx.name} ({lctx.profile.format_label()}" + (f", desde {since}" if since else "") + ")"]
    rec = kb_queries.records_report(con).splitlines()
    lines += [l for l in rec if l.startswith("• #1 ")][:3]
    lines.append("Trocas na história: " + str(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]))
    top = con.execute("SELECT manager, COUNT(*) n FROM trade_sides GROUP BY 1 ORDER BY n DESC LIMIT 1").fetchone()
    lines.append(f"Maior negociador: {top['manager']} ({top['n']} trocas)")
    return lines


def brief(alvo: str = "", alvo2: str = "", tema: str = "") -> str:
    con = kb_db.connect()
    try:
        body: List[str] = []
        a = kb_queries.resolve_roster(con, alvo) if alvo else None
        b = kb_queries.resolve_roster(con, alvo2) if alvo2 else None
        if a:
            body += _manager_brief(con, a, tema)
            if b:
                body += [""] + _h2h_brief(con, a, b)
        elif alvo:  # a player
            from knowledge_service import KnowledgeBaseService
            body.append(f"## Jogador: {alvo}")
            try:
                body.append(" ".join(KnowledgeBaseService().get_player_dossier(alvo).split())[:1500])
            except Exception:
                pass
            try:
                from sleeper_client import SleeperClient
                news = SleeperClient().get_player_news(alvo)
                for n in (news.get("recent_news") or [])[:3]:
                    body.append(f"Notícia: {n.get('headline', '')}")
                if news.get("injury_status") not in (None, "Active", "Healthy", "None"):
                    body.append(f"Status médico oficial: {news['injury_status']}")
            except Exception:
                pass
        else:
            body += _league_brief(con)
        who = f"{alvo}{' x ' + alvo2 if alvo2 else ''}" or "a liga"
    finally:
        con.close()
    focus = f" Foco do pedido: {tema}." if tema else ""
    rules = (
        "\n\nCOMO ESCREVER (siga à risca):\n"
        f"• Gênero rap em PT-BR sobre {who}.{focus} Use SÓ os fatos acima; não invente jogo, placar ou história.\n"
        "• 20 a 28 versos, um por linha, 9 a 13 sílabas, rimas em pares (AABB). Refrão de 4 versos no começo, repetido no fim.\n"
        "• Tom ácido de amigos de liga: zoe o fantasy (escalação, derrota, waiver), nunca a vida pessoal além do perfil acima.\n"
        "• Números por extenso e arredondados (\"cento e dezessete\", não 117,6). Sem siglas: escreva \"semana catorze\", não S14.\n"
        "• Depois chame liga consulta=cantar com letra=<os versos separados por quebra de linha> e responda SÓ com a linha "
        "MEDIA devolvida (a música vai como áudio no grupo). Se pediram apenas a letra, responda a letra em texto e não chame cantar.")
    return "MATERIAL PARA A MÚSICA\n" + "\n".join(body) + rules


# --------------------------------------------------------------------------- lyrics -> speech text
_UN = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez", "onze", "doze", "treze",
       "catorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]
_TENS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
_HUND = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos", "setecentos", "oitocentos", "novecentos"]


def num2pt(n: int) -> str:
    """Integer 0..999999 in Brazilian Portuguese words (the voice reads digits unreliably, e.g. years)."""
    if n < 20:
        return _UN[n]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t] + (f" e {_UN[u]}" if u else "")
    if n == 100:
        return "cem"
    if n < 1000:
        h, r = divmod(n, 100)
        return _HUND[h] + (f" e {num2pt(r)}" if r else "")
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        head = "mil" if k == 1 else f"{num2pt(k)} mil"
        if not r:
            return head
        return head + (" e " if r < 100 or r % 100 == 0 else " ") + num2pt(r)
    return str(n)


def speak_text(line: str) -> str:
    s = re.sub(r"[*_`#>\[\]]", "", line)
    s = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", s)
    s = re.sub(r"\bS(\d{1,2})\b", lambda m: "semana " + num2pt(int(m.group(1))), s)

    def dec(m):
        a, b = m.group(1), m.group(2)
        return f"{num2pt(int(a))} vírgula {' '.join(num2pt(int(d)) for d in b)}"
    s = re.sub(r"(\d{1,6})[.,](\d{1,2})\b(?!\d)", dec, s)
    s = re.sub(r"\d{1,6}", lambda m: num2pt(int(m.group(0))), s)
    s = s.replace("%", " por cento")
    return re.sub(r"\s+", " ", s).strip()


def parse_lyrics(letra: str) -> List[str]:
    if not letra or not letra.strip():
        raise ValueError("`letra` vazia: escreva os versos, um por linha.")
    if len(letra) > MAX_TOTAL_CHARS:
        raise ValueError(f"A letra passa de {MAX_TOTAL_CHARS} caracteres; encurte (20 a 28 versos bastam).")
    lines = []
    for raw in letra.replace("\\n", "\n").splitlines():
        t = raw.strip()
        if not t or re.fullmatch(r"[\[(][^\])]{1,30}[\])]:?", t) or re.fullmatch(r"(?i)(refr[aã]o|verso\s*\d*|intro|ponte|outro)\s*:?", t):
            continue  # blank lines and section labels such as [Refrão]
        if len(t) > MAX_LINE_CHARS:
            raise ValueError(f"Verso grande demais ({len(t)} caracteres): quebre em versos de até {MAX_LINE_CHARS}.")
        lines.append(t)
    if len(lines) < 4:
        raise ValueError("Preciso de pelo menos 4 versos (um por linha).")
    if len(lines) > MAX_LINES:
        raise ValueError(f"No máximo {MAX_LINES} versos (recebi {len(lines)}).")
    return lines


# --------------------------------------------------------------------------- audio
def out_dir() -> str:
    import league_ctx
    d = os.environ.get("LIGA_SONGS_DIR")
    if not d:
        ctx = league_ctx.current()
        home = ctx.home or os.environ.get("HERMES_HOME")
        d = os.path.join(home, "cache", "audio", "liga_musicas") if home else os.path.join(BASE_DIR, "charts_out", "musicas")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup(d: str) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def _voice() -> str:
    if os.environ.get("LIGA_TTS_VOICE"):
        return os.environ["LIGA_TTS_VOICE"]
    try:
        import yaml
        import league_ctx
        home = league_ctx.current().home or os.environ.get("HERMES_HOME", "/opt/data")   # the league's own profile config
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
            return (((yaml.safe_load(f) or {}).get("tts") or {}).get("edge") or {}).get("voice") or DEFAULT_VOICE
    except Exception:
        return DEFAULT_VOICE


def _edge():
    try:
        import edge_tts
        return edge_tts
    except ImportError:
        try:  # Hermes installs optional SDKs on demand into its lazy-packages dir
            from tools.lazy_deps import ensure
            ensure("tts.edge", prompt=False)
            import edge_tts
            return edge_tts
        except Exception as exc:
            raise RuntimeError("Voz indisponível (edge-tts não instalado).") from exc


def _synth_lines(texts: List[str], workdir: str) -> List[str]:
    """Edge TTS, all lines concurrently, in a private thread/event loop (the gateway already runs one)."""
    edge = _edge()
    voice = _voice()

    async def one(i: int, t: str) -> str:
        p = os.path.join(workdir, f"l{i}.mp3")
        await edge.Communicate(t, voice, rate="+8%", pitch="-2Hz").save(p)
        return p

    async def all_():
        sem = asyncio.Semaphore(8)

        async def guarded(i, t):
            async with sem:
                return await one(i, t)
        return await asyncio.gather(*[guarded(i, t) for i, t in enumerate(texts)])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(all_())).result(timeout=150)


def _run(*a) -> None:
    subprocess.run(list(a), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)


def _read_wav(path: str) -> array.array:
    with wave.open(path) as w:
        a = array.array("h")
        a.frombytes(w.readframes(w.getnframes()))
        return a


def _write_wav(path: str, samples: array.array) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(samples.tobytes())


def beat_loop(bpm: int, bars: int = 8) -> array.array:
    """Boom-bap loop: kick on 1 and 3.5, snare on 2/4, 8th-note hats, a minor bass line (A A C G, 2 bars each)."""
    beat_len = 60.0 / bpm
    bar = 4 * beat_len
    n = int(bars * bar * SR)
    buf = [0.0] * n
    rnd = random.Random(7)

    def add(start, samples, gain):
        s = int(start * SR)
        for k, v in enumerate(samples):
            if s + k < n:
                buf[s + k] += v * gain
    ts = lambda secs: (k / SR for k in range(int(secs * SR)))  # noqa: E731
    kick = [math.sin(2 * math.pi * (50 + 110 * math.exp(-t * 28)) * t) * math.exp(-t * 9) for t in ts(0.30)]
    snare = [(rnd.uniform(-1, 1) * 0.8 + math.sin(2 * math.pi * 190 * t) * 0.5) * math.exp(-t * 22) for t in ts(0.2)]
    hat, prev = [], 0.0
    for k in range(int(0.05 * SR)):
        x = rnd.uniform(-1, 1)
        hat.append((x - prev) * math.exp(-k / SR * 70))
        prev = x
    roots = [55.0, 55.0, 65.4, 49.0]
    for b in range(bars):
        t0 = b * bar
        add(t0, kick, 0.9)
        add(t0 + 2.5 * beat_len, kick, 0.7)
        add(t0 + beat_len, snare, 0.55)
        add(t0 + 3 * beat_len, snare, 0.55)
        for h in range(8):
            add(t0 + h * beat_len / 2, hat, 0.22 if h % 2 else 0.30)
        f = roots[(b // 2) % 4]
        bass = [math.sin(2 * math.pi * f * t) * math.exp(-t * 2.2) * 0.45 for t in ts(1.4)]
        add(t0, bass, 0.8)
        add(t0 + 2 * beat_len, bass, 0.6)
    peak = max(abs(v) for v in buf) or 1
    return array.array("h", [int(max(-1, min(1, v / peak * 0.9)) * 32767) for v in buf])


def sing(letra: str, estilo: str = "rap") -> Dict[str, Any]:
    """Lyrics (one verse per line) -> Ogg/Opus voice note over a beat. Raises ValueError/RuntimeError with a clear message."""
    lines = parse_lyrics(letra)
    estilo = (estilo or "rap").strip().lower()
    if estilo not in STYLES:
        raise ValueError(f"`estilo` inválido. Opções: {' | '.join(STYLES)}.")
    bpm = STYLES[estilo]
    bar = 4 * 60.0 / bpm
    d = out_dir()
    _cleanup(d)
    with tempfile.TemporaryDirectory(prefix="song_") as wd:
        mp3s = _synth_lines([speak_text(t) for t in lines], wd)
        intro, tail = 2, 2
        total_bars = intro + 2 * len(lines) + tail
        voice = array.array("h", [0] * int(total_bars * bar * SR))
        for i, mp3 in enumerate(mp3s):
            raw, fit = os.path.join(wd, f"r{i}.wav"), os.path.join(wd, f"f{i}.wav")
            _run("ffmpeg", "-y", "-i", mp3, "-ar", str(SR), "-ac", "1", raw)
            dur = len(_read_wav(raw)) / SR
            tempo = max(0.85, min(1.35, dur / (2 * bar * 0.95)))
            _run("ffmpeg", "-y", "-i", raw, "-filter:a", f"atempo={tempo:.3f}", "-ar", str(SR), "-ac", "1", fit)
            clip = _read_wav(fit)
            start = int((intro + 2 * i) * bar * SR + 0.10 * SR)
            for k, v in enumerate(clip):
                if start + k >= len(voice):
                    break
                voice[start + k] = max(-32768, min(32767, voice[start + k] + v))
        vw, bw = os.path.join(wd, "voice.wav"), os.path.join(wd, "beat.wav")
        _write_wav(vw, voice)
        _write_wav(bw, beat_loop(bpm))
        secs = total_bars * bar
        name = re.sub(r"[^a-z0-9]+", "-", lines[0].lower())[:28].strip("-") or "musica"
        out = os.path.join(d, f"{name}-{datetime.now():%Y%m%d-%H%M%S}.ogg")
        _run("ffmpeg", "-y", "-i", vw, "-stream_loop", "-1", "-i", bw, "-t", f"{secs:.2f}", "-filter_complex",
             "[0:a]volume=1.6,acompressor=threshold=-18dB:ratio=3[v];[1:a]volume=0.5[b];"
             "[v][b]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-16:TP=-1.5[o]",
             "-map", "[o]", "-c:a", "libopus", "-b:a", "48k", "-ac", "1", out)
    return {"path": out, "seconds": round(secs), "lines": len(lines), "voice": _voice(), "estilo": estilo}


def media_reply(res: Dict[str, Any]) -> str:
    return (f"🎤 Música pronta: {res['lines']} versos, {res['seconds'] // 60}:{res['seconds'] % 60:02d}, voz {res['voice']}.\n"
            "Responda APENAS com a linha abaixo (a música vai como nota de voz no chat), sem repetir a letra:\n"
            f"MEDIA:{res['path']}")


def main(argv: List[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("brief", "sing"):
        print(__doc__)
        return 2
    opt = lambda k, d="": next((argv[i + 1] for i, a in enumerate(argv) if a == k and i + 1 < len(argv)), d)  # noqa: E731
    if argv[1] == "brief":
        print(brief(argv[2], opt("--com"), opt("--tema")))
        return 0
    try:
        with open(argv[2], encoding="utf-8") as f:
            res = sing(f.read(), opt("--estilo", "rap"))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print(res)
    if "--send" in argv:
        from evolution_client import EvolutionClient, sent_ok
        cli = EvolutionClient(os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080"), os.environ.get("EVOLUTION_API_KEY", ""),
                              os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot"))
        ok = sent_ok(cli.send_audio(opt("--dest") or __import__("league_ctx").current().group_jid, res["path"]))
        print("enviado:", ok)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
