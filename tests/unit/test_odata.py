from azgenai_lab.services.odata import escape_odata_literal


def test_escape_odata_literal_doubles_single_quote() -> None:
    assert escape_odata_literal("a'b") == "a''b"
