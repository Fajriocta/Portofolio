# -*- coding: utf-8 -*-
"""Generate the portfolio's data block in index.html from a Google Sheet.

This script is deliberately NOT tracked in the repo (see .git/info/exclude).
Run it locally, then commit index.html -- the sheet stays the only copy of the
data, and the repo only carries what the site needs to render.

The sheet is the single source of truth. Each tab is read as published CSV, from
URLs held in env vars -- a local .env -- so the URLs never land in the repo:

    SHEET_CSV_PROJECTS
    SHEET_CSV_SKILLS
    SHEET_CSV_CERTS

    python scripts/sync_sheets.py            # rewrite the block in index.html
    python scripts/sync_sheets.py --check    # verify only, non-zero exit on drift
"""
import csv, io, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "index.html")

MULTI_SEP = ";"   # separates values inside one cell
LINK_SEP = "|"    # separates a link's label from its url

TABS = (
    # name        env var               required columns
    ("projects", "SHEET_CSV_PROJECTS", ("title", "desc")),
    ("skills",   "SHEET_CSV_SKILLS",   ("category", "items")),
    ("certs",    "SHEET_CSV_CERTS",    ("name", "issuer")),
)

# The data is written straight into the page rather than fetched at runtime, so
# the page renders instantly, needs no network, keeps the sheet URLs private,
# and still works when index.html is opened directly as a file:// URL.
BEGIN = "  /* GENERATED DATA - do not edit by hand */"
END = "  /* END GENERATED DATA */"


def die(msg):
    sys.stderr.write("ERROR: %s\n" % msg)
    sys.exit(1)


def load_dotenv():
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_tab(name, env_var, required):
    url = os.environ.get(env_var, "").strip()
    if not url:
        die("%s is not set. Put the published-CSV URL for the '%s' tab in .env"
            % (env_var, name))
    if not url.startswith("https://"):
        die("%s must be an https URL" % env_var)

    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status != 200:
            die("%s returned HTTP %s" % (env_var, r.status))
        text = r.read().decode("utf-8-sig")

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        die("tab '%s' came back empty -- check that %s points at the right tab"
            % (name, env_var))

    have = {(k or "").strip() for k in rows[0].keys()}
    missing = [c for c in required if c not in have]
    if missing:
        die("tab '%s' is missing column(s) %s -- found %s. Either %s points at "
            "the wrong tab, or a header was renamed."
            % (name, ", ".join(missing), sorted(have), env_var))

    clean = []
    for r in rows:
        row = {(k or "").strip(): (v or "").strip()
               for k, v in r.items() if k is not None}
        if any(row.values()):
            clean.append(row)
    print("  %-9s %2d rows" % (name, len(clean)))
    return clean


def multi(cell):
    return [p.strip() for p in cell.split(MULTI_SEP) if p.strip()] if cell else []


def parse_links(cell, where):
    out = []
    for chunk in multi(cell):
        if LINK_SEP not in chunk:
            die("%s: link %r must be written as Label %s https://..."
                % (where, chunk, LINK_SEP))
        label, href = [x.strip() for x in chunk.split(LINK_SEP, 1)]
        if not label or not href:
            die("%s: link %r is missing a label or a url" % (where, chunk))
        if not href.startswith(("http://", "https://")):
            die("%s: link url %r must start with http:// or https://" % (where, href))
        out.append({"label": label, "href": href})
    return out


def build():
    load_dotenv()
    print("reading tabs from the sheet:")
    raw = {n: read_tab(n, e, r) for n, e, r in TABS}

    projects = []
    for n, r in enumerate(raw["projects"], 1):
        if not r.get("title"):
            die("projects row %d has no title" % n)
        p = {
            "i": "%02d" % n,  # display number, taken from row order
            "title": r["title"],
            "desc": r.get("desc", ""),
            "tools": multi(r.get("tools", "")),
            "skills": multi(r.get("skills", "")),
            "roles": multi(r.get("roles", "")),
        }
        if r.get("highlight"):
            p["highlight"] = r["highlight"]
        if r.get("impact"):
            p["impact"] = multi(r["impact"])
        links = parse_links(r.get("links", ""),
                            "projects row %d (%s)" % (n, r["title"]))
        if links:
            p["links"] = links
        projects.append(p)

    skills = []
    for n, r in enumerate(raw["skills"], 1):
        if not r.get("category"):
            die("skills row %d has no category" % n)
        items = multi(r.get("items", ""))
        if not items:
            die("skills row %d (%s) has no items" % (n, r["category"]))
        skills.append({"key": r["category"], "items": items})

    certs = []
    for n, r in enumerate(raw["certs"], 1):
        if not r.get("name"):
            die("certs row %d has no name" % n)
        certs.append({"name": r["name"], "issuer": r.get("issuer", ""),
                      "date": r.get("date", "")})

    return {"projects": projects, "skills": skills, "certs": certs}


def js_block(data):
    """Render the three arrays as an indented, diff-friendly JS literal block."""
    def arr(name, value):
        body = json.dumps(value, indent=2, ensure_ascii=False)
        body = "\n".join("  " + ln for ln in body.split("\n")).lstrip()
        return "  const %s = %s;" % (name, body)

    return "\n".join([
        BEGIN,
        "  /* Source: the Google Sheet. Refresh with scripts/sync_sheets.py */",
        arr("SKILLS", data["skills"]),
        "",
        arr("PROJECTS", data["projects"]),
        "",
        arr("CERTS", data["certs"]),
        END,
    ])


def inject(data, src):
    """Replace the generated block in index.html, or create it on first run."""
    block = js_block(data)

    if BEGIN in src and END in src:
        if src.count(BEGIN) != 1 or src.count(END) != 1:
            die("index.html: generated-data markers appear more than once")
        return src[:src.index(BEGIN)] + block + src[src.index(END) + len(END):]

    # First run: the arrays are still hand-written. Replace the span holding
    # SKILLS, PROJECTS and CERTS, which sits between EXPERIENCE and HUMAN_EDGE.
    start_at, stop_at = "  const SKILLS = [", "  const HUMAN_EDGE = ["
    for anchor in (start_at, stop_at):
        if src.count(anchor) != 1:
            die("index.html: expected exactly one %r, found %d"
                % (anchor.strip(), src.count(anchor)))
    i, j = src.index(start_at), src.index(stop_at)
    if i > j:
        die("index.html: SKILLS appears after HUMAN_EDGE; refusing to guess")
    return src[:i] + block + "\n\n" + src[j:]


def main():
    data = build()
    src = io.open(HTML, encoding="utf-8").read()
    patched = inject(data, src)
    counts = ("%d projects, %d skill groups, %d certs"
              % (len(data["projects"]), len(data["skills"]), len(data["certs"])))

    if "--check" in sys.argv:
        if patched == src:
            print("\nindex.html is up to date (%s)" % counts)
            return 0
        print("\nindex.html is STALE -- run: python scripts/sync_sheets.py")
        return 1

    io.open(HTML, "w", encoding="utf-8", newline="").write(patched)
    print("\nwrote index.html (%s)" % counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
