"""Agent groups: hierarchical teams of agent templates, referred to by "#" + slug (e.g. #python-team).

A group's members are template-based agents with a name and the member they report to. Exactly one
member, the lead, reports to nobody; it receives the project's tasks and delegates down the hierarchy.
Applying a group to a project creates the whole team as the project's agents.
"""

import json
import re

from . import db, template_presets as presets

SLUG_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$|^[a-z0-9]$")
MAX_MEMBERS = 40


class GroupError(ValueError):
    """Invalid group input; the message is shown to the user."""


def symbol(slug: str) -> str:
    return f"#{slug}"


def parse_symbol(value: str) -> str:
    return (value or "").strip().lstrip("#").strip().lower()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")


# ---------------------------------------------------------------- members

def ordered(members: list[dict]) -> list[dict]:
    """Members depth-first from the lead, each with `depth`."""
    children: dict[str | None, list[dict]] = {}
    for m in members:
        children.setdefault(m["parent"], []).append(m)
    out = []

    def walk(m, depth):
        out.append({**m, "depth": depth})
        for c in children.get(m["key"], []):
            walk(c, depth + 1)

    for root in children.get(None, []):
        walk(root, 0)
    return out


def normalize(members: list[dict]) -> list[dict]:
    """Validate group members ({template, name, key?, parent?}) and return them clean and ordered."""
    if not members:
        raise GroupError("A group needs at least one member")
    if len(members) > MAX_MEMBERS:
        raise GroupError(f"A group can have at most {MAX_MEMBERS} members")
    templates = {r["slug"]: r for r in db.query("SELECT slug, name FROM agent_templates")}
    out, keys = [], set()
    for m in members:
        slug = (m.get("template") or "").strip().lstrip("@").lower()
        if slug not in templates:
            raise GroupError(f"Unknown agent template @{slug}" if slug else "Every member needs a template")
        name = " ".join((m.get("name") or templates[slug]["name"]).split())[:100]
        key = base = slugify(m.get("key") or name) or slug
        n = 2
        while key in keys:
            key, n = f"{base}-{n}", n + 1
        keys.add(key)
        out.append({"key": key, "template": slug, "name": name, "parent": m.get("parent") or None,
                    "_given": m.get("key") or key})
    # Parents may refer to the keys the client sent; map them to the final keys.
    given = {m["_given"]: m["key"] for m in out}
    for m in out:
        if m["parent"] is not None:
            if m["parent"] not in given:
                raise GroupError(f"{m['name']} reports to an unknown member")
            m["parent"] = given[m["parent"]]
        del m["_given"]
    roots = [m for m in out if m["parent"] is None]
    if len(roots) != 1:
        raise GroupError("Exactly one member must lead the group (report to nobody)")
    by_key = {m["key"]: m for m in out}
    for m in out:
        seen, cur = set(), m
        while cur["parent"] is not None:
            if cur["key"] in seen:
                raise GroupError(f"The reporting lines around {m['name']} form a loop")
            seen.add(cur["key"])
            cur = by_key[cur["parent"]]
    return [{k: m[k] for k in ("key", "template", "name", "parent")} for m in ordered(out)]


def without_template(members: list[dict], slug: str) -> list[dict]:
    """Remove the members based on template `slug`; their reports move up to their manager (if the lead
    goes, its first report becomes the lead)."""
    members = [dict(m) for m in members]
    for gone in [m for m in members if m["template"] == slug]:
        members = [m for m in members if m is not gone]
        reports = [m for m in members if m["parent"] == gone["key"]]
        if gone["parent"] is None and reports:
            reports[0]["parent"] = None
            for m in reports[1:]:
                m["parent"] = reports[0]["key"]
        else:
            for m in reports:
                m["parent"] = gone["parent"]
    return [{k: m[k] for k in ("key", "template", "name", "parent")} for m in ordered(members)]


# ---------------------------------------------------------------- storage

def _row(row: dict | None) -> dict | None:
    if row:
        row["members"] = json.loads(row["members"])
        row["symbol"] = symbol(row["slug"])
    return row


def _with_templates(group: dict) -> dict:
    """Add each member's template details and depth, for display."""
    templates = {r["slug"]: r for r in db.query("SELECT slug, name, role, area, language, model, effort "
                                                "FROM agent_templates")}
    group["members"] = [{**m, "template_name": templates.get(m["template"], {}).get("name", ""),
                         "role": templates.get(m["template"], {}).get("role", ""),
                         "area": templates.get(m["template"], {}).get("area", ""),
                         "language": templates.get(m["template"], {}).get("language", "")}
                        for m in ordered(group["members"])]
    return group


def get(gid: int) -> dict | None:
    row = _row(db.one("SELECT * FROM agent_groups WHERE id = ?", (gid,)))
    return _with_templates(row) if row else None


