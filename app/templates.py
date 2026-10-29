"""Agent templates: reusable agent profiles (role, rules, areas of expertise, skills, instructions).

A template is referred to by its symbol, "@" followed by its slug (e.g. @code-reviewer). A project
agent can be based on a template (its `template` column holds the slug) and can have internal agents
(its `subagents` column holds a JSON list of slugs). A template can list internal agents of its own,
which an agent based on it inherits. On the Claude Code engine, internal agents become Claude Code
sub-agents the agent can delegate to; the API engine has no sub-agents and ignores them.

A library of starter templates (app/template_presets.py) is installed on first run. It also feeds the
template editor's suggestions, which depend on the fields filled in so far (see suggest).
"""

import json
import math
import re
from . import db, template_presets as presets

SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$")
LIST_FIELDS = ("rules", "expertise", "skills", "subagents")

# General values offered by the template editor's autocomplete after the context-dependent ones.
CATALOG = {
    "rules": [
        "Write tests for every change",
        "Run the test suite before reporting a task as done",
        "Keep changes small and focused on the task",
        "Follow the existing code style and conventions of the repository",
        "Never commit secrets, keys or credentials",
        "Ask before deleting files or data",
        "Do not add new dependencies without a clear reason",
        "Document public functions and non-obvious decisions",
        "Prefer readability over cleverness",
        "Handle errors explicitly; never swallow exceptions silently",
        "Validate all user input at the boundaries",
        "Keep backwards compatibility unless told otherwise",
        "Update the README when behavior or setup changes",
        "Use conventional commit messages",
        "Never force-push or rewrite shared git history",
        "Explain the reasoning behind every recommendation",
        "Report risks and trade-offs before making large changes",
        "Prefer the standard library over third-party packages",
        "Keep functions short and single-purpose",
        "Use type hints in Python code",
        "Use TypeScript strict mode",
        "Make UI changes accessible (labels, contrast, keyboard navigation)",
        "Respond in the language of the user",
        "Cite sources for factual claims",
        "Do not modify generated or vendored files",
        "Measure before optimizing",
        "Leave the codebase cleaner than you found it",
    ],
    "expertise": [
        "Backend development", "Frontend development", "Full-stack web development", "Mobile development",
        "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin", "Swift", "C#", "C++", "PHP", "Ruby",
        "SQL and relational databases", "PostgreSQL", "SQLite", "MongoDB", "Redis",
        "REST API design", "GraphQL", "Microservices architecture", "Distributed systems",
        "React", "Vue", "Svelte", "Angular", "Next.js", "Node.js", "FastAPI", "Django", "Flask", "Spring Boot",
        "HTML and CSS", "UI/UX design", "Accessibility", "Design systems",
        "DevOps", "Docker", "Kubernetes", "CI/CD pipelines", "Linux system administration", "Cloud infrastructure (AWS)",
        "Cloud infrastructure (GCP)", "Cloud infrastructure (Azure)", "Terraform", "Observability and monitoring",
        "Application security", "Authentication and authorization", "Performance optimization",
        "Software testing and QA", "Test automation", "Code review", "Software architecture", "Refactoring legacy code",
        "Data engineering", "Data analysis", "Machine learning", "LLM applications and prompt engineering",
        "Technical writing", "Product management", "Project planning", "Requirements analysis",
        "Marketing", "SEO", "Copywriting", "Customer support", "Finance and accounting", "Legal research",
    ],
    "skills": [
        "Writing unit tests", "Writing integration tests", "End-to-end testing", "Debugging", "Profiling",
        "Code review", "Refactoring", "Writing documentation", "Writing API documentation", "Database schema design",
        "Writing SQL migrations", "Query optimization", "API design", "Git workflows", "Writing shell scripts",
        "Writing Dockerfiles", "Configuring CI pipelines", "Dependency management", "Security auditing",
        "Threat modelling", "Responsive layout", "CSS styling", "State management", "Data visualization",
        "Data cleaning", "Writing regular expressions", "Estimating effort", "Breaking work into tasks",
        "Writing release notes", "Summarizing long documents", "Research and fact-checking", "Translating text",
        "Editing and proofreading", "Writing user stories", "Root-cause analysis", "Incident response",
        "Performance benchmarking", "Accessibility auditing", "Internationalization", "Web scraping",
    ],
}


