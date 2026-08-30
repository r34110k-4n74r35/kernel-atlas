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


def test_linux_26_mainline_and_stable_versions_use_their_respective_trees():
    assert links.is_upstream_release("2.6.39")
    assert not links.is_stable_patch("2.6.39")
    mainline = links.links("2.6.39", "mm/page_alloc.c")
    assert "torvalds/linux.git/tree/mm/page_alloc.c?h=v2.6.39" in mainline["git"]
    assert "torvalds/linux/blob/v2.6.39/mm/page_alloc.c" in mainline["github"]

    assert links.is_upstream_release("2.6.32.71")
    assert links.is_stable_patch("2.6.32.71")
    stable = links.links("2.6.32.71", "mm/page_alloc.c")
    assert "stable/linux.git/tree/mm/page_alloc.c?h=v2.6.32.71" in stable["git"]
    assert "gregkh/linux/blob/v2.6.32.71/mm/page_alloc.c" in stable["github"]


def test_modern_zero_sublevel_does_not_invent_an_upstream_tag():
    assert not links.is_upstream_release("6.18.0")
    assert not links.is_stable_patch("6.18.0")
    assert links.links("6.18.0", "mm/page_alloc.c") == {}


def test_mainline_github_and_dir_urls():
    ln = links.links("7.2", "mm", is_dir=True)
    assert "torvalds/linux/tree/v7.2/mm" in ln["github"]
    assert "torvalds/linux.git/tree/mm?h=v7.2" in ln["git"]
    assert "#L" not in ln["elixir"]


def test_docs_and_ident_urls():
    ln = links.links("7.2", "Documentation/mm/page_alloc.rst", ident="__alloc_pages")
    assert ln["docs"].endswith("/v7.2/mm/page_alloc.html")
    assert ln["ident"].endswith("/ident/__alloc_pages")


def test_linux_next_emits_only_its_authoritative_dated_git_link():
    ln = links.links("next-20260827", "mm/page_alloc.c", 42,
                     ident="__alloc_pages")
    assert set(ln) == {"git"}
    assert "git.kernel.org/pub/scm/linux/kernel/git/next/linux-next.git" in ln["git"]
    assert "h=next-20260827" in ln["git"]
    assert ln["git"].endswith("#n42")


def test_repo_paths_are_url_quoted_without_quoting_slashes():
    path = "Documentation/dev-tools/a file, notes.rst"
    ln = links.links("7.2", path)
    for key in ("elixir", "git", "github", "docs"):
        assert "a%20file%2C%20notes" in ln[key]
        assert "Documentation/dev-tools" in ln[key] or key == "docs"


def test_vendor_suffix_does_not_claim_upstream_ref():
    ln = links.links("6.6.12-acme+debug", "mm/page_alloc.c")
    assert ln == {}
    assert not links.is_stable_patch("6.6.12-acme+debug")


def test_local_tree_does_not_claim_upstream_links():
    assert links.links(
        "7.2", "mm/page_alloc.c", source="/work/vendor/linux-7.2") == {}
    assert links.links(
        "7.2", "mm/page_alloc.c", source="https://example.test/linux-7.2.tar.xz") == {}


def test_recorded_kernel_org_source_keeps_upstream_links():
    ln = links.links(
        "7.2", "mm/page_alloc.c",
        source="https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.tar.xz")
    assert "torvalds/linux/blob/v7.2/mm/page_alloc.c" in ln["github"]


def test_kernel_org_archive_must_match_the_indexed_version():
    assert links.links(
        "7.2", "mm/page_alloc.c",
        source=("https://cdn.kernel.org/pub/linux/kernel/v6.x/"
                "linux-6.12.104.tar.xz"),
    ) == {}
    assert links.has_upstream_provenance(
        "7.3-rc1",
        "https://git.kernel.org/torvalds/t/linux-7.3-rc1.tar.gz",
    )


def test_local_linux_next_tree_does_not_claim_authoritative_remote():
    assert links.links(
        "next-20260827", "mm/page_alloc.c",
        source="/work/linux-next") == {}


def test_non_upstream_release_spellings_do_not_claim_links():
    assert links.links("6.6.12-rc1", "mm/page_alloc.c") == {}
    assert links.links("next-vendor", "mm/page_alloc.c") == {}
