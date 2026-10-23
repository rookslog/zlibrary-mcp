"""MARC-8 mnemonics in catalogue metadata: decoded for display and filenames,
kept raw in the acquisition record."""

import unicodedata

import pytest
from lib import acquisition as acq
from lib.filename_utils import create_unified_filename, decode_marc_mnemonics

GRID_AUTHOR = "M{uml}uller-Brockmann, Josef, 1914-"
GRID_TITLE = "Raster systeme f{uml}ur die visuelle Gestaltung"


def test_catalogue_record_strings_decode():
    assert decode_marc_mnemonics(GRID_AUTHOR) == "Müller-Brockmann, Josef, 1914-"
    assert decode_marc_mnemonics("f{uml}ur") == "für"


@pytest.mark.parametrize(
    "raw, decoded",
    [
        ("M{uml}uller", "Müller"),
        ("Andr{acute}e", "André"),
        ("tr{grave}es", "très"),
        ("h{circ}otel", "hôtel"),
        ("Fran{cedil}cois", "François"),
        ("Espa{tilde}na", "España"),
        ("{ring}Angstr{uml}om", "Ångström"),
        ("{caron}Capek", "Čapek"),
    ],
)
def test_combining_mnemonic_modifies_the_following_letter(raw, decoded):
    # MARC order: the diacritic comes before its base letter, never after.
    result = decode_marc_mnemonics(raw)
    assert result == decoded
    assert unicodedata.is_normalized("NFC", result)


@pytest.mark.parametrize(
    "raw, decoded",
    [
        ("Stra{szlig}e", "Straße"),
        ("{eth}a", "ða"),
        ("{thorn}orn", "þorn"),
        ("{aelig}on", "æon"),
        ("{AElig}lfric", "Ælfric"),
        ("S{oslash}ren", "Søren"),
        ("{Oslash}stergaard", "Østergaard"),
    ],
)
def test_spacing_mnemonics(raw, decoded):
    assert decode_marc_mnemonics(raw) == decoded


@pytest.mark.parametrize("raw", ["{unknown}x", "x{uml}", "{uml}", "{uml} a", "{Uml}a"])
def test_unknown_or_ill_placed_mnemonics_stay_as_is(raw):
    assert decode_marc_mnemonics(raw) == raw


def test_filename_folds_the_decoded_letter():
    name = create_unified_filename(
        {"author": GRID_AUTHOR, "title": "Grid systems", "id": "1", "extension": "pdf"}
    )
    # "ü" folds to "u" as any accented letter does; no "Uml" debris.
    assert name.startswith("MullerBrockmannJosef1914_GridSystems_")
    assert name == create_unified_filename(
        {
            "author": "Müller-Brockmann, Josef, 1914-",
            "title": "Grid systems",
            "id": "1",
            "extension": "pdf",
        }
    )
    assert "Uml" not in create_unified_filename(
        {"title": GRID_TITLE, "authors": [GRID_AUTHOR], "id": "1"}
    )


def test_record_keeps_raw_and_adds_decoded_beside_it(tmp_path):
    book = {
        "title": GRID_TITLE,
        "author": GRID_AUTHOR,
        "publisher": "Niggli",
        "id": "1",
    }
    record = acq._record(book, "zlibrary", {"id": "1"}, tmp_path / "x.pdf", {})
    assert record["author"] == GRID_AUTHOR
    assert record["author_decoded"] == "Müller-Brockmann, Josef, 1914-"
    assert record["title"] == GRID_TITLE
    assert record["title_decoded"] == "Raster systeme für die visuelle Gestaltung"
    assert "publisher_decoded" not in record