class TemplateError(ValueError):
    """Invalid template input; the message is shown to the user."""


def symbol(slug: str) -> str:
    return f"@{slug}"


def parse_symbol(value: str) -> str:
    """'@Code-Reviewer ' -> 'code-reviewer'. Empty input gives ''."""
    return (value or "").strip().lstrip("@").strip().lower()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40].strip("-")


def clean_list(values) -> list[str]:
    """Trimmed, de-duplicated (case-insensitively), non-empty strings in their original order."""
    seen, out = set(), []
    for v in values or []:
        v = " ".join(str(v).split())
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


# ---------------------------------------------------------------- storage

def _row(row: dict | None) -> dict | None:
    if row:
        for key in LIST_FIELDS:
            row[key] = json.loads(row[key])
        row["symbol"] = symbol(row["slug"])
    return row


def get(tid: int) -> dict | None:
    return _row(db.one("SELECT * FROM agent_templates WHERE id = ?", (tid,)))


def by_slug(slug: str) -> dict | None:
    return _row(db.one("SELECT * FROM agent_templates WHERE slug = ?", (parse_symbol(slug),)))


def list_all() -> list[dict]:
    rows = [_row(r) for r in db.query("SELECT * FROM agent_templates ORDER BY category COLLATE NOCASE, name COLLATE NOCASE")]
    usage = usages()
    for r in rows:
        r["used_by"] = usage.get(r["slug"], [])
    return rows


def usages() -> dict[str, list[dict]]:
    """slug -> where it is used: project agents ("template" or "internal") and other templates."""
    out: dict[str, list[dict]] = {}
    for a in db.query("SELECT a.template, a.subagents, a.name, p.id AS project_id, p.name AS project "
                      "FROM agents a JOIN projects p ON p.id = a.project_id"):
        where = {"project_id": a["project_id"], "project": a["project"], "agent": a["name"]}
        if a["template"]:
            out.setdefault(a["template"], []).append({**where, "as": "template"})
        for s in json.loads(a["subagents"]):
            out.setdefault(s, []).append({**where, "as": "internal"})
    for t in db.query("SELECT slug, name, subagents FROM agent_templates"):
        for s in json.loads(t["subagents"]):
            out.setdefault(s, []).append({"template": t["name"], "symbol": symbol(t["slug"]), "as": "internal"})
    return out


def validate_refs(slugs: list[str], own_slug: str | None = None) -> list[str]:
    """Normalize a list of template symbols and check that each one exists."""
    out = []
    for raw in slugs or []:
        slug = parse_symbol(raw)
        if not slug or slug in out:
            continue
        if own_slug and slug == own_slug:
            raise TemplateError("A template cannot be its own internal agent")
        if not db.one("SELECT 1 FROM agent_templates WHERE slug = ?", (slug,)):
            raise TemplateError(f"Unknown agent template {symbol(slug)}")
        out.append(slug)
    return out


def check_slug(slug: str, exclude_id: int | None = None) -> str:
    slug = parse_symbol(slug)
    if not SLUG_RX.match(slug):
        raise TemplateError("The symbol may contain lowercase letters, digits and hyphens (1-40 characters), "
                            "e.g. @code-reviewer")
    clash = db.one("SELECT id FROM agent_templates WHERE slug = ?", (slug,))
    if clash and clash["id"] != exclude_id:
        raise TemplateError(f"{symbol(slug)} is already used by another template")
    return slug


def save(fields: dict, tid: int | None = None) -> dict:
    """Create (tid None) or update a template. Renaming a symbol updates every reference to it."""
    old = get(tid) if tid else None
    slug = check_slug(fields.get("slug") or slugify(fields["name"]), tid)
    values = {
        "slug": slug, "name": fields["name"].strip(), "category": fields.get("category", "").strip(),
        "description": fields.get("description", "").strip(),
        "role": fields.get("role", "").strip(), "instructions": fields.get("instructions", ""),
        "model": fields.get("model") or "", "effort": fields.get("effort") or "",
        "rules": json.dumps(clean_list(fields.get("rules")), ensure_ascii=False),
        "expertise": json.dumps(clean_list(fields.get("expertise")), ensure_ascii=False),
        "skills": json.dumps(clean_list(fields.get("skills")), ensure_ascii=False),
        "subagents": json.dumps(validate_refs(fields.get("subagents"), slug)),
    }
    ts = db.now()
    with db.transaction():
        if old:
            db._conn.execute(f"UPDATE agent_templates SET {', '.join(f'{k} = ?' for k in values)}, updated_at = ? "
                             "WHERE id = ?", (*values.values(), ts, tid))
            if old["slug"] != slug:
                _replace_refs(old["slug"], slug)
        else:
            tid = db._conn.execute(
                f"INSERT INTO agent_templates({', '.join(values)}, created_at, updated_at) "
                f"VALUES({', '.join('?' * len(values))}, ?, ?)", (*values.values(), ts, ts)).lastrowid
    return get(tid)