def by_slug(slug: str) -> dict | None:
    row = _row(db.one("SELECT * FROM agent_groups WHERE slug = ?", (parse_symbol(slug),)))
    return _with_templates(row) if row else None


def list_all() -> list[dict]:
    return [_with_templates(_row(r)) for r in
            db.query("SELECT * FROM agent_groups ORDER BY category COLLATE NOCASE, name COLLATE NOCASE")]


def save(fields: dict, gid: int | None = None) -> dict:
    slug = parse_symbol(fields.get("slug") or slugify(fields["name"]))
    if not SLUG_RX.match(slug):
        raise GroupError("The symbol may contain lowercase letters, digits and hyphens (1-40 characters), "
                         "e.g. #python-team")
    clash = db.one("SELECT id FROM agent_groups WHERE slug = ?", (slug,))
    if clash and clash["id"] != gid:
        raise GroupError(f"{symbol(slug)} is already used by another group")
    values = {"slug": slug, "name": fields["name"].strip(), "category": fields.get("category", "").strip(),
              "description": fields.get("description", "").strip(),
              "members": json.dumps(normalize(fields.get("members") or []), ensure_ascii=False)}
    ts = db.now()
    if gid:
        db.execute(f"UPDATE agent_groups SET {', '.join(f'{k} = ?' for k in values)}, updated_at = ? WHERE id = ?",
                   (*values.values(), ts, gid))
    else:
        gid = db.execute(f"INSERT INTO agent_groups({', '.join(values)}, created_at, updated_at) "
                         f"VALUES({', '.join('?' * len(values))}, ?, ?)", (*values.values(), ts, ts))
    return get(gid)


def delete(gid: int) -> None:
    db.execute("DELETE FROM agent_groups WHERE id = ?", (gid,))


# ---------------------------------------------------------------- teams of projects

def insert_team(pid: int, group: dict, model: str, effort: str, ts: str) -> int:
    """Create a group's members as the agents of project `pid` (inside a transaction). Members take the
    model and effort of their template, or `model` and `effort`. Returns the lead's id."""
    tpl = {t["slug"]: t for t in db.query("SELECT slug, role, model, effort FROM agent_templates")}
    ids: dict[str, int] = {}
    for i, m in enumerate(group["members"]):
        t = tpl.get(m["template"], {})
        ids[m["key"]] = db._conn.execute(
            "INSERT INTO agents(project_id, parent_id, key, sort, name, role, instructions, model, effort, template, "
            "subagents, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, '', ?, ?, ?, '[]', ?, ?)",
            (pid, ids.get(m["parent"]), m["key"], i, m["name"], t.get("role", ""), t.get("model") or model,
             t.get("effort") or effort, m["template"], ts, ts)).lastrowid
    return ids[group["members"][0]["key"]]


def apply_to_project(pid: int, group: dict, model: str, effort: str) -> int:
    """Replace the agents of project `pid` with the group's team; its tasks move to the new lead and the
    old agents' own lessons are dropped. Returns the new lead's id."""
    with db.transaction():
        db._conn.execute("DELETE FROM learnings WHERE scope = 'self' AND agent_id IN "
                         "(SELECT id FROM agents WHERE project_id = ?)", (pid,))
        db._conn.execute("DELETE FROM agents WHERE project_id = ?", (pid,))
        lead = insert_team(pid, group, model, effort, db.now())
        db._conn.execute("UPDATE tasks SET assignee_id = ? WHERE project_id = ?", (lead, pid))
    return lead


# ---------------------------------------------------------------- starter groups

def preset_status() -> list[dict]:
    installed = {r["slug"] for r in db.query("SELECT slug FROM agent_groups")}
    return [{"slug": g["slug"], "name": g["name"], "category": g["category"], "installed": g["slug"] in installed}
            for g in presets.GROUPS]


def install_presets(slugs: list[str] | None = None) -> int:
    """Install the missing built-in groups (all, or those in `slugs`). Members whose template no longer
    exists are left out."""
    existing = {r["slug"] for r in db.query("SELECT slug FROM agent_groups")}
    templates = {r["slug"] for r in db.query("SELECT slug FROM agent_templates")}
    count = 0
    for g in presets.GROUPS:
        if g["slug"] in existing or (slugs is not None and g["slug"] not in slugs):
            continue
        members = g["members"]
        for missing in {m["template"] for m in members} - templates:
            members = without_template(members, missing)
        if members:
            save({**g, "members": members})
            count += 1
    return count


def seed_once() -> None:
    """Install the starter groups this database has not been offered yet."""
    seen = set(db.get_settings()["group_presets_seen"])
    new = [g["slug"] for g in presets.GROUPS if g["slug"] not in seen]
    if new:
        install_presets(new)
        db.save_settings({"group_presets_seen": sorted(seen | set(new))})
