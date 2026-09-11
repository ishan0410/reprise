from cua.surface.snapshot import parse_refs

SAMPLE = """\
- generic [active] [ref=e1]:
  - heading "Staff Sign In" [level=2] [ref=e16]
  - cell "Username:" [ref=e21]
  - textbox "Username" [ref=e23]
  - button "Sign In" [ref=e31]
  - link "Log out" [ref=f2e10] [cursor=pointer]:
    - /url: /logout
  - cell [ref=e7]
  - text: Member Services Console
  - cell "Say \\"hi\\"" [ref=e99]
"""


def test_parses_roles_names_and_refs() -> None:
    refs = parse_refs(SAMPLE)
    assert refs["e16"].role == "heading" and refs["e16"].name == "Staff Sign In"
    assert refs["e23"].role == "textbox" and refs["e23"].name == "Username"
    assert refs["e31"].role == "button" and refs["e31"].name == "Sign In"
    assert refs["f2e10"].role == "link" and refs["f2e10"].name == "Log out"


def test_nameless_nodes_and_non_ref_lines() -> None:
    refs = parse_refs(SAMPLE)
    assert refs["e7"].role == "cell" and refs["e7"].name is None
    assert "/url" not in refs and len(refs) == 8


def test_unescapes_quotes_in_names() -> None:
    assert parse_refs(SAMPLE)["e99"].name == 'Say "hi"'
