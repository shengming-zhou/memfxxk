from memfxxk.text import MixedTokenizer, build_tsquery_literal, build_tsvector_literal


def test_tokenizer_normalizes_fullwidth_text_and_splits_identifiers() -> None:
    assert MixedTokenizer().tokenize("ＰａｒｓｅHTTPResponse ［！］") == [
        "parsehttpresponse",
        "parse",
        "http",
        "response",
    ]


def test_tsvector_literal_preserves_positions_and_caps_position_limit() -> None:
    assert build_tsvector_literal(["alpha", "beta", "alpha"]) == "'alpha':1,3 'beta':2"
    tokens = ["alpha"] * 16384 + ["tail"]
    assert "'tail':16383" in build_tsvector_literal(tokens)


def test_tsquery_literal_is_empty_without_tokens() -> None:
    assert build_tsquery_literal([]) is None