def delete(tid: int) -> None:
    """Delete a template and drop every reference to it."""
    t = get(tid)
    with db.transaction():
        _replace_refs(t["slug"], None)
        db._conn.execute("DELETE FROM agent_templates WHERE id = ?", (tid,))


def _replace_refs(old: str, new: str | None) -> None:
    """Point references to `old` at `new`, or remove them when `new` is None. Runs inside a transaction."""
    conn = db._conn
    conn.execute("UPDATE agents SET template = ? WHERE template = ?", (new or "", old))
    for table in ("agents", "agent_templates"):
        for r in conn.execute(f"SELECT id, subagents FROM {table}").fetchall():
            refs = json.loads(r["subagents"])
            if old in refs:
                refs = clean_list(new if s == old else s for s in refs if new or s != old)
                conn.execute(f"UPDATE {table} SET subagents = ? WHERE id = ?", (json.dumps(refs), r["id"]))


# ---------------------------------------------------------------- starter templates

def preset_status() -> list[dict]:
    installed = {r["slug"] for r in db.query("SELECT slug FROM agent_templates")}
    return [{"slug": p["slug"], "name": p["name"], "category": p["category"], "installed": p["slug"] in installed}
            for p in presets.PRESETS]


def install_presets(slugs: list[str] | None = None) -> int:
    """Install the built-in templates that are missing (all, or those in `slugs`). Returns how many."""
    existing = {r["slug"] for r in db.query("SELECT slug FROM agent_templates")}
    chosen = [p for p in presets.PRESETS if p["slug"] not in existing and (slugs is None or p["slug"] in slugs)]
    known = existing | {p["slug"] for p in chosen}
    ts = db.now()
    with db.transaction():
        for p in chosen:
            db._conn.execute(
                "INSERT INTO agent_templates(slug, name, category, description, role, instructions, rules, expertise, "
                "skills, subagents, model, effort, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (p["slug"], p["name"], p["category"], p["descriptions"][0], p["roles"][0], p["instructions"][0],
                 json.dumps(p["rules"]), json.dumps(p["expertise"]), json.dumps(p["skills"]),
                 json.dumps([s for s in p["subagents"] if s in known]), p.get("model", ""), p.get("effort", ""), ts, ts))
    return len(chosen)


def seed_once() -> None:
    """Install the starter templates the first time the server runs with this database."""
    if not db.get_settings()["templates_seeded"]:
        install_presets()
        db.save_settings({"templates_seeded": True})


# ---------------------------------------------------------------- context-dependent suggestions

# The template editor's fields, in order. Suggestions for a field depend only on the fields before it,
# so choosing a category narrows the names, a name narrows descriptions and roles, and so on.
FIELD_ORDER = ("category", "name", "description", "role", "expertise", "skills", "rules", "subagents", "instructions")
# How strongly each value a draft shares with a profile ties the two together.
LIST_WEIGHTS = {"expertise": 2.5, "skills": 2.0, "rules": 1.5, "subagents": 1.5}
LIMITS = {"name": 40, "description": 10, "role": 10, "expertise": 120, "skills": 120, "rules": 120, "instructions": 6}
STOPWORDS = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "who", "that", "is", "are", "by", "as",
             "at", "or", "from", "it", "its", "their", "be", "this", "our", "your", "we", "my", "i"}


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _words(text: str) -> list[str]:
    """Significant words in order, plural "s" stripped, without repeats."""
    words = (w[:-1] if len(w) > 3 and w.endswith("s") else w
             for w in re.findall(r"[a-z0-9+#]+", text.lower()) if w not in STOPWORDS)
    return list(dict.fromkeys(words))


