"""Tests for the s-expression reader/writer."""

import pytest

from kicad_jlcpcb_mcp.sexpr import (
    SExprError,
    add,
    dump,
    find,
    find_all,
    parse,
    replace,
)


class TestParseBasic:
    def test_simple(self):
        assert parse("(hello)") == ["hello"]

    def test_atoms(self):
        assert parse("(foo bar baz)") == ["foo", "bar", "baz"]

    def test_nested(self):
        assert parse("(a (b c) (d e f))") == ["a", ["b", "c"], ["d", "e", "f"]]

    def test_quoted_string(self):
        assert parse('(name "hello world")') == ["name", "hello world"]

    def test_quoted_with_parens(self):
        assert parse('(text "(parens)")') == ["text", "(parens)"]

    def test_escaped_quote(self):
        assert parse('(text "say \\"hi\\"")') == ["text", 'say "hi"']

    def test_numbers_as_strings(self):
        assert parse("(at 1.27 -3.5 90)") == ["at", "1.27", "-3.5", "90"]

    def test_empty_string_atom(self):
        assert parse('(rev "")') == ["rev", ""]


class TestParseErrors:
    def test_empty_input(self):
        with pytest.raises(SExprError, match="Empty"):
            parse("")

    def test_no_leading_paren(self):
        with pytest.raises(SExprError, match="Expected"):
            parse("hello")

    def test_unclosed_list(self):
        with pytest.raises(SExprError, match="Unclosed"):
            parse("(hello")

    def test_unterminated_string(self):
        with pytest.raises(SExprError, match="Unterminated"):
            parse('(name "hello')

    def test_trailing_garbage(self):
        with pytest.raises(SExprError, match="Trailing"):
            parse("(a) (b)")


class TestDump:
    def test_simple(self):
        assert dump(["hello"]) == "(hello)"

    def test_atoms(self):
        assert dump(["foo", "bar", "baz"]) == "(foo bar baz)"

    def test_quotes_strings_with_spaces(self):
        assert dump(["name", "hello world"]) == '(name "hello world")'

    def test_escapes_internal_quotes(self):
        out = dump(["text", 'say "hi"'])
        assert '\\"hi\\"' in out

    def test_nested_indents(self):
        node = ["root", ["child", "1"], ["child", "2"]]
        out = dump(node)
        assert "(root" in out
        assert "  (child 1)" in out
        assert "  (child 2)" in out

    def test_empty_string_quoted(self):
        assert dump(["rev", ""]) == '(rev "")'


class TestRoundTrip:
    def test_simple_roundtrip(self):
        text = "(a b c)"
        assert dump(parse(text)) == text

    def test_nested_roundtrip(self):
        text = '(root\n  (child "value")\n  (other 1.5)\n)'
        round1 = dump(parse(text))
        round2 = dump(parse(round1))
        assert round1 == round2

    def test_kicad_sch_skeleton_roundtrips(self):
        # The minimal schematic skeleton from project.py
        text = (
            '(kicad_sch (version 20231120) (generator "kicad_jlcpcb_mcp")\n'
            '  (uuid "00000000-0000-0000-0000-000000000000")\n'
            '  (paper "A4")\n'
            "  (lib_symbols)\n"
            ")\n"
        )
        node = parse(text)
        assert node[0] == "kicad_sch"
        # Re-emit and re-parse to confirm structural stability
        assert parse(dump(node)) == node


class TestFind:
    def test_finds_named_child(self):
        node = parse("(root (a 1) (b 2) (c 3))")
        assert find(node, "b") == ["b", "2"]

    def test_returns_none_for_missing(self):
        node = parse("(root (a 1))")
        assert find(node, "missing") is None

    def test_find_all(self):
        node = parse("(root (pin 1) (pin 2) (other) (pin 3))")
        all_pins = find_all(node, "pin")
        assert len(all_pins) == 3
        assert all_pins[0] == ["pin", "1"]


class TestPatch:
    def test_add_appends(self):
        node = parse("(root (a 1))")
        add(node, ["b", "2"])
        assert find(node, "b") == ["b", "2"]

    def test_replace_swaps(self):
        node = parse("(root (version 1) (a))")
        ok = replace(node, "version", ["version", "2"])
        assert ok is True
        assert find(node, "version") == ["version", "2"]

    def test_replace_returns_false_when_missing(self):
        node = parse("(root (a))")
        ok = replace(node, "version", ["version", "2"])
        assert ok is False
