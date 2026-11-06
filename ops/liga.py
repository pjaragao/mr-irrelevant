#!/usr/bin/env python3
"""Multi-league control for this installation: one WhatsApp number/Evolution instance, one bot per league.

A league is a Hermes *profile* (`data/profiles/<slug>/`: its own config, persona, secrets, sessions, cron, knowledge
base) served by the same gateway; the WhatsApp group of each league is routed to its profile (`profile_routes`).
Everything about a league's roster structure and rules is read from Sleeper — you only give the league id.

    ops/liga.py new <slug> --league-id ID --group 1203…@g.us [--admin 5548…] [--persona "Mr. Irrelevant"]
    ops/liga.py list                    leagues, groups, formats, route status
    ops/liga.py show <slug>             the rules + people this league's bot works with
    ops/liga.py sync                    regenerate gateway.profile_routes in data/config.yaml from every league.yaml
    ops/liga.py bootstrap <slug>        (re)build the knowledge base from Sleeper (history, trades, drafts...)
    ops/liga.py refresh <slug>          re-read rules/people from Sleeper and update the persona's managed sections
    ops/liga.py cron <slug> [--apply]   print/register the league's scheduled jobs inside the running container
    ops/liga.py groups                  the WhatsApp groups of the instance (to find the --group JID of a new league)
    ops/liga.py doctor [slug]           consistency checks
    ops/liga.py remove <slug> --yes     drop the routes and the profile directory

Details and the isolation model: docs/MULTILIGA.md.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import yaml  # noqa: E402

import league_ctx  # noqa: E402
from league_profile import LeagueProfile  # noqa: E402

DEFAULT_DATA = os.path.join(ROOT, "data")
TEMPLATE = os.path.join(ROOT, "ops", "templates", "SOUL.md.tmpl")
CONTAINER = os.environ.get("LIGA_CONTAINER", "mr-irrelevant")
HERMES = "/opt/hermes/.venv/bin/hermes"
HERMES_PY = "/opt/hermes/.venv/bin/python"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Hermes secret scoping: a profile only sees ITS .env, so the model credentials must be there too.
SHARED_ENV_KEYS = ("AGY_SHIM_TOKEN", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
# directories every Hermes profile has (hermes_cli/profiles.py::_PROFILE_DIRS)
PROFILE_DIRS = ("memories", "sessions", "skills", "skins", "logs", "plans", "workspace", "cron", "home")
# jobs that exist once per installation (model health, the admin's election watch): never per league
GLOBAL_JOBS = {"provider-watch", "eleicao-watch", "session-reset"}
BEGIN = "# >>> liga.py managed: profile routes (generated from every league.yaml: run `ops/liga.py sync`) >>>"
END = "# <<< liga.py managed <<<"
RULES_OPEN, RULES_CLOSE = "<!-- liga:rules -->", "<!-- /liga:rules -->"
PEOPLE_OPEN, PEOPLE_CLOSE = "<!-- liga:people -->", "<!-- /liga:people -->"

Get = Callable[[str], Any]


class LigaError(Exception):
    """A problem the operator can fix (bad id, group already used...)."""


# --------------------------------------------------------------------------- Sleeper
def sleeper_get(path: str) -> Any:
    base = os.environ.get("SLEEPER_API_BASE", "https://api.sleeper.app/v1").rstrip("/")
    req = urllib.request.Request(f"{base}/{path.lstrip('/')}", headers={"User-Agent": "liga-cli/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as exc:
        raise LigaError(f"Sleeper request failed for {path}: {exc}") from exc


# --------------------------------------------------------------------------- paths / files
def profile_dir(data: str, slug: str) -> str:
    return os.path.join(data, "profiles", slug)


def _write(path: str, text: str, mode: Optional[int] = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _link(target_abs: str, link_abs: str) -> None:
    """Relative symlink (the same path layout holds inside the container)."""
    if os.path.lexists(link_abs):
        os.remove(link_abs)
    os.makedirs(os.path.dirname(link_abs), exist_ok=True)
    os.symlink(os.path.relpath(target_abs, os.path.dirname(link_abs)), link_abs)


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def default_groups(data: str) -> set:
    """Groups the default league (#1) answers in: they live in its .env, not in league.yaml."""
    env = league_ctx.read_env_file(os.path.join(data, ".env"))
    raw = ",".join(filter(None, (env.get("WHATSAPP_GROUP_JID"), env.get("EVOLUTION_ALLOWED_GROUPS"))))
    return {g.strip() for g in raw.split(",") if g.strip()}


def default_dm_contacts(data: str) -> List[str]:
    """Numbers league #1 answers in DM: like its groups, they live in data/.env (not in league.yaml)."""
    env = league_ctx.read_env_file(os.path.join(data, ".env"))
    raw = env.get("LEAGUE_DM_ALLOWED") or env.get("EVOLUTION_DM_ALLOWED") or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


# --------------------------------------------------------------------------- persona
def default_mention_regex(persona: str, aka: str = "") -> str:
    """What summons the bot: the full name first ("Mr. Irrelevant", so the adapter strips the whole summon and a
    command like "anota aí" stays at the start of the text), then each distinctive word (4+ letters)."""
    names = [n for n in (persona, aka) if n]
    alts: List[str] = []
    for name in names:
        words = re.findall(r"[A-Za-zÀ-ÿ]+", name)
        if len(words) > 1:
            alts.append(r"[\s.]*".join(re.escape(w.lower()) for w in words))
    for name in names:
        alts += [re.escape(w.lower()) for w in re.findall(r"[A-Za-zÀ-ÿ]+", name) if len(w) >= 4]
    return "|".join(dict.fromkeys(alts)) or re.escape(persona.lower())


def rules_block(profile: LeagueProfile) -> str:
    return "\n".join(f"- {line}" for line in profile.describe_pt())


def people_block(directory: List[Dict[str, Any]], admin_name: str, cfg: Dict[str, Any]) -> str:
    import people as _people
    curated = {int(k): v for k, v in (cfg.get("people") or {}).items() if str(k).isdigit()}
    lines = [f"- O admin do bot (quem o configurou): **{admin_name}**." if admin_name else
             "- O admin do bot é quem aparece como `✅ADMIN` abaixo."]
    lines.append("- Managers da liga (use time ou manager, nunca \"Roster X\"):")
    for d in directory:
        rid = int(d["roster_id"])
        c = curated.get(rid) or {}
        team = c.get("team") or d.get("team_name")
        mgr = c.get("name") or d.get("display_name")
        nicks = c.get("nicks") or _people.auto_nicks(str(d.get("display_name")), str(d.get("team_name")))
        lines.append(f"  • *{team}* — {mgr}" + (f" (apelidos: {', '.join(nicks[:5])})" if nicks else ""))
    return "\n".join(lines)


def render_soul(cfg: Dict[str, Any], profile: LeagueProfile, directory: List[Dict[str, Any]]) -> str:
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    persona = (cfg.get("persona") or {}).get("name") or "Mr. Irrelevant"
    admin_name = (cfg.get("persona") or {}).get("admin_name") or ""
    lore = [str(x) for x in (cfg.get("lore") or [])]
    values = {
        "%%PERSONA%%": persona,
        "%%LEAGUE%%": cfg.get("name") or profile.name or cfg.get("slug", ""),
        "%%FORMAT%%": profile.format_label(),
        "%%LEAGUE_RULES%%": f"{RULES_OPEN}\n{rules_block(profile)}\n{RULES_CLOSE}",
        "%%HOUSE_RULES%%": "\n".join(f"- {x}" for x in lore),
        "%%PEOPLE%%": f"{PEOPLE_OPEN}\n{people_block(directory, admin_name, cfg)}\n{PEOPLE_CLOSE}",
        "%%ADMIN%%": admin_name or "o admin",
    }
    for k, v in values.items():
        tpl = tpl.replace(k, v)
    return re.sub(r"\n{3,}", "\n\n", tpl).strip() + "\n"


def refresh_soul_sections(soul: str, profile: LeagueProfile, directory: List[Dict[str, Any]],
                          cfg: Dict[str, Any]) -> str:
    """Rewrite only the two managed sections (rules, people); anything the operator edited around them stays."""
    admin_name = (cfg.get("persona") or {}).get("admin_name") or ""

    def swap(text: str, open_: str, close: str, body: str) -> str:
        pat = re.compile(re.escape(open_) + r".*?" + re.escape(close), re.S)
        if not pat.search(text):
            return text
        return pat.sub(lambda _m: f"{open_}\n{body}\n{close}", text, count=1)
    soul = swap(soul, RULES_OPEN, RULES_CLOSE, rules_block(profile))
    return swap(soul, PEOPLE_OPEN, PEOPLE_CLOSE, people_block(directory, admin_name, cfg))


# --------------------------------------------------------------------------- hermes config
def derive_config(default_cfg: Dict[str, Any], tz: str) -> Dict[str, Any]:
    """A profile's config.yaml: the default profile's model chain/agent/display settings, without the gateway
    wiring (that belongs to the default profile) and with only the league plugin."""
    cfg = copy.deepcopy(default_cfg)
    for key in ("gateway", "platforms"):
        cfg.pop(key, None)
    plugins = dict(cfg.get("plugins") or {})
    plugins["enabled"] = ["sleeper-league"]
    cfg["plugins"] = plugins
    cfg["timezone"] = tz
    return cfg


def make_env(default_env: Dict[str, str], admins: List[str], phones: str, alert: str, dm: List[str]) -> str:
    lines = ["# Private settings of this league (never versioned). Credentials copied from the default profile",
             "# because Hermes scopes secrets per profile; the Evolution credentials are NOT here on purpose:",
             "# the default profile owns the WhatsApp connection and this league's group is routed to it."]
    for k in SHARED_ENV_KEYS:
        if default_env.get(k):
            lines.append(f"{k}={default_env[k]}")
    lines.append("EVOLUTION_ALLOW_ALL_USERS=true")
    lines.append(f"LEAGUE_ADMINS={','.join(admins)}")
    lines.append(f"LEAGUE_PEOPLE_PHONES={phones}")
    lines.append(f"LEAGUE_DM_ALLOWED={','.join(dm)}")
    lines.append(f"ADMIN_ALERT_JID={alert}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- create / refresh
def _validate(slug: str, league_id: str, group: str) -> None:
    if slug in ("default", "profiles") or not SLUG_RE.match(slug):
        raise LigaError(f"slug inválido {slug!r}: use minúsculas, números, '-' ou '_' (máx. 32), nunca 'default'")
    if not str(league_id).isdigit():
        raise LigaError(f"league id inválido {league_id!r}: é o número que aparece na URL da liga no Sleeper")
    if not str(group).endswith("@g.us"):
        raise LigaError(f"group {group!r} não parece um JID de grupo do WhatsApp (termina com @g.us)")


def create_league(data: str, slug: str, league_id: str, group: str, *, name: Optional[str] = None,
                  persona: Optional[str] = None, mention: Optional[str] = None, admins: Optional[List[str]] = None,
                  phones: str = "", alert: Optional[str] = None, timezone: str = "America/Sao_Paulo",
                  get: Optional[Get] = None, bootstrap: bool = True, sync: bool = True, force: bool = False,
                  admin_name: str = "", out: Callable[[str], None] = print) -> "league_ctx.LeagueContext":
    get = get or sleeper_get
    _validate(slug, league_id, group)
    pdir = profile_dir(data, slug)
    if os.path.exists(pdir) and not force:
        raise LigaError(f"{pdir} já existe (use `refresh {slug}` ou --force)")
    if group in default_groups(data):
        raise LigaError(f"o grupo {group} já é o da liga principal (data/.env): cada liga precisa do seu grupo")
    for other in league_ctx.discover(data):
        if other.slug == slug and not force:
            raise LigaError(f"o slug {slug!r} já está em uso")
        if other.slug != slug and group in other.group_jids:
            raise LigaError(f"o grupo {group} já pertence à liga {other.slug!r}")
        if other.slug != slug and other.league_id == str(league_id):
            raise LigaError(f"a liga Sleeper {league_id} já está configurada como {other.slug!r}")

    league = get(f"league/{league_id}")
    if not isinstance(league, dict) or not league.get("roster_positions"):
        raise LigaError(f"liga {league_id} não encontrada na Sleeper (é pública? o id está certo?)")
    profile = LeagueProfile.from_sleeper(league)
    directory = league_ctx.build_directory(get(f"league/{league_id}/users") or [], get(f"league/{league_id}/rosters") or [])
    out(f"✓ Sleeper: {profile.name} ({profile.season}) — {profile.format_label()}")
    for line in profile.describe_pt():
        out(f"    {line}")

    persona = persona or "Mr. Irrelevant"
    admins = [re.sub(r"\D", "", a) for a in (admins or []) if re.sub(r"\D", "", a)]
    cfg: Dict[str, Any] = {
        "slug": slug, "sleeper_league_id": str(league_id), "name": name or profile.name or slug,
        "timezone": timezone, "group_jid": group,
        "persona": {"name": persona, "mention_regex": mention or default_mention_regex(persona),
                    "admin_name": admin_name},
        "rules": {}, "schedule": {}, "disable_slots": [], "people": {}, "lore": [],
    }

    # --- profile directory (same layout as `hermes profile create`)
    for d in PROFILE_DIRS:
        os.makedirs(os.path.join(pdir, d), exist_ok=True)
    header = ("# League config (ops/liga.py new). Roster structure, scoring, playoffs and FAAB are NOT here: the bot reads\n"
              "# them from Sleeper. Only what Sleeper does not know goes below.\n"
              "#   rules:         house rules, e.g. draft_order: max_pf\n"
              "#   schedule:      move a weekly slot, e.g. tue_08h_power: \"09:30\"\n"
              "#   disable_slots: drop a weekly slot, e.g. [wed_08h_waiver_radar]\n"
              "#   people:        names/nicknames per roster id (the rest is read from Sleeper)\n"
              "#   lore:          extra facts for the persona prompt (commissioner, slang...)\n")
    _write(os.path.join(pdir, league_ctx.CONFIG_NAME),
           header + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False))
    default_cfg = _load_yaml(os.path.join(data, "config.yaml"))
    _write(os.path.join(pdir, "config.yaml"),
           "# Generated by ops/liga.py from the default profile's config.yaml; edit freely.\n"
           + yaml.safe_dump(derive_config(default_cfg, timezone), allow_unicode=True, sort_keys=False))
    _write(os.path.join(pdir, "SOUL.md"), render_soul(cfg, profile, directory))
    default_env = league_ctx.read_env_file(os.path.join(data, ".env"))
    alert = alert if alert is not None else (f"{admins[0]}@s.whatsapp.net" if admins else "")
    _write(os.path.join(pdir, ".env"), make_env(default_env, admins, phones, alert, admins), mode=0o600)
    # shared code and skills (the league code is generic: it finds its league through HERMES_HOME)
    _link(os.path.join(data, "plugins"), os.path.join(pdir, "plugins"))
    _link(os.path.join(data, "scripts"), os.path.join(pdir, "scripts"))
    mine = os.path.join(data, "skills", "mr-irrelevant")
    if os.path.isdir(mine):
        _link(mine, os.path.join(pdir, "skills", "mr-irrelevant"))
    shown = os.path.relpath(pdir, ROOT)
    out(f"✓ perfil criado: {pdir if shown.startswith('..') else shown}")

    league_ctx.reset_cache()
    ctx = league_ctx.for_home(pdir)
    ctx.prime(league, get(f"league/{league_id}/users") or [], get(f"league/{league_id}/rosters") or [])
    if bootstrap:
        import kb_bootstrap
        with league_ctx.use(ctx):
            rep = kb_bootstrap.bootstrap(ctx, get=get, progress=lambda m: out(f"  … {m}"))
        out("✓ base de conhecimento: " + ", ".join(f"{k}={v}" for k, v in rep.items() if k != "seasons")
            + f" ({len(rep['seasons'])} temporadas)")
    if sync:
        n = sync_routes(data)
        out(f"✓ rotas do gateway sincronizadas ({n} liga(s) extra) em data/config.yaml")
    return ctx


def refresh_league(data: str, slug: str, get: Optional[Get] = None, out: Callable[[str], None] = print) -> None:
    get = get or sleeper_get
    pdir = profile_dir(data, slug)
    cfg = _load_yaml(os.path.join(pdir, league_ctx.CONFIG_NAME))
    if not cfg:
        raise LigaError(f"liga {slug!r} não encontrada em {pdir}")
    lid = str(cfg["sleeper_league_id"])
    league = get(f"league/{lid}")
    if not isinstance(league, dict) or not league.get("roster_positions"):
        raise LigaError(f"liga {lid} não encontrada na Sleeper agora; nada foi alterado")
    profile = LeagueProfile.from_sleeper(league)
    users, rosters = get(f"league/{lid}/users") or [], get(f"league/{lid}/rosters") or []
    directory = league_ctx.build_directory(users, rosters)
    soul_path = os.path.join(pdir, "SOUL.md")
    with open(soul_path, encoding="utf-8") as f:
        soul = f.read()
    new = refresh_soul_sections(soul, profile, directory, cfg)
    _write(soul_path, new)
    league_ctx.reset_cache()
    league_ctx.for_home(pdir).prime(league, users, rosters)      # the running bot picks it up within its cache TTL
    out(f"✓ {slug}: regras e managers atualizados ({profile.format_label()})" + (" [persona mudou]" if new != soul else ""))


# --------------------------------------------------------------------------- routes
def _route_entries(data: str) -> List[Dict[str, str]]:
    routes: List[Dict[str, str]] = []
    default_slug = None
    root = league_ctx.for_home(data) if os.path.isfile(os.path.join(data, league_ctx.CONFIG_NAME)) else None
    if root is not None:
        default_slug = root.slug
    for ctx in league_ctx.discover(data):
        if root is not None and ctx.home == root.home:
            continue                                   # the default profile is served directly
        for i, g in enumerate(ctx.group_jids):
            routes.append({"name": ctx.slug if i == 0 else f"{ctx.slug}-{i + 1}", "platform": "evolution",
                           "chat_id": g, "profile": ctx.slug})
        seen_ids = {r["chat_id"] for r in routes}
        if root is not None:               # a number the default league lists is served by the default profile
            seen_ids |= {cid for n in default_dm_contacts(data) for cid in dm_chat_ids(n)}
        for n in ctx.dm_allowed:
            for chat_id in dm_chat_ids(n):
                if chat_id in seen_ids:
                    continue
                seen_ids.add(chat_id)
                tail = re.sub(r"\D", "", chat_id)[-4:] or "x"
                base = f"{ctx.slug}-dm-{tail}"
                name, k = base, 1
                while any(r["name"] == name for r in routes):
                    k += 1
                    name = f"{base}-{k}"
                routes.append({"name": name, "platform": "evolution", "chat_id": chat_id, "profile": ctx.slug})
    return routes


def dm_chat_ids(entry: str) -> List[str]:
    """Every way the adapter can name a DM chat with this number: bare digits (when Evolution sends the phone JID as
    `remoteJidAlt`), the full phone JID, and for Brazilian numbers both spellings of the 9th digit. An entry that already
    is a JID/LID (`…@lid`) is routed exactly as written."""
    raw = str(entry).strip()
    if not raw:
        return []
    if "@" in raw and not raw.endswith("@s.whatsapp.net"):
        return [raw]
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return []
    forms = [digits]
    if digits.startswith("55") and len(digits) == 13 and digits[4] == "9":        # 55 DD 9 XXXX XXXX
        forms.append(digits[:4] + digits[5:])
    elif digits.startswith("55") and len(digits) == 12:                           # the same number without the 9
        forms.append(digits[:4] + "9" + digits[4:])
    return forms + [f"{d}@s.whatsapp.net" for d in forms]


def managed_block(routes: List[Dict[str, str]]) -> str:
    if not routes:
        return ""
    lines = [BEGIN, "gateway:", "  multiplex_profiles: true", "  profile_routes:"]
    for r in routes:
        lines.append(f"    - {{name: {r['name']}, platform: {r['platform']}, chat_id: \"{r['chat_id']}\", "
                     f"profile: {r['profile']}}}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def sync_routes(data: str) -> int:
    """Write the managed `gateway:` block of data/config.yaml. Returns the number of extra leagues routed."""
    path = os.path.join(data, "config.yaml")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
    base = pat.sub("", text)
    if re.search(r"^gateway:", base, re.M):
        raise LigaError("data/config.yaml já tem uma chave `gateway:` fora do bloco gerenciado: mova as rotas "
                        "para dentro do bloco (ou remova a chave) e rode `sync` de novo")
    routes = _route_entries(data)
    block = managed_block(routes)
    new = base.rstrip("\n") + "\n" + (("\n" + block) if block else "")
    if new != text:
        _write(path, new)
    return len({r["profile"] for r in routes})


# --------------------------------------------------------------------------- cron
def league_jobs() -> List[Tuple[str, str, str]]:
    """(name, schedule, script) of the per-league jobs: ops/crons.sh stays the single source of the cadence."""
    out = []
    with open(os.path.join(ROOT, "ops", "crons.sh"), encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*"([^|"]+)\|([^|"]+)\|([^|"]+)"', line)
            if m and m.group(1) not in GLOBAL_JOBS:
                out.append((m.group(1), m.group(2), m.group(3)))
    return out


def cron_commands(slug: str, container: str = CONTAINER) -> List[str]:
    base = f"docker exec -u hermes {container} {HERMES} -p {slug} cron"
    cmds = []
    for name, sched, script in league_jobs():
        cmds.append(f'{base} create "{sched}" --name {slug}-{name} --script {script} --no-agent '
                    f"--deliver local --failure-deliver local")
    # a fresh conversation every morning, in THIS profile's sessions
    cmds.append(f'{base} create "0 5 * * *" --name {slug}-session-reset --script session_reset.py --no-agent '
                f"--deliver local --failure-deliver local")
    return cmds


# --------------------------------------------------------------------------- doctor / list / show
def doctor(data: str, only: Optional[str] = None, get: Optional[Get] = None) -> List[str]:
    problems: List[str] = []
    leagues = league_ctx.discover(data)
    seen_g: Dict[str, str] = {}
    seen_l: Dict[str, str] = {}
    for ctx in leagues:
        if only and ctx.slug != only:
            continue
        tag = f"[{ctx.slug}]"
        for g in ctx.group_jids:
            if g in seen_g and seen_g[g] != ctx.slug:
                problems.append(f"{tag} o grupo {g} também está em {seen_g[g]}")
            seen_g[g] = ctx.slug
        if ctx.league_id in seen_l and seen_l[ctx.league_id] != ctx.slug:
            problems.append(f"{tag} a liga Sleeper {ctx.league_id} também está em {seen_l[ctx.league_id]}")
        seen_l[ctx.league_id] = ctx.slug
        if not ctx.group_jids and not (ctx.legacy_layout and default_groups(data)):
            problems.append(f"{tag} sem group_jid: nenhuma mensagem será enviada")
        if not ctx.legacy_layout:
            for need in ("SOUL.md", "config.yaml", ".env"):
                if not os.path.isfile(os.path.join(ctx.home, need)):
                    problems.append(f"{tag} falta {need}")
            for link in ("plugins", "scripts"):
                if not os.path.exists(os.path.join(ctx.home, link)):
                    problems.append(f"{tag} {link}/ ausente ou symlink quebrado")
            envp = os.path.join(ctx.home, ".env")
            if os.path.isfile(envp) and (os.stat(envp).st_mode & 0o077):
                problems.append(f"{tag} .env com permissão aberta (chmod 600)")
            if not os.path.isfile(os.path.join(ctx.kb_dir, "matchups_history.json")):
                problems.append(f"{tag} base de conhecimento vazia: `ops/liga.py bootstrap {ctx.slug}`")
        try:
            prof = ctx.profile
            if prof.estimated:
                problems.append(f"{tag} regras NÃO carregadas da Sleeper (perfil estimado): Sleeper fora do ar ou id errado")
        except Exception as exc:  # pragma: no cover
            problems.append(f"{tag} perfil: {exc}")
    dm_owner: Dict[str, str] = {}
    for ctx in leagues:                    # a DM has no group: one number can only belong to one league
        contacts = default_dm_contacts(data) if ctx.legacy_layout else ctx.dm_allowed
        for n in contacts:
            key = re.sub(r"\D", "", n)[-8:] or n
            if key in dm_owner and dm_owner[key] != ctx.slug:
                problems.append(f"[{ctx.slug}] o contato de DM {n} também está em {dm_owner[key]}: "
                                f"a DM é atendida por {dm_owner[key]}")
            dm_owner.setdefault(key, ctx.slug)
    extra = [c for c in leagues if not c.legacy_layout]
    cfg_text = ""
    try:
        with open(os.path.join(data, "config.yaml"), encoding="utf-8") as f:
            cfg_text = f.read()
    except OSError:
        problems.append("data/config.yaml não encontrado")
    if extra and "multiplex_profiles: true" not in cfg_text:
        problems.append("há ligas extras mas data/config.yaml não liga o multiplex: rode `ops/liga.py sync`")
    for c in extra:
        for g in c.group_jids:
            if f'chat_id: "{g}"' not in cfg_text:
                problems.append(f"[{c.slug}] grupo {g} sem rota em data/config.yaml: rode `ops/liga.py sync`")
    return problems


GROUPS_SNIPPET = (
    "import json,os,urllib.request as u;b=os.environ['EVOLUTION_API_URL'].rstrip('/');i=os.environ['EVOLUTION_INSTANCE'];"
    "r=u.Request(f'{b}/group/fetchAllGroups/{i}?getParticipants=false',headers={'apikey':os.environ['EVOLUTION_API_KEY']});"
    "print(u.urlopen(r,timeout=30).read().decode())")


def cmd_groups(data: str, container: str = CONTAINER, out: Callable[[str], None] = print,
               run: Callable[..., Any] = subprocess.run) -> List[Dict[str, str]]:
    """List the WhatsApp groups the instance is in (asks Evolution from inside the running container, which has the
    credentials and the network route), marking the ones a league already uses."""
    r = run(["docker", "exec", container, HERMES_PY, "-c", GROUPS_SNIPPET], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise LigaError(f"não consegui perguntar ao Evolution via `docker exec {container}`: "
                        f"{(r.stderr or r.stdout).strip()[-300:]}")
    try:
        raw = json.loads(r.stdout)
    except ValueError as exc:
        raise LigaError(f"resposta inesperada do Evolution: {r.stdout.strip()[:200]}") from exc
    used = set(default_groups(data))
    for c in league_ctx.discover(data):
        used |= set(c.group_jids)
    groups = [{"id": str(g.get("id") or ""), "subject": str(g.get("subject") or ""), "size": str(g.get("size") or "")}
              for g in (raw if isinstance(raw, list) else [])]
    for g in sorted(groups, key=lambda x: x["subject"].lower()):
        out(f"{g['id']:<40} {g['subject']}" + ("   [em uso]" if g["id"] in used else ""))
    if not groups:
        out("nenhum grupo encontrado (o número do bot já foi adicionado ao grupo?)")
    return groups


def cmd_list(data: str, out: Callable[[str], None] = print) -> None:
    leagues = league_ctx.discover(data)
    if not leagues:
        out("nenhuma liga configurada (falta data/league.yaml)")
        return
    out(f"{'slug':<16} {'liga Sleeper':<20} {'formato':<42} grupo")
    for c in leagues:
        try:
            fmt = c.profile.format_label()
        except Exception:
            fmt = "?"
        kind = "default" if c.legacy_layout else "perfil"
        out(f"{c.slug:<16} {c.league_id:<20} {fmt:<42} {(c.group_jid or '-')}   [{kind}]")


# --------------------------------------------------------------------------- CLI
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--data", default=os.environ.get("LIGA_DATA", DEFAULT_DATA), help="Hermes home of the default profile")
    ap.add_argument("--container", default=CONTAINER, help="running bot container (docker exec target)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="create a league bot from its Sleeper id")
    n.add_argument("slug")
    n.add_argument("--league-id", required=True)
    n.add_argument("--group", required=True, help="WhatsApp group JID (…@g.us)")
    n.add_argument("--name")
    n.add_argument("--persona", help='bot persona name (default "Mr. Irrelevant")')
    n.add_argument("--mention", help="regex that summons the bot in the group")
    n.add_argument("--admin", default="", help="admin phone numbers, comma separated (digits)")
    n.add_argument("--admin-name", default="")
    n.add_argument("--phones", default="", help='people phones: "ROSTER:num,num;ROSTER:num"')
    n.add_argument("--alert")
    n.add_argument("--timezone", default="America/Sao_Paulo")
    n.add_argument("--no-bootstrap", action="store_true")
    n.add_argument("--no-sync", action="store_true")
    n.add_argument("--force", action="store_true")
    sub.add_parser("list")
    for name in ("show", "bootstrap", "refresh", "cron", "remove"):
        p = sub.add_parser(name)
        p.add_argument("slug")
        if name == "cron":
            p.add_argument("--apply", action="store_true")
        if name == "remove":
            p.add_argument("--yes", action="store_true")
    sub.add_parser("sync")
    sub.add_parser("groups")
    d = sub.add_parser("doctor")
    d.add_argument("slug", nargs="?")
    a = ap.parse_args(argv)
    data = os.path.abspath(a.data)

    try:
        if a.cmd == "new":
            create_league(data, a.slug, a.league_id, a.group, name=a.name, persona=a.persona, mention=a.mention,
                          admins=[x for x in a.admin.split(",") if x], phones=a.phones, alert=a.alert,
                          timezone=a.timezone, bootstrap=not a.no_bootstrap, sync=not a.no_sync, force=a.force,
                          admin_name=a.admin_name)
            print(f"\nPróximos passos:\n  1. ops/liga.py cron {a.slug} --apply      (agenda os jobs no container)\n"
                  f"  2. docker restart {a.container}                (o gateway passa a rotear o grupo para a liga)\n"
                  f"  3. coloque o número do bot no grupo e mencione-o\n"
                  f"  Ajustes finos (apelidos, horários, regras da casa): data/profiles/{a.slug}/league.yaml "
                  f"e `ops/liga.py refresh {a.slug}`")
        elif a.cmd == "list":
            cmd_list(data)
        elif a.cmd == "groups":
            cmd_groups(data, a.container)
        elif a.cmd == "show":
            ctx = league_ctx.for_home(profile_dir(data, a.slug))
            print(json.dumps(ctx.summary(), ensure_ascii=False, indent=1))
            print("\n".join(ctx.profile.describe_pt()))
            for rid, e in sorted(ctx.people.entries.items()):
                print(f"  {rid:>2}  {e['team']} — {e['name']}")
        elif a.cmd == "sync":
            print(f"{sync_routes(data)} liga(s) extra roteadas")
        elif a.cmd == "bootstrap":
            import kb_bootstrap
            ctx = league_ctx.for_home(profile_dir(data, a.slug))
            with league_ctx.use(ctx):
                print(json.dumps(kb_bootstrap.bootstrap(ctx, progress=print), ensure_ascii=False, indent=1))
        elif a.cmd == "refresh":
            refresh_league(data, a.slug)
        elif a.cmd == "cron":
            cmds = cron_commands(a.slug, a.container)
            for c in cmds:
                print(c)
                if a.apply:
                    subprocess.run(c, shell=True, check=True)
        elif a.cmd == "remove":
            if not a.yes:
                raise LigaError("isto apaga o perfil e a base da liga: confirme com --yes")
            pdir = profile_dir(data, a.slug)
            if not os.path.isdir(pdir):
                raise LigaError(f"{pdir} não existe")
            shutil.rmtree(pdir)
            league_ctx.reset_cache()
            sync_routes(data)
            print(f"removida a liga {a.slug} (rotas sincronizadas; reinicie o container)")
        elif a.cmd == "doctor":
            problems = doctor(data, a.slug)
            for p in problems:
                print("✗", p)
            print("ok" if not problems else f"{len(problems)} problema(s)")
            return 1 if problems else 0
    except LigaError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
