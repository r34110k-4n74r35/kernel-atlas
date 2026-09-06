"""Build a tiny fake kernel tree that exercises the real code paths."""

from __future__ import annotations

from pathlib import Path

MAINTAINERS = """\
\tList of maintainers and how to submit kernel changes

Maintainers List
----------------

EXT4 FILE SYSTEM
M:\tTheodore Ts'o <tytso@mit.edu>
M:\tAndreas Dilger <adilger@dilger.ca>
L:\tlinux-ext4@vger.kernel.org
S:\tMaintained
W:\thttps://ext4.wiki.kernel.org
F:\tDocumentation/filesystems/ext4/
F:\tfs/ext4/

FILESYSTEMS (VFS and infrastructure)
M:\tAlexander Viro <viro@zeniv.linux.org.uk>
L:\tlinux-fsdevel@vger.kernel.org
S:\tMaintained
F:\tfs/*
F:\tinclude/linux/fs.h
X:\tfs/ext4/

MEMORY MANAGEMENT
M:\tAndrew Morton <akpm@linux-foundation.org>
L:\tlinux-mm@kvack.org
S:\tMaintained
F:\tmm/

FUTEX SUBSYSTEM
M:\tFutex Maintainer <futex@example.com>
L:\tlinux-kernel@vger.kernel.org
S:\tMaintained
F:\tkernel/futex/*
F:\tDocumentation/locking/*futex*

NETWORKING [IPv4/IPv6]
M:\tDavid S. Miller <davem@davemloft.net>
L:\tnetdev@vger.kernel.org
S:\tMaintained
F:\tnet/ipv4/
F:\tnet/ipv6/

ETHERNET DRIVERS (shallow only)
M:\tJakub Kicinski <kuba@kernel.org>
L:\tnetdev@vger.kernel.org
S:\tMaintained
F:\tdrivers/net/*

INTEL ETHERNET DRIVERS
M:\tTony Nguyen <anthony.l.nguyen@intel.com>
L:\tintel-wired-lan@lists.osuosl.org
S:\tSupported
F:\tdrivers/net/ethernet/intel/

ARCH MM CATCHER
M:\tNobody <nobody@example.com>
S:\tOdd Fixes
F:\tarch/*/mm/

THE REST
M:\tLinus Torvalds <torvalds@linux-foundation.org>
L:\tlinux-kernel@vger.kernel.org
S:\tBuried alive in reporters
F:\t*
F:\t*/
"""

EXT4_INODE_C = """\
#include <linux/fs.h>

static int ext4_inode_blocks_set(struct ext4_inode *raw_inode)
{
\treturn 0;
}

int ext4_get_block(struct inode *inode, sector_t iblock)
{
\tint err = ext4_inode_blocks_set(NULL);
\treturn err;
}

sector_t ext4_bmap(struct address_space *mapping, sector_t block)
{
\text4_get_block(NULL, block);
\treturn 0;
}
EXPORT_SYMBOL(ext4_bmap);

static inline void ext4_helper(void) { }
"""

EXT4_SUPER_C = """\
#include <linux/fs.h>

/**
 * struct ext4_sb_info - in-memory ext4 superblock study fixture
 * @s_blocks_count: Total number of filesystem blocks.
 * @s_inodes_count: Total number of inodes.
 * @label: Human-readable volume label.
 * @state: Two-bit filesystem state.
 * @active: Whether the filesystem is active.
 * @write_inode: Callback used to persist one inode.
 * @generation: Full generation counter.
 * @low: Low half of the promoted generation view.
 * @high: High half of the promoted generation view.
 * @features: Feature bitmap retained by DECLARE_BITMAP.
 * @tail: Configuration-defined flexible payload.
 *
 * This intentionally mixes ordinary fields, callbacks, bitfields, anonymous
 * aggregates, conditional source, declaration macros, and private data.
 */
struct ext4_sb_info {
\tunsigned long s_blocks_count;
\tunsigned int s_inodes_count;
\tchar label[16];
\tunsigned int state:2, active:1;
\tint (*write_inode)(struct inode *inode);
\tunion {
\t\tunsigned long generation;
\t\tstruct {
\t\t\tunsigned int low;
\t\t\tunsigned int high;
\t\t};
\t};
#ifdef CONFIG_EXT4_STUDY_FEATURES
\tDECLARE_BITMAP(features, 64);
#endif
\t/* private: */
\tDECLARE_FLEX_ARRAY(unsigned char, tail);
};

static int ext4_fill_super(struct super_block *sb, void *data, int silent)
{
\treturn 0;
}

int ext4_remount(struct super_block *sb, int *flags, char *data)
{
\treturn ext4_fill_super(sb, NULL, 0);
}
"""

