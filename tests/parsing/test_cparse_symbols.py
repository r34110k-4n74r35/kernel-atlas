"""C symbol kinds, kernel macros, attributes, and parser input contracts."""

import pytest

from kernel_atlas import cparse

from .helpers import KINDS, parse, by_name


@pytest.mark.parametrize("value", [True, False, 1.5, "1024", 0, -1])
def test_public_parse_size_limit_requires_a_positive_integer(tmp_path, value):
    source = tmp_path / "sample.c"
    source.write_text("int sample(void) { return 0; }\n")

    with pytest.raises(ValueError, match="positive integer"):
        cparse.parse_source(b"int sample(void) {}", KINDS,
                            max_file_bytes=value)
    with pytest.raises(ValueError, match="positive integer"):
        cparse.parse_file(source, KINDS, max_file_bytes=value)


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


def test_symbols_have_repeatable_source_order_with_explicit_ties():
    source = """\
#define EARLY_VALUE 1
int source_variable;
typedef struct same_line { int member; } zeta_t, alpha_t;
static int later_function(void) { return EARLY_VALUE; }
"""
    expected = [
        (1, "macro", "EARLY_VALUE"),
        (2, "variable", "source_variable"),
        (3, "struct", "same_line"),
        (3, "typedef", "alpha_t"),
        (3, "typedef", "zeta_t"),
        (4, "function", "later_function"),
    ]

    for _ in range(8):
        assert [(symbol.start_line, symbol.kind, symbol.name)
                for symbol in parse(source)] == expected


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


def test_function_pointer_variable_is_not_a_prototype():
    kinds = KINDS | {"prototype"}
    syms = by_name(parse("""
        int real_prototype(void);
        static int (*handler_fp)(int sig) = default_handler;
    """, kinds))
    assert syms["real_prototype"].kind == "prototype"
    assert syms["handler_fp"].kind == "variable"


def test_kinds_filter_is_respected():
    syms = parse("int fn(void) {} struct s { int a; };", frozenset({"struct"}))
    assert [s.kind for s in syms] == ["struct"]


def test_garbage_does_not_raise():
    assert isinstance(parse("this is (((not { valid c at all"), list)


def test_iteration_macro_block_is_not_a_nested_function():
    syms = by_name(parse("""
        void outer(void)
        {
            int cpu;
            for_each_possible_cpu(cpu) {
                do_work(cpu);
            }
        }
    """, calls=True))
    assert set(syms) == {"outer"}
    assert syms["outer"].calls == ("do_work",)


def test_noinline_is_not_mistaken_for_inline():
    syms = by_name(parse("""
        static noinline int deliberately_slow(void) { return 0; }
        static __noinline int also_slow(void) { return 0; }
        static __always_inline int deliberately_fast(void) { return 0; }
    """))
    assert not syms["deliberately_slow"].is_inline
    assert not syms["also_slow"].is_inline
    assert syms["deliberately_fast"].is_inline


def test_leading_function_annotations_do_not_replace_the_real_name():
    sources = ["""
int __printf(2, 3) debugfs_change_name(const char *fmt, ...)
{
        int error = 0;
        if (helper()) { error++; }
        return error;
}
    """, """
__success __flag(BPF_F_TEST_STATE_FREQ)
int loop_inside_iter(const void *ctx)
{
        int sum = 0;
        while (helper()) { sum++; }
        return sum;
}
    """, """
module_init(driver_init)
module_exit(driver_exit)
static int actual_driver_fn(struct device *dev)
{
        int error = 0;
        if (helper()) { error++; }
        return error;
}
    """]
    syms = by_name([symbol for src in sources for symbol in parse(src)])
    assert {"debugfs_change_name", "loop_inside_iter", "actual_driver_fn"} \
        <= set(syms)
    assert not ({"__printf", "__flag", "module_exit"} & set(syms))
    assert syms["actual_driver_fn"].is_static


def test_source_export_scan_ignores_comments_and_macro_definitions():
    syms = by_name(parse(r'''
        int real_export(void) { return 0; }
        int trailing_export(void) { return 0; } EXPORT_SYMBOL_NS(trailing_export, TEST_NS);
        int commented_export(void) { return 0; }
        int macro_body_export(void) { return 0; }
        EXPORT_SYMBOL(real_export);
        /* EXPORT_SYMBOL(commented_export); */
#define EXPORT_WRAPPER() \
        EXPORT_SYMBOL(macro_body_export)
'''))
    assert syms["real_export"].is_exported
    assert syms["trailing_export"].is_exported
    assert not syms["commented_export"].is_exported
    assert not syms["macro_body_export"].is_exported


