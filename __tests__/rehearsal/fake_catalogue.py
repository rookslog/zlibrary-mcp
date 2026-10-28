"""Fixed synthetic catalogue shared by offline rehearsal and agent evaluation.

No title is derived from request text. Payloads are synthetic, not real books.
"""

import hashlib
import html
import re


def book(id, title, author="Offline Author", year=2026, extension="pdf", **extra):
    return dict(
        id=id,
        hash=f"hash{id}",
        title=title,
        author=author,
        year=str(year),
        extension=extension,
        language="english",
        filesize=1024,
        pages=100,
        publisher="Fixture Press",
        **extra,
    )


# Keep the original rehearsal observations as named, fixed records, not an echo.
REHEARSAL_QUERIES = [
    "sequential",
    "slow-first",
    "fast-second",
    "slow-third",
    "fast-fourth",
    "fast-fifth",
    "host-0",
    "host-1",
    "warm",
    "refresh-0",
    "refresh-1",
    "refresh-2",
    "refresh-3",
    "refresh-4",
    "recovered",
]
REHEARSAL = [
    book(101 + i, title, extension="txt")
    for i, title in enumerate(
        [
            "Rehearsal sequential",
            "Rehearsal slow-first",
            "Rehearsal fast-second",
            "Rehearsal slow-third",
            "Rehearsal fast-fourth",
            "Rehearsal fast-fifth",
            "Rehearsal host-0",
            "Rehearsal host-1",
            "Rehearsal warm",
            "Rehearsal refresh-0",
            "Rehearsal refresh-1",
            "Rehearsal refresh-2",
            "Rehearsal refresh-3",
            "Rehearsal refresh-4",
            "Rehearsal recovered",
        ]
    )
]
REHEARSAL[0].update(description="Details for rehearsal book 101", filesize=27)
CATALOGUE = [
    book(201, "Persuasion", "Jane Austen", 1818),
    book(
        202,
        "Letters on Ethics: To Lucilius",
        "Seneca",
        2015,
        translator="Margaret Graver and A. A. Long",
        edition="Graver–Long",
        description="Translated by Margaret Graver and A. A. Long; 2015 edition.",
    ),
    book(
        203,
        "Letters from a Stoic",
        "Seneca",
        1969,
        translator="Robin Campbell",
        edition="Campbell",
        description="Translated by Robin Campbell; 1969 edition.",
    ),
    book(204, "The Human Condition", "Hannah Arendt", 1958),
    book(205, "The Origins of Totalitarianism", "Hannah Arendt", 1951),
    book(206, "Eichmann in Jerusalem", "Hannah Arendt", 1963),
    book(207, "The Life of the Mind", "Hannah Arendt", 1978),
    book(208, "Love and Saint Augustine", "Hannah Arendt", 1929),
    book(209, "Pride and Prejudice", "Jane Austen", 1813),
    book(210, "Middlemarch", "George Eliot", 1871),
    book(211, "On Liberty", "John Stuart Mill", 1859),
    book(212, "A Room of One's Own", "Virginia Woolf", 1929),
    book(213, "The Republic", "Plato", 1992, translator="G. M. A. Grube"),
]
# Sixty stable, distinct rows make paging observable. Not query-generated titles.
PAGING = [
    book(
        1000 + i,
        f"Phenomenology Studies, Volume {i + 1:02}",
        "Catalogue Collective",
        1900 + i,
    )
    for i in range(60)
]
HISTORY = [
    REHEARSAL[0],
    book(
        901,
        "Being and Time",
        "Martin Heidegger",
        1962,
        translator="John Macquarrie and Edward Robinson",
    ),
]
LIBGEN = [
    book(301, "Persuasion", "Jane Austen", 1818, extension="epub"),
    book(302, "The Glass Orchard", "Mira Vale", 2001),
    book(303, "Pride and Prejudice", "Jane Austen", 1813, extension="epub"),
    book(304, "Middlemarch", "George Eliot", 1871, extension="epub"),
    book(305, "On Liberty", "John Stuart Mill", 1859, extension="epub"),
    book(306, "Rehearsal LibGen fixture", extension="txt"),
]
for row in LIBGEN:
    row["md5"] = hashlib.md5(str(row["id"]).encode()).hexdigest()
