# -*- coding: utf-8 -*-
"""Build the portfolio's data from a Google Sheet.

Source of truth is a Google Sheet, exposed per-tab as published CSV. The URLs
live in env vars (a local .env, or GitHub Actions secrets) so they never end up
in the repo:

    SHEET_CSV_PROJECTS
    SHEET_CSV_SKILLS
    SHEET_CSV_CERTS

Any URL that is not set falls back to the matching file in data/, which is how
this runs before the sheet is wired up.

    python scripts/sync_sheets.py            # refresh data.json + index.html
    python scripts/sync_sheets.py --check    # verify only, non-zero exit on drift
    python scripts/sync_sheets.py --strict   # fail unless every sheet URL is set
"""
import csv, io, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
HTML = os.path.join(ROOT, "index.html")

MULTI_SEP = ";"   # separates values inside one cell
LINK_SEP = "|"    # separates a link's label from its url

TABS = (
    # name        env var               local fallback        required columns
    ("projects", "SHEET_CSV_PROJECTS", "data/projects.csv", ("title", "desc")),
    ("skills",   "SHEET_CSV_SKILLS",   "data/skills.csv",   ("category", "items")),
    ("certs",    "SHEET_CSV_CERTS",    "data/certs.csv",    ("name", "issuer")),
)

# The data is written straight into the page rather than fetched at runtime, so
# the page renders instantly, needs no network, and still works when index.html
# is opened directly as a file:// URL (where fetch() is blocked by the browser).
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


def read_tab(name, env_var, local, required):
    url = os.environ.get(env_var, "").strip()
    if not url and "--strict" in sys.argv:
        # CI runs with --strict so a missing secret fails loudly, instead of
        # quietly rebuilding from the possibly-stale CSVs committed in data/.
        die("%s is not set. In strict mode every tab must come from the sheet."
            % env_var)
    if url:
        if not url.startswith("https://"):
            die("%s must be an https URL" % env_var)
        req = urllib.request.Request(url, headers={"User-Agent": "portfolio-sync"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status != 200:
                die("%s returned HTTP %s" % (env_var, r.status))
            text = r.read().decode("utf-8-sig")
        origin = "sheet (%s)" % env_var
    else:
        with io.open(os.path.join(ROOT, local), encoding="utf-8-sig") as f:
            text = f.read()
        origin = "local %s" % local

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        die("tab '%s' has no data rows (%s)" % (name, origin))

    have = {(k or "").strip() for k in rows[0].keys()}
    missing = [c for c in required if c not in have]
    if missing:
        die("tab '%s' is missing column(s) %s -- found %s (%s)"
            % (name, ", ".join(missing), sorted(have), origin))

    clean = []
    for r in rows:
        row = {(k or "").strip(): (v or "").strip()
               for k, v in r.items() if k is not None}
        if any(row.values()):
            clean.append(row)
    print("  %-9s %2d rows  <- %s" % (name, len(clean), origin))
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
    print("reading tabs:")
    raw = {n: read_tab(n, e, l, r) for n, e, l, r in TABS}

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
        "  /* Refresh with: python scripts/sync_sheets.py */",
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
        head = src[:src.index(BEGIN)]
        tail = src[src.index(END) + len(END):]
        return head + block + tail

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
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    src = io.open(HTML, encoding="utf-8").read()
    patched = inject(data, src)

    if "--check" in sys.argv:
        current = io.open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        stale = [n for n, ok in (("data.json", current == text),
                                 ("index.html", patched == src)) if not ok]
        if not stale:
            print("\nup to date: data.json and index.html")
            return 0
        print("\nSTALE: %s -- run: python scripts/sync_sheets.py" % ", ".join(stale))
        return 1

    io.open(OUT, "w", encoding="utf-8", newline="").write(text)
    io.open(HTML, "w", encoding="utf-8", newline="").write(patched)
    print("\nwrote data.json and injected into index.html")
    print("  %d projects, %d skill groups, %d certs"
          % (len(data["projects"]), len(data["skills"]), len(data["certs"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
