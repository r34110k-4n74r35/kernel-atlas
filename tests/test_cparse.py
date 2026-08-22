from kernel_atlas import cparse

KINDS = frozenset(cparse.DEFAULT_KINDS)


def parse(src: str, kinds=KINDS, calls=False):
    return cparse.parse_source(src.encode(), kinds, calls)


def by_name(symbols):
    return {s.name: s for s in symbols}


def test_plain_functions_and_static_inline():
    syms = by_name(parse("""
        int public_fn(int a) { return a; }
        static int local_fn(void) { return 0; }
        static inline void inline_fn(void) { }
    """))
    assert syms["public_fn"].kind == "function"
    assert not syms["public_fn"].is_static
    assert syms["local_fn"].is_static
    assert syms["inline_fn"].is_inline


def test_kernel_attribute_macros_do_not_break_the_name():
    syms = by_name(parse("static int __init foo_init(struct bar *b, int n) { return 0; }"))
    assert "foo_init" in syms
    assert syms["foo_init"].signature.startswith("static int __init foo_init")


def test_syscall_define_becomes_sys_name():
    syms = by_name(parse("""
        SYSCALL_DEFINE3(open, const char __user *, filename, int, flags, umode_t, mode)
        {
            return do_sys_open(AT_FDCWD, filename, flags, mode);
        }
    """))
    assert "sys_open" in syms
    assert syms["sys_open"].kind == "syscall"
    # The body is a sibling node, so the span must still cover it.
    assert syms["sys_open"].end_line > syms["sys_open"].start_line


def test_syscall_define0():
    """A single argument makes this parse as a real function, unlike DEFINE3."""
    syms = by_name(parse("SYSCALL_DEFINE0(fork)\n{\n\treturn 0;\n}\n"))
    assert "sys_fork" in syms
    assert syms["sys_fork"].kind == "syscall"
    assert "fork" not in syms


def test_compat_syscall_is_a_distinct_symbol():
    """COMPAT_SYSCALL_DEFINE4(openat,...) is compat_sys_openat, not sys_openat."""
    syms = by_name(parse("""
        SYSCALL_DEFINE4(openat, int, dfd, const char __user *, filename,
                        int, flags, umode_t, mode)
        {
            return do_sys_open(dfd, filename, flags, mode);
        }
        COMPAT_SYSCALL_DEFINE4(openat, int, dfd, const char __user *, filename,
                               int, flags, umode_t, mode)
        {
            return do_sys_open(dfd, filename, flags, mode);
        }
    """))
    assert syms["sys_openat"].kind == "syscall"
    assert syms["compat_sys_openat"].kind == "syscall"


def test_compat_syscall_define0():
    syms = by_name(parse("COMPAT_SYSCALL_DEFINE0(fork)\n{\n\treturn 0;\n}\n"))
    assert "compat_sys_fork" in syms


def test_export_symbol_marks_the_function():
    syms = by_name(parse("""
        int ext4_bmap(void) { return 0; }
        int quiet_fn(void) { return 0; }
        EXPORT_SYMBOL(ext4_bmap);
    """))
    assert syms["ext4_bmap"].is_exported
    assert not syms["quiet_fn"].is_exported


def test_export_symbol_gpl_variant():
    syms = by_name(parse("""
        int a_fn(void) { return 0; }
        EXPORT_SYMBOL_GPL(a_fn);
    """))
    assert syms["a_fn"].is_exported


def test_types_and_macros():
    syms = by_name(parse("""
        struct super_block { int a; void *b; };
        union u_thing { int a; };
        enum colours { RED, GREEN };
        typedef unsigned int fmode_t;
        #define S_IRWXU 00700
        #define MAY_EXEC(x) ((x) & 1)
    """))
    assert syms["super_block"].kind == "struct"
    assert "2 members" in syms["super_block"].signature
    assert syms["u_thing"].kind == "union"
    assert syms["colours"].kind == "enum"
    assert syms["fmode_t"].kind == "typedef"
    assert syms["S_IRWXU"].kind == "macro"
    assert syms["MAY_EXEC"].kind == "macro"


def test_file_scope_variable_but_not_locals():
    syms = by_name(parse("""
        static const struct file_operations ext4_fops = { .open = NULL };
        int some_fn(void) { int local_only = 1; return local_only; }
    """))
    assert syms["ext4_fops"].kind == "variable"
    assert "local_only" not in syms


def test_trailing_attribute_macro_is_not_a_variable_name():
    """`struct sem { ... } ____cacheline_aligned_in_smp;` declares no variable."""
    syms = by_name(parse("struct sem {\n\tint semval;\n} ____cacheline_aligned_in_smp;"))
    assert "____cacheline_aligned_in_smp" not in syms
    assert syms["sem"].kind == "struct"


