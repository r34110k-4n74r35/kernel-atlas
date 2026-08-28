from pathlib import Path
from tempfile import TemporaryDirectory

from fixture import MAINTAINERS

from kernel_atlas.maintainers import (
    SubsystemMap,
    load,
    parse_maintainers,
    top_level_area,
)


def build():
    return SubsystemMap(parse_maintainers(MAINTAINERS))


def names(smap, path):
    return [s.name for s, _ in smap.match(path)]


def test_sections_parse_with_tags():
    sections = {s.name: s for s in parse_maintainers(MAINTAINERS)}
    ext4 = sections["EXT4 FILE SYSTEM"]
    assert ext4.status == "Maintained"
    assert "Theodore Ts'o <tytso@mit.edu>" in ext4.maintainers
    assert "linux-ext4@vger.kernel.org" in ext4.lists
    assert "fs/ext4/" in ext4.files
    assert ext4.web == "https://ext4.wiki.kernel.org"


def test_prose_header_is_not_a_section():
    assert not any("List of maintainers" in s.name for s in parse_maintainers(MAINTAINERS))


def test_directory_pattern_matches_recursively():
    smap = build()
    assert "EXT4 FILE SYSTEM" in names(smap, "fs/ext4/inode.c")
    assert "EXT4 FILE SYSTEM" in names(smap, "fs/ext4")


def test_star_does_not_cross_a_slash():
    """'F: fs/*' must match fs/open.c but not fs/ext4/inode.c."""
    smap = build()
    assert "FILESYSTEMS (VFS and infrastructure)" in names(smap, "fs/open.c")
    assert "FILESYSTEMS (VFS and infrastructure)" not in names(smap, "fs/ext4/inode.c")


def test_shallow_driver_pattern():
    smap = build()
    assert "ETHERNET DRIVERS (shallow only)" in names(smap, "drivers/net/dummy.c")
    deep = names(smap, "drivers/net/ethernet/intel/igb/igb_main.c")
    assert "ETHERNET DRIVERS (shallow only)" not in deep
    assert "INTEL ETHERNET DRIVERS" in deep


def test_exclusion_wins():
    """FILESYSTEMS claims fs/* but excludes fs/ext4/."""
    smap = build()
    assert "FILESYSTEMS (VFS and infrastructure)" not in names(smap, "fs/ext4/super.c")


def test_middle_wildcard_component():
    smap = build()
    assert "ARCH MM CATCHER" in names(smap, "arch/x86/mm/fault.c")
    assert "ARCH MM CATCHER" not in names(smap, "arch/x86/kernel/setup.c")


def test_most_precise_section_ranks_first():
    smap = build()
    assert names(smap, "fs/ext4/inode.c")[0] == "EXT4 FILE SYSTEM"
    assert names(smap, "drivers/net/ethernet/intel/igb/igb_main.c")[0] == \
        "INTEL ETHERNET DRIVERS"


def test_catch_all_ranks_last_but_still_matches():
    smap = build()
    got = names(smap, "some/random/path.c")
    assert got[-1] == "THE REST"
    assert names(smap, "fs/ext4/inode.c")[-1] == "THE REST"


def test_exact_file_pattern():
    smap = build()
    assert "FILESYSTEMS (VFS and infrastructure)" in names(smap, "include/linux/fs.h")
    assert "FILESYSTEMS (VFS and infrastructure)" not in names(smap, "include/linux/mm.h")


def test_top_level_area_descriptions():
    assert top_level_area("fs/ext4/inode.c")[0] == "Filesystems"
    assert top_level_area("mm/page_alloc.c")[0] == "Memory management"
    assert top_level_area("nonexistent/x.c") is None


def test_character_class_globs_match_files_not_literal_brackets():
    smap = SubsystemMap(parse_maintainers("""\
ARM GIC
M: Maintainer <m@example.com>
F: drivers/irqchip/irq-gic*.[ch]

SCMI
M: Maintainer <m@example.com>
F: drivers/clk/clk-sc[mp]i.c
"""))
    assert names(smap, "drivers/irqchip/irq-gic.c") == ["ARM GIC"]
    assert names(smap, "drivers/irqchip/irq-gic.h") == ["ARM GIC"]
    assert names(smap, "drivers/clk/clk-scmi.c") == ["SCMI"]
    assert names(smap, "drivers/clk/clk-scpi.c") == ["SCMI"]


def test_escaped_star_used_by_netconsole_is_a_prefix_wildcard():
    smap = SubsystemMap(parse_maintainers(r"""NETCONSOLE
M: Maintainer <m@example.com>
F: tools/testing/selftests/drivers/net/netcons\*
"""))
    assert names(smap, "tools/testing/selftests/drivers/net/netcons_basic.sh") == \
        ["NETCONSOLE"]


def test_existing_directory_without_trailing_slash_is_recursive():
    with TemporaryDirectory() as tmp:
        tree = Path(tmp)
        driver = tree / "drivers/infiniband/hw/erdma"
        driver.mkdir(parents=True)
        (driver / "erdma_cmdq.c").write_text("", encoding="utf-8")
        (tree / "MAINTAINERS").write_text("""\
ALIBABA ELASTIC RDMA DRIVER
M: Maintainer <m@example.com>
F: drivers/infiniband/hw/erdma
""", encoding="utf-8")
        smap = load(tree)
        assert names(smap, "drivers/infiniband/hw/erdma/erdma_cmdq.c") == \
            ["ALIBABA ELASTIC RDMA DRIVER"]


def test_name_regex_with_backreference_keeps_its_group_numbering():
    smap = SubsystemMap(parse_maintainers(r"""FIRST
N: (x)

REPEATED WORD
N: (foo)\1
"""))
    assert names(smap, "foofoo") == ["REPEATED WORD"]


def test_specific_name_regex_ranks_ahead_of_broad_file_glob():
    smap = SubsystemMap(parse_maintainers("""\
GENERIC DEVICE TREE
F: arch/*/boot/dts/

IMX PLATFORM
N: imx
"""))
    assert names(smap, "arch/arm64/boot/dts/imx8mq.dts") == [
        "IMX PLATFORM", "GENERIC DEVICE TREE"]