def _tokens(text: str) -> set[str]:
    return set(_words(text))


def _overlap(query: str, text: str, idf: dict[str, float]) -> float:
    """Share of the query's words that occur in `text`. Each word is weighted by how rare it is across the
    profiles (so "translator" counts for more than "app") and by its position: names and roles lead with
    their head noun ("Translator for the mobile app")."""
    q = _words(query)
    if not q:
        return 0.0
    rarest = max(idf.values(), default=1.0)
    weight = {w: idf.get(w, rarest) / (1 + i) for i, w in enumerate(q)}
    found = _tokens(text)
    return sum(v for w, v in weight.items() if w in found) / sum(weight.values())


def _idf(profiles: list[dict]) -> dict[str, float]:
    docs = [_tokens(" ".join(p["name"] + p["role"] + p["description"])) for p in profiles]
    df: dict[str, int] = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    return {w: math.log(1 + len(docs) / n) for w, n in df.items()}


def _profiles() -> list[dict]:
    """What suggestions are drawn from: the built-in presets (merged with the user's version of them, if
    installed and edited) and the user's own templates."""
    rows = {t["slug"]: t for t in (_row(r) for r in db.query("SELECT * FROM agent_templates"))}
    out = []
    for p in presets.PRESETS:
        prof = {"slug": p["slug"], "category": p["category"], "name": [p["name"], *p["aliases"]],
                "description": list(p["descriptions"]), "role": list(p["roles"]), "instructions": list(p["instructions"]),
                "model": p.get("model", ""), "effort": p.get("effort", ""),
                **{k: list(p[k]) for k in LIST_FIELDS}}
        t = rows.pop(p["slug"], None)
        if t:   # the user's current values first, then the preset's alternatives
            prof["category"] = t["category"] or prof["category"]
            for key in ("name", "description", "role", "instructions"):
                prof[key] = clean_list([t[key], *prof[key]])
            for key in LIST_FIELDS:
                prof[key] = clean_list(t[key] + prof[key])
            prof["model"], prof["effort"] = t["model"], t["effort"]
        out.append(prof)
    for t in rows.values():
        out.append({"slug": t["slug"], "category": t["category"], "model": t["model"], "effort": t["effort"],
                    **{k: clean_list([t[k]]) for k in ("name", "description", "role", "instructions")},
                    **{k: t[k] for k in LIST_FIELDS}})
    return out


def _weight(p: dict, draft: dict, upto: int, idf: dict[str, float]) -> float:
    """How well profile `p` matches the draft, judged by the first `upto` fields of FIELD_ORDER."""
    fields = FIELD_ORDER[:upto]
    w = 0.0
    if "category" in fields and draft["category"].strip():
        w += 4 if _norm(draft["category"]) == _norm(p["category"]) else 0
    if "name" in fields and draft["name"].strip():
        exact = _norm(draft["name"]) in map(_norm, p["name"])
        w += 12 if exact else 6 * _overlap(draft["name"], " ".join(p["name"]), idf)
    if "description" in fields and draft["description"].strip():
        w += 4 * _overlap(draft["description"], " ".join(p["description"] + p["role"] + p["name"]), idf)
    if "role" in fields and draft["role"].strip():
        exact = _norm(draft["role"]) in map(_norm, p["role"])
        w += 8 if exact else 5 * _overlap(draft["role"], " ".join(p["role"] + p["name"]), idf)
    for key, weight in LIST_WEIGHTS.items():
        if key in fields and draft[key]:
            mine = {_norm(v) for v in p[key]}
            w += weight * sum(_norm(v) in mine for v in draft[key])
    return w


def _ranked(profiles: list[dict], weights: list[float], key: str) -> list[dict]:
    """Values of `key` across the profiles, best first. A value scores the weight of each profile that has
    it (a little less further down the profile's list); a small base keeps unrelated values in order of
    how common they are."""
    score, best, shown = {}, {}, {}
    for p, w in zip(profiles, weights):
        for i, value in enumerate(p[key]):
            k = _norm(value)
            s = (w + 0.1) * 0.97 ** i
            shown.setdefault(k, value)
            score[k] = score.get(k, 0) + s
            if s > best.get(k, (-1, None))[0]:
                best[k] = (s, p)
    items = []
    for k in sorted(score, key=lambda k: -score[k]):
        p = best[k][1]
        item = {"value": shown[k], "hint": p["category"] if key == "name" else p["name"][0]}
        if key == "name":
            item["category"] = p["category"]
        items.append(item)
    for value in CATALOG.get(key, []):
        if _norm(value) not in score:
            items.append({"value": value, "hint": ""})
    return items[:LIMITS[key]]


