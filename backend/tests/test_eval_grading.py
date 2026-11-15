"""The evaluation's text matching. A grader that fails a correct answer for its spelling measures nothing."""

from eval.run_eval import mentions


def test_a_version_may_be_written_as_v7():
    assert mentions("version 7", "cause: prompt release v6 -> v7 at 10:00")
    assert mentions("version 7", "order-lookup version 7 was released")
    assert mentions("version 7", "prompt version7")


def test_a_different_version_is_not_a_match():
    assert not mentions("version 7", "release v17 and version 70")
    assert not mentions("version 7", "v6 only")


def test_ordinary_terms_still_need_the_words():
    assert mentions("enterprise", "the enterprise segment looped")
    assert not mentions("enterprise", "agent loops in doc-search")