def test_declaration_macros_yield_the_declared_name():
    syms = by_name(parse("""
        static DECLARE_WORK(free_ipc_work, free_ipc);
        static DEFINE_MUTEX(foo_lock);
        static LIST_HEAD(my_list);
        static DECLARE_BITMAP(found_map, MAX_UNITS);
    """))
    assert syms["free_ipc_work"].kind == "variable"
    assert syms["foo_lock"].kind == "variable"
    assert syms["my_list"].kind == "variable"
    assert syms["found_map"].kind == "variable"


def test_per_cpu_macros_use_the_second_argument():
    """DEFINE_PER_CPU(type, name) is name-second, unlike DECLARE_WORK."""
    syms = by_name(parse("""
        static DEFINE_PER_CPU(int, cpu_number);
        static DEFINE_PER_CPU(u64, cpu_ticks);
    """))
    assert "cpu_number" in syms
    assert "cpu_ticks" in syms
    assert "int" not in syms and "u64" not in syms


def test_export_per_cpu_symbol_is_detected():
    syms = by_name(parse("""
        static DEFINE_PER_CPU(int, cpu_number);
        EXPORT_PER_CPU_SYMBOL(cpu_number);
    """))
    assert syms["cpu_number"].is_exported


def test_misparsed_type_keywords_never_become_symbols():
    """`STATIC int INIT fn(...)` with unexpanded macros must not produce a
    variable literally named 'int'."""
    syms = by_name(parse("STATIC int INIT get_next_block(struct bd *b)\n{\n}\n"))
    assert "int" not in syms and "unsigned" not in syms


def test_shouting_case_prototypes_are_macro_artifacts():
    kinds = KINDS | {"prototype"}
    syms = by_name(parse(
        "DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);\n"
        "int real_fn(void);\n", kinds))
    assert "DEFINE_PER_CPU_SHARED_ALIGNED" not in syms
    assert syms["real_fn"].kind == "prototype"


def test_no_symbol_ever_has_an_empty_name():
    syms = parse("""
        static DECLARE_WORK(w, fn);
        struct s { int a; } __read_mostly;
        int ok(void) { return 0; }
    """)
    assert all(s.name.strip() for s in syms)


def test_function_inside_ifdef_is_found():
    syms = by_name(parse("""
        #ifdef CONFIG_SOMETHING
        static int guarded(void) { return 1; }
        #endif
    """))
    assert "guarded" in syms


def test_locals_inside_an_ifdef_in_a_function_are_not_file_scope():
    """#ifdef reaches into function bodies too; those declarations are locals."""
    syms = by_name(parse("""
        static int top_level_var;
        long ksys_shmdt(char __user *shmaddr)
        {
        #ifdef CONFIG_MMU
            loff_t size = 0;
            struct file *file;
        #endif
            return 0;
        }
    """))
    assert "top_level_var" in syms
    assert "file" not in syms
    assert "size" not in syms


def test_file_scope_var_inside_ifdef_is_still_found():
    syms = by_name(parse("""
        #ifdef CONFIG_PROC_FS
        static struct proc_ops my_ops = { .proc_open = NULL };
        #endif
    """))
    assert syms["my_ops"].kind == "variable"


def test_prototypes_are_opt_in():
    src = "extern int vfs_open(const struct path *p);"
    assert "vfs_open" not in by_name(parse(src))
    with_proto = by_name(parse(src, KINDS | {"prototype"}))
    assert with_proto["vfs_open"].kind == "prototype"


def test_pointer_and_array_declarators():
    syms = by_name(parse("struct page *__alloc_pages(gfp_t gfp) { return 0; }"))
    assert "__alloc_pages" in syms


def test_syscall_bodies_yield_call_edges():
    """The body of SYSCALL_DEFINEn is a sibling compound_statement; call
    collection must reach into it, not just into real function definitions."""
    syms = by_name(parse("""
        SYSCALL_DEFINE3(open, const char __user *, filename, int, flags,
                        umode_t, mode)
        {
            if (force_o_largefile())
                flags |= O_LARGEFILE;
            return do_sys_open(AT_FDCWD, filename, flags, mode);
        }
    """, calls=True))
    assert "do_sys_open" in syms["sys_open"].calls
    assert "force_o_largefile" in syms["sys_open"].calls


def test_function_pointer_variable_is_not_a_prototype():
    kinds = KINDS | {"prototype"}
    syms = by_name(parse("""
        int real_prototype(void);
        static int (*handler_fp)(int sig) = default_handler;
    """, kinds))
    assert syms["real_prototype"].kind == "prototype"
    assert syms["handler_fp"].kind == "variable"


def test_calls_are_collected_only_when_asked():
    src = "int outer(void) { return inner_one() + inner_two(); }"
    assert parse(src)[0].calls == ()
    calls = parse(src, calls=True)[0].calls
    assert set(calls) == {"inner_one", "inner_two"}


def test_kinds_filter_is_respected():
    syms = parse("int fn(void) {} struct s { int a; };", frozenset({"struct"}))
    assert [s.kind for s in syms] == ["struct"]


def test_garbage_does_not_raise():
    assert isinstance(parse("this is (((not { valid c at all"), list)