LIBGEN[-1]["md5"] = "0123456789abcdef0123456789abcdef"
# E-only records do not alter any legacy row or legacy account history.
E_CATALOGUE = [
    book(401, "Crystal Forms", "Niggli", 1979, edition="Look-alike"),
    book(402, "Crystal Forms", "Niggli", 1981, edition="Requested 1981 edition"),
    book(403, "Uncertain Transfer", "Offline E Author", 2026),
]
E_HISTORY = [
    book(902, "Being and Time", "Martin Heidegger", 1996, translator="Joan Stambaugh"),
    HISTORY[1],
]
ALL = {
    str(b["id"]): b
    for b in REHEARSAL + CATALOGUE + PAGING + HISTORY + LIBGEN + E_CATALOGUE + E_HISTORY
}


def norm(text):
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def select(query, source="zlibrary", fields=None, mode="healthy"):
    fields = fields or {}
    if mode == "empty":
        return []
    q = norm(query)
    if source == "zlibrary" and query in REHEARSAL_QUERIES:
        rows = [REHEARSAL[REHEARSAL_QUERIES.index(query)]]
    elif source == "libgen" and q == "fixture":
        rows = [LIBGEN[-1]]
    else:
        rows = LIBGEN if source == "libgen" else CATALOGUE + PAGING + E_CATALOGUE
        words = q.split()
        rows = [
            b
            for b in rows
            if words
            and all(
                w
                in norm(
                    " ".join(
                        str(b.get(k, ""))
                        for k in ["title", "author", "translator", "isbn"]
                    )
                )
                for w in words
            )
        ]
        if source == "zlibrary" and q in {"seneca letters", "letters seneca"}:
            rows = [b for b in CATALOGUE if b["author"] == "Seneca"]

    def first(key, default=None):
        v = fields.get(key, [default])
        return v[0] if isinstance(v, list) else v

    rows = [
        b
        for b in rows
        if (not first("yearFrom") or int(b["year"]) >= int(first("yearFrom")))
        and (not first("yearTo") or int(b["year"]) <= int(first("yearTo")))
        and (not fields.get("extensions[]") or b["extension"] in fields["extensions[]"])
        and (not fields.get("languages[]") or b["language"] in fields["languages[]"])
    ]
    if first("e") == "1":
        rows = [
            b
            for b in rows
            if q
            in {
                norm(b["title"]),
                norm(b["author"]),
                norm(b["title"] + " " + b["author"]),
            }
        ]
    limit, page = int(first("limit", 10)), int(first("page", 1))
    return rows[(page - 1) * limit : page * limit]


def payload(row):
    if row["id"] == 101:
        return b"Offline rehearsal book 101.\n"
    header = b"%PDF-1.4\n" if row["extension"] == "pdf" else b"PK\x03\x04"
    return header + (f"Synthetic fixture {row['id']}\n".encode() * 60)


# Acquisition verifies MD5 against the actual synthetic bytes, not an ID.
for row in LIBGEN[:-1]:
    row["md5"] = hashlib.md5(payload(row)).hexdigest()


def libgen_html(rows):
    def esc(value):
        return html.escape(str(value), quote=True)

    cells = []
    for b in rows:
        cells.append(
            f'<tr><td><a href="/edition.php?id={b["id"]}">{esc(b["title"])}</a></td>'
            f"<td>{esc(b['author'])}</td><td>Fixture Press</td><td>{b['year']}</td>"
            f"<td>English</td><td>100</td><td>1 MB</td><td>{b['extension']}</td>"
            f'<td><a href="/ads.php?md5={b["md5"]}">Mirror</a></td></tr>'
        )
    return (
        "<!doctype html><html><head><title>Offline LibGen</title></head><body>"
        '<table id="tablelibgen">' + "".join(cells) + "</table></body></html>"
    ).encode()
