from guidance import any_char, block, models
import pytest


def test_text_opener():
    model = models.Mock("<s>open texta")
    with block(opener="open text"):
        model += any_char()
    assert str(model) == "open texta"


def test_text_closer():
    # NOTE(nopdive): Behavioral change, no longer need closer for str call.
    model = models.Mock("<s>a")
    model += "<s>"
    with block(closer="close text"):
        model += any_char()
    assert str(model) == "<s>a"


def test_grammar_opener():
    model = models.Mock("<s>open texta")
    with block(opener="open tex" + any_char()):
        model += any_char()
    assert str(model) == "open texta"


# TODO(nopdive): Review this exception later -- how should we be going about grammars in blocks overall.
@pytest.mark.skip(reason="requires review")
def test_grammar_closer():
    model = models.Mock(["<s>aclose text", "<s>close text"])
    model += "<s>"
    try:
        with block(closer=any_char() + "lose text"):
            model += any_char()
    except:
        return  # we expect an exception
    assert (
        False
    ), "We should have thrown an exception using a context (prompt) based grammar in the closer!"


def test_block_name_capture():
    model = models.Mock("<s>open texta")
    with block("my_data"):
        model += "open text"
        model += any_char()
    assert model["my_data"] == "open texta"


def test_block_name_capture_closed():
    model = models.Mock("<s>open texta")
    with block("my_data"):
        model += "open text"
        model += any_char()
    model += "tmp"
    assert model["my_data"] == "open texta"
