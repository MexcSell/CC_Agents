"""Agent templates: reusable agent profiles (role, rules, areas of expertise, skills, instructions).

A template is referred to by its symbol, "@" followed by its slug (e.g. @code-reviewer). A project
agent can be based on a template (its `template` column holds the slug) and can have internal agents
(its `subagents` column holds a JSON list of slugs). A template can list internal agents of its own,
which an agent based on it inherits. On the Claude Code engine, internal agents become Claude Code
sub-agents the agent can delegate to; the API engine has no sub-agents and ignores them.
"""

import json
import re
from collections import Counter

from . import db

SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$")
LIST_FIELDS = ("rules", "expertise", "skills", "subagents")

# Suggestions offered by the autocomplete of the template editor, after the values already used in
# existing templates.
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
        "Update the README when behaviour or setup changes",
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
        "Measure before optimising",
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
        "Application security", "Authentication and authorization", "Performance optimisation",
        "Software testing and QA", "Test automation", "Code review", "Software architecture", "Refactoring legacy code",
        "Data engineering", "Data analysis", "Machine learning", "LLM applications and prompt engineering",
        "Technical writing", "Product management", "Project planning", "Requirements analysis",
        "Marketing", "SEO", "Copywriting", "Customer support", "Finance and accounting", "Legal research",
    ],
    "skills": [
        "Writing unit tests", "Writing integration tests", "End-to-end testing", "Debugging", "Profiling",
        "Code review", "Refactoring", "Writing documentation", "Writing API documentation", "Database schema design",
        "Writing SQL migrations", "Query optimisation", "API design", "Git workflows", "Writing shell scripts",
        "Writing Dockerfiles", "Configuring CI pipelines", "Dependency management", "Security auditing",
        "Threat modelling", "Responsive layout", "CSS styling", "State management", "Data visualisation",
        "Data cleaning", "Writing regular expressions", "Estimating effort", "Breaking work into tasks",
        "Writing release notes", "Summarising long documents", "Research and fact-checking", "Translating text",
        "Editing and proofreading", "Writing user stories", "Root-cause analysis", "Incident response",
        "Performance benchmarking", "Accessibility auditing", "Internationalisation", "Web scraping",
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
    rows = [_row(r) for r in db.query("SELECT * FROM agent_templates ORDER BY name COLLATE NOCASE")]
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
    """Normalise a list of template symbols and check that each one exists."""
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
        "slug": slug, "name": fields["name"].strip(), "description": fields.get("description", "").strip(),
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


def vocabulary() -> dict[str, list[str]]:
    """Autocomplete values per field: values used in templates (most used first), then the catalog."""
    out = {}
    rows = db.query("SELECT rules, expertise, skills FROM agent_templates")
    for key in ("rules", "expertise", "skills"):
        used = Counter(v for r in rows for v in json.loads(r[key]))
        ranked = [v for v, _ in used.most_common()]
        out[key] = clean_list(ranked + CATALOG[key])
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
