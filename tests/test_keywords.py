from petrovaradin_archive.keywords import contains_keyword


def test_latin_and_cyrillic_roots_include_derivatives():
    keywords = ["Petrovaradin", "Петроварадин"]
    assert contains_keyword("Vest iz Petrovaradina: Petrovaradin danas", keywords)
    assert contains_keyword("Вест: Петроварадин данас", keywords)
    assert contains_keyword("Petrovaradinski festival", keywords)
    assert contains_keyword("Петроварадину", keywords)
    assert not contains_keyword("Festival u Novom Sadu", keywords)
