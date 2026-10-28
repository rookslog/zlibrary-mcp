"""Fixed synthetic catalogue shared by offline rehearsal and agent evaluation.

No title is derived from request text. Payloads are synthetic, not real books.
"""

import hashlib
import html
import re
from e_pdf_fixture import pdf_fixture


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
E2_CATALOGUE = [
    {
        **book(501, "Glas-Piece [A Review of Glas]", "Offline Reviewer", 1986),
        "pages": 23,
    },
    book(
        502,
        "Glas",
        "Jacques Derrida",
        1986,
        translator="John P. Leavey and Richard Rand",
        edition="Complete Leavey-Rand",
    ),
    book(
        503,
        "Regulation of political campaigns: Of Grammatology 1976",
        "Spivak, Lawrence E.",
        1976,
    ),
    book(
        504,
        "Of Grammatology",
        "Jacques Derrida; Gayatri Chakravorty Spivak",
        1976,
        translator="Gayatri Chakravorty Spivak",
    ),
    {**book(505, "E Language Study", "Offline French Author"), "language": "french"},
    book(506, "E Language Study", "Offline English Author"),
    book(507, "Reader Labels", "Offline E Reader"),
    book(508, "Reader Oversize", "Offline E Reader"),
]
E2_LIBGEN = [
    {
        **book(520 + i, "E Mixed Language Study", "Offline E Author"),
        "language": "french" if i in (3, 17) else "english",
    }
    for i in range(20)
]
for row in E2_LIBGEN:
    row["md5"] = hashlib.md5(
        (b"%PDF-1.4\n" + (f"Synthetic fixture {row['id']}\n".encode() * 60))
    ).hexdigest()
E_PAGING = [
    book(410 + i, "Crystal Forms", "Niggli", 1979, edition="Other edition")
    for i in range(9)
]
E_HISTORY_PAGING = [
    book(
        910 + i, "Being and Time", "Martin Heidegger", 1996, translator="Joan Stambaugh"
    )
    for i in range(9)
]
E_MODES = {
    "e_catalogue",
    "e_graver_second",
    "e_target_first",
    "e_annas_rows",
    "e_history",
    "e_history_first",
    "e_outcome_unknown",
    "e_real_timeout",
    "e_person",
    "e_transient",
    "e_pacing",
    "e_runtime",
    "e_ceiling",
    "e_malformed",
}
ALL = {
    str(b["id"]): b
    for b in REHEARSAL
    + CATALOGUE
    + PAGING
    + HISTORY
    + LIBGEN
    + E_CATALOGUE
    + E_HISTORY
    + E2_CATALOGUE
    + E2_LIBGEN
    + E_PAGING
    + E_HISTORY_PAGING
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
        epool = [E_CATALOGUE[0]] + E_PAGING + E_CATALOGUE[1:] + E2_CATALOGUE
        if mode == "e_target_first":
            epool = [E_CATALOGUE[1]] + [b for b in epool if b["id"] != 402]
        rows = (
            (LIBGEN + (E2_LIBGEN if mode in E_MODES else []))
            if source == "libgen"
            else CATALOGUE + PAGING + (epool if mode in E_MODES else [])
        )
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
        if mode == "e_graver_second":
            rows = sorted(rows, key=lambda b: b["id"] == 202)

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
    if row["id"] in (507, 508):
        return pdf_fixture(row["id"] == 508)
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
            f"<td>{esc(b['language'].title())}</td><td>100</td><td>1 MB</td><td>{b['extension']}</td>"
            f'<td><a href="/ads.php?md5={b["md5"]}">Mirror</a></td></tr>'
        )
    return (
        "<!doctype html><html><head><title>Offline LibGen</title></head><body>"
        '<table id="tablelibgen">' + "".join(cells) + "</table></body></html>"
    ).encode()
