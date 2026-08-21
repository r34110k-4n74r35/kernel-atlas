from fixture import MAINTAINERS

from kernel_atlas.maintainers import SubsystemMap, parse_maintainers, top_level_area


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
