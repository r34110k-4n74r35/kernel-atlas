from kernel_atlas import links


def test_elixir_and_docs_tags():
    assert links.elixir_tag("7.2") == "v7.2"
    assert links.elixir_tag("v6.18.45") == "v6.18.45"
    assert links.elixir_tag("next-20260101") == "latest"
    assert links.docs_series("6.18.45") == "v6.18"
    assert links.docs_series("7.2") == "v7.2"


def test_stable_patch_goes_to_the_stable_tree():
    assert links.is_stable_patch("6.18.45")
    assert not links.is_stable_patch("7.2")
    ln = links.links("6.18.45", "mm/page_alloc.c", 100)
    assert "elixir.bootlin.com/linux/v6.18.45/source/mm/page_alloc.c#L100" in ln["elixir"]
    assert "stable/linux.git" in ln["git"] and "h=v6.18.45" in ln["git"]
    assert "#n100" in ln["git"]
    assert "gregkh/linux/blob/v6.18.45/mm/page_alloc.c#L100" in ln["github"]
    assert "docs" not in ln


def test_mainline_github_and_dir_urls():
    ln = links.links("7.2", "mm", is_dir=True)
    assert "torvalds/linux/tree/v7.2/mm" in ln["github"]
    assert "torvalds/linux.git/tree/mm?h=v7.2" in ln["git"]
    assert "#L" not in ln["elixir"]


def test_docs_and_ident_urls():
    ln = links.links("7.2", "Documentation/mm/page_alloc.rst", ident="__alloc_pages")
    assert ln["docs"].endswith("/v7.2/mm/page_alloc.html")
    assert ln["ident"].endswith("/ident/__alloc_pages")