FS_OPEN_C = """\
#include <linux/fs.h>

long do_sys_open(int dfd, const char __user *filename, int flags, umode_t mode)
{
\treturn 0;
}

SYSCALL_DEFINE3(open, const char __user *, filename, int, flags, umode_t, mode)
{
\treturn do_sys_open(AT_FDCWD, filename, flags, mode);
}

SYSCALL_DEFINE1(close, unsigned int, fd)
{
\treturn 0;
}
"""

FS_NAMEI_C = """\
#include <linux/fs.h>

static int link_path_walk(const char *name, struct nameidata *nd)
{
\treturn 0;
}

int path_lookup(const char *name)
{
\treturn link_path_walk(name, NULL);
}
"""

MM_PAGE_ALLOC_C = """\
struct page *__alloc_pages(gfp_t gfp, unsigned int order)
{
\treturn NULL;
}
EXPORT_SYMBOL_GPL(__alloc_pages);

static void free_one_page(struct zone *zone, struct page *page)
{
}
"""

NET_TCP_C = """\
int tcp_sendmsg(struct sock *sk, struct msghdr *msg, size_t size)
{
\treturn 0;
}

static int tcp_write_xmit(struct sock *sk)
{
\treturn 0;
}
"""

INCLUDE_FS_H = """\
#ifndef _LINUX_FS_H
#define _LINUX_FS_H

#define S_IRWXU 00700
#define MAY_EXEC(x) ((x) & 1)

typedef unsigned int fmode_t;

struct super_block {
\tunsigned long s_blocksize;
\tvoid *s_fs_info;
};

/**
 * study_mask_t - anonymous typedef-backed structure
 * @bits: Mask bits.
 */
typedef struct {
\tunsigned long bits[2];
} study_mask_t;

/**
 * union study_value - alternate scalar views
 * @signed_value: Signed interpretation.
 * @unsigned_value: Unsigned interpretation.
 */
union study_value {
\tint signed_value;
\tunsigned int unsigned_value;
};

enum rw_hint {
\tWRITE_LIFE_NOT_SET = 0,
\tWRITE_LIFE_NONE = 1,
};

extern int vfs_open(const struct path *path, struct file *file);

static const struct file_operations generic_ro_fops = {
\t.read = NULL,
};

#endif
"""

INTEL_DRIVER_C = """\
static int igb_probe(struct pci_dev *pdev)
{
\treturn 0;
}
"""

FILES: dict[str, str] = {
    "MAINTAINERS": MAINTAINERS,
    "Makefile": "VERSION = 6\nPATCHLEVEL = 12\nSUBLEVEL = 104\nEXTRAVERSION =\n",
    "Kconfig": "# dummy\n",
    "fs/ext4/inode.c": EXT4_INODE_C,
    "fs/ext4/super.c": EXT4_SUPER_C,
    "fs/ext4/Makefile": "obj-y := inode.o super.o\n",
    "fs/open.c": FS_OPEN_C,
    "fs/namei.c": FS_NAMEI_C,
    "fs/btrfs/super.c": "int btrfs_mount(void) { return 0; }\n",
    "mm/page_alloc.c": MM_PAGE_ALLOC_C,
    "mm/slab.c": "void *kmalloc(size_t n) { return NULL; }\n",
    "kernel/futex/core.c": "int futex_wait(void) { return 0; }\n",
    "net/ipv4/tcp.c": NET_TCP_C,
    "net/ipv4/udp.c": "int udp_sendmsg(void) { return 0; }\n",
    "net/core/dev.c": "int netif_rx(void) { return 0; }\n",
    "include/linux/fs.h": INCLUDE_FS_H,
    "include/linux/mm.h": "#define PAGE_SIZE 4096\n",
    "drivers/net/dummy.c": "static int dummy_init(void) { return 0; }\n",
    "drivers/net/ethernet/intel/igb/igb_main.c": INTEL_DRIVER_C,
    "arch/x86/mm/fault.c": "void do_page_fault(void) { }\n",
    "Documentation/filesystems/ext4/about.rst": "ext4 docs\n",
    "Documentation/mm/page_alloc.rst": "page allocator\n",
    "Documentation/locking/futex.rst": "futex locking documentation\n",
}


def make_mini_kernel(root: Path) -> Path:
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root