def test_unqualified_declaration_macros_are_file_scope_variables():
    syms = by_name(parse("""
        DEFINE_MUTEX(global_lock);
        LIST_HEAD(global_items);
        DECLARE_WORK(global_work, work_fn);
        DECLARE_BITMAP(global_bits, 64);
        static DECLARE_TRANSPORT_CLASS(raid_class, raid_attrs, NULL,
                                       raid_remove);
        DEFINE_PER_CPU(int, global_count);
    """))
    assert {"global_lock", "global_items", "global_work", "global_bits",
            "raid_class", "global_count"} <= set(syms)
    assert all(syms[name].kind == "variable" for name in (
        "global_lock", "global_items", "global_work", "global_bits",
        "raid_class", "global_count"))
    assert "NULL" not in syms and "raid_remove" not in syms


def test_common_object_declaration_macros_choose_the_object_argument():
    syms = by_name(parse("""
        DECLARE_RWSEM(global_sem);
        BLOCKING_NOTIFIER_HEAD(global_chain);
        DEFINE_SEMAPHORE(console_sem, 1);
        SIMPLE_DEV_PM_OPS(pm_ops, suspend_fn, resume_fn);
        DEFINE_STATIC_KEY_MAYBE(CONFIG_FEATURE, feature_enabled);
    """))
    assert {"global_sem", "global_chain", "console_sem", "pm_ops",
            "feature_enabled"} <= set(syms)
    assert "CONFIG_FEATURE" not in syms


def test_per_cpu_typedef_is_not_mistaken_for_the_object_name():
    syms = by_name(parse("""
        static DEFINE_PER_CPU(cpumask_var_t, load_balance_mask);
        DEFINE_PER_CPU(call_single_data_t, blk_cpu_csd);
    """))
    assert "load_balance_mask" in syms and "blk_cpu_csd" in syms
    assert "cpumask_var_t" not in syms and "call_single_data_t" not in syms


def test_function_annotations_do_not_become_variables():
    syms = by_name(parse("""
        __flag(BPF_F_ANY_ALIGNMENT)
        __naked void verifier_case(void) { }
    """))
    assert set(syms) == {"verifier_case"}


def test_function_returning_function_pointer_is_a_prototype():
    syms = by_name(parse("""
        int (*factory(void))(int);
        int (*callback)(int);
    """, KINDS | {"prototype"}))
    assert syms["factory"].kind == "prototype"
    assert syms["callback"].kind == "variable"


def test_gnu_inline_spelling_is_detected():
    syms = by_name(parse("static __inline__ int fast(void) { return 0; }"))
    assert syms["fast"].is_inline


def test_sysfs_attribute_macro_records_generated_object_not_callback_name():
    symbols = parse("""\
static int undock(void) { return 0; }
static void request(void) { undock(); }
static DEVICE_ATTR_WO(undock);
    """, calls=True)
    undock = [symbol for symbol in symbols if symbol.name == "undock"]

    assert len(undock) == 1
    assert undock[0].kind == "function"
    generated = next(symbol for symbol in symbols
                     if symbol.name == "dev_attr_undock")
    assert generated.kind == "variable"
    assert generated.is_static
    assert "undock" in next(symbol for symbol in symbols
                            if symbol.name == "request").calls


def test_one_line_macro_span_does_not_extend_past_end_of_file():
    symbol = by_name(parse("#define ONLY_LINE 1\n"))["ONLY_LINE"]
    assert (symbol.start_line, symbol.end_line) == (1, 1)


def test_custom_attribute_wrappers_do_not_invent_first_argument_objects():
    symbols = by_name(parse("""\
DEVICE_ATTR_SEC_REH_RO(bmc);
IIO_CONST_ATTR_FREQ_SCALE(channel, values);
    """))

    assert "dev_attr_bmc" not in symbols
    assert "iio_const_attr_channel" not in symbols


def test_fixed_name_iio_attribute_wrappers_record_the_generated_object():
    symbols = by_name(parse("""\
IIO_CONST_ATTR_SAMP_FREQ_AVAIL(values);
IIO_CONST_ATTR_INT_TIME_AVAIL(values);
    """))

    assert "iio_const_attr_sampling_frequency_available" in symbols
    assert "iio_const_attr_integration_time_available" in symbols
