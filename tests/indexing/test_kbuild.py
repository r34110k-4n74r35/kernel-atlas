"""Unit tests for conservative Kbuild evidence; builds live in test_indexer."""

import pytest

from kernel_atlas.indexing import kbuild


@pytest.mark.parametrize(
    "name",
    [
        "hostprogs",
        "host-progs",
        "userprogs",
        "hostprogs-always-y",
        "hostprogs-always-m",
        "userprogs-always-$(CONFIG_CC_CAN_LINK)",
    ],
)
def test_kbuild_program_list_names_are_recognized(name):
    assert kbuild._is_program_list(name)


@pytest.mark.parametrize(
    "name",
    [
        "hostprogs-installed",
        "userprogs-always-n",
        "obj-y",
        "always-y",
    ],
)
def test_unrelated_kbuild_lists_are_not_programs(name):
    assert not kbuild._is_program_list(name)


def test_pure_kbuild_addprefix_object_list_is_expanded():
    values = {
        "libfdt-objs": ["fdt.o fdt_ro.o"],
        "libfdt": ["$(addprefix libfdt/,$(libfdt-objs))"],
        "fdtoverlay-objs": ["fdtoverlay.o $(libfdt)"],
    }
    assert (
        kbuild._expand_make_value(" ".join(values["fdtoverlay-objs"]), values)
        == "fdtoverlay.o libfdt/fdt.o libfdt/fdt_ro.o"
    )


@pytest.mark.parametrize(
    "name",
    [
        "obj-y",
        "obj-$(CONFIG_TEST)",
        "lib-m",
        "module-objs",
        "module-y",
        "module-$(CONFIG_TEST)",
        "always-y",
    ],
)
def test_kbuild_compile_link_object_lists_are_recognized(name):
    assert kbuild._is_kbuild_object_list(name)


@pytest.mark.parametrize(
    "name",
    [
        "clean-files",
        "targets",
        "ccflags-y",
        "subdir-ccflags-y",
        "CFLAGS_x.o",
    ],
)
def test_non_build_object_lists_are_not_compile_evidence(name):
    assert not kbuild._is_kbuild_object_list(name)


def test_recipes_and_unexpanded_defines_are_not_build_evidence(tmp_path):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "all:\n"
        "\tobj-y += recipe.o\n"
        "\tccflags-y += -I$(src)/recipe\n"
        "define unused_template\n"
        "obj-y += template.o\n"
        "template.o: template.c\n"
        "define nested_template\n"
        "hostprogs += unused\n"
        "endef\n"
        "endef\n"
        "obj-y += real.o\n"
        "real.o: real.c\n",
        encoding="utf-8",
    )
    values, text = kbuild._make_assignments(makefile)
    assert values == {"obj-y": ["real.o"]}
    assert kbuild._explicit_rule_sources(
        "", text, {"real.c", "recipe.c", "template.c"}
    ) == {"real.c"}