def suggest(draft: dict) -> dict:
    """Candidate values for every field of the template editor, given what the draft contains so far."""
    draft = {**{k: "" for k in FIELD_ORDER}, **{k: [] for k in LIST_FIELDS}, **draft}
    own = parse_symbol(draft.get("slug", ""))
    profiles = _profiles()
    idf = _idf(profiles)
    out = {"category": [{"value": c["name"], "hint": c["about"], "icon": c["icon"]} for c in presets.CATEGORIES]}
    custom = {_norm(c["name"]) for c in presets.CATEGORIES}
    for p in profiles:
        if p["category"] and _norm(p["category"]) not in custom:
            custom.add(_norm(p["category"]))
            out["category"].append({"value": p["category"], "hint": "your category", "icon": "🏷"})
    for index, key in enumerate(FIELD_ORDER):
        if key in ("category", "subagents"):
            continue
        weights = [_weight(p, draft, index, idf) for p in profiles]
        out[key] = _ranked(profiles, weights, key)

    # Internal agents can only be existing templates.
    weights = [_weight(p, draft, FIELD_ORDER.index("subagents"), idf) for p in profiles]
    templates = {t["slug"]: t for t in (_row(r) for r in db.query("SELECT * FROM agent_templates")) if t["slug"] != own}
    score = {slug: 0.0 for slug in templates}
    for p, w in zip(profiles, weights):
        for slug in p["subagents"]:
            if slug in score:
                score[slug] += w + 0.1
    out["subagents"] = [{"value": symbol(s), "label": templates[s]["name"],
                         "hint": templates[s]["description"] or templates[s]["role"]}
                        for s in sorted(score, key=lambda s: (-score[s], templates[s]["name"].lower()))]

    # The best-matching profile as a whole, for "fill empty fields".
    weights = [_weight(p, draft, len(FIELD_ORDER), idf) for p in profiles]
    top = max(range(len(profiles)), key=lambda i: weights[i], default=None)
    out["best"] = None
    if top is not None and weights[top] >= 4:
        p = profiles[top]
        out["best"] = {
            "name": p["name"][0], "category": p["category"], "model": p["model"], "effort": p["effort"],
            **{k: (p[k][0] if p[k] else "") for k in ("description", "role", "instructions")},
            **{k: p[k] for k in ("expertise", "skills", "rules")},
            "subagents": [symbol(s) for s in p["subagents"] if s in templates],
        }
    return out


# ---------------------------------------------------------------- prompts

def profile_text(t: dict) -> str:
    """The parts of a template that shape an agent's behaviour, as system prompt text."""
    parts = []
    if t["expertise"]:
        parts.append("Areas of expertise: " + "; ".join(t["expertise"]) + ".")
    if t["skills"]:
        parts.append("Skills: " + "; ".join(t["skills"]) + ".")
    if t["rules"]:
        parts.append("Rules you must follow:\n" + "\n".join(f"- {r}" for r in t["rules"]))
    if t["instructions"].strip():
        parts.append(t["instructions"].strip())
    return "\n\n".join(parts)


def subagent_prompt(t: dict) -> str:
    role = f", {t['role']}" if t["role"] else ""
    intro = (f"You are {t['name']}{role}, an internal agent ({t['symbol']}) that another agent delegates "
             "work to. Do the delegated work and report the result clearly and concisely.")
    return "\n\n".join(p for p in (intro, profile_text(t)) if p)


def resolve_agent(agent: dict) -> tuple[dict | None, list[dict]]:
    """A project agent's template and its internal agents (its own plus the template's), as template rows.
    References to deleted templates are skipped."""
    template = by_slug(agent["template"]) if agent.get("template") else None
    slugs = clean_list(json.loads(agent["subagents"]) if isinstance(agent.get("subagents"), str)
                       else agent.get("subagents") or [])
    if template:
        slugs = clean_list(slugs + template["subagents"])
    internal = [t for s in slugs if s != (template or {}).get("slug") and (t := by_slug(s))]
    return template, internal
