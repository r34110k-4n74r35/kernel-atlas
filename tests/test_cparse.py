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


def test_call_tuples_have_repeatable_source_order():
    source = """\
int caller(void (*later_hook)(void), void (*early_hook)(void))
{
    first_call();
    later_hook();
    middle_call();
    early_hook();
    first_call();
    return final_call();
}
"""
    for _ in range(8):
        caller = by_name(parse(source, calls=True))["caller"]
        assert caller.calls == (
            "first_call", "later_hook", "middle_call", "early_hook",
            "final_call",
        )
        assert caller.indirect_calls == ("later_hook", "early_hook")


def test_calls_through_parameters_and_local_objects_are_marked_indirect():
    syms = by_name(parse("""
        int callback(void) { return 1; }

        int through_parameter(int (*callback)(void))
        {
            return callback();
        }

        int through_local(void)
        {
            int (*callback)(void) = 0;
            return callback();
        }

        int before_local(void)
        {
            int value = callback();
            int (*callback)(void) = 0;
            return value;
        }

        int through_prototype(void)
        {
            int callback(void);
            return callback();
        }
    """, calls=True))

    assert syms["through_parameter"].indirect_calls == ("callback",)
    assert syms["through_local"].indirect_calls == ("callback",)
    assert syms["before_local"].indirect_calls == ()
    assert syms["through_prototype"].indirect_calls == ()


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


def test_error_recovery_does_not_hide_file_scope_symbols():
    """An initializer with guarded nested braces makes tree-sitter put the
    remaining translation unit in ERROR, but its direct children remain valid
    file-scope declarations and macro definitions."""
    kinds = KINDS | {"prototype"}
    syms = by_name(parse("""\
static DEFINE_PER_CPU(struct pool, state) = {
#ifdef CONFIG_A
    .values = { 1 },
#else
    .values = { 2 },
#endif
};
int recovered_global;
int recovered_proto(void);
SYSCALL_DEFINE1(recovered, int, value)
{
    return helper(value);
}
int recovered_api(void) { return 0; }
EXPORT_SYMBOL(recovered_api);
    """, kinds, calls=True))
    assert syms["state"].kind == "variable"
    assert syms["recovered_global"].kind == "variable"
    assert syms["recovered_proto"].kind == "prototype"
    assert syms["sys_recovered"].calls == ("helper",)
    assert syms["recovered_api"].is_exported


def test_error_recovered_export_declarations_mark_symbols_not_variables():
    syms = parse("""
        int first_api(void) { return 0; }
        int second_api(void) { return 0; }
        module_init(driver_init)
        module_exit(driver_exit)
        EXPORT_SYMBOL(first_api);
        EXPORT_SYMBOL_GPL(second_api);
    """)
    functions = {s.name: s for s in syms if s.kind == "function"}
    variables = {s.name for s in syms if s.kind == "variable"}
    assert functions["first_api"].is_exported
    assert functions["second_api"].is_exported
    assert "first_api" not in variables and "second_api" not in variables


def test_recovered_outer_function_does_not_swallow_later_definitions():
    """The N() switch idiom can make tree-sitter extend the first function to
    EOF and nest later source-level functions inside its recovered body."""
    syms = parse(r'''
const char *netdev_cmd_to_name(enum netdev_cmd cmd)
{
#define N(val)                         \
        case NETDEV_##val:             \
                return "NETDEV_" __stringify(val);
        switch (cmd) {
        N(UP) N(DOWN) N(REBOOT) N(CHANGE) N(REGISTER) N(UNREGISTER)
        N(CHANGEMTU) N(CHANGEADDR) N(GOING_DOWN) N(CHANGENAME)
        N(BONDING_FAILOVER) N(PRE_UP) N(PRE_TYPE_CHANGE)
        N(CHANGEUPPER) N(RESEND_IGMP) N(PRECHANGEMTU)
        N(UDP_TUNNEL_PUSH_INFO) N(UDP_TUNNEL_DROP_INFO)
        N(CVLAN_FILTER_PUSH_INFO) N(CVLAN_FILTER_DROP_INFO)
        N(SVLAN_FILTER_PUSH_INFO) N(SVLAN_FILTER_DROP_INFO)
        N(PRE_CHANGEADDR) N(OFFLOAD_XSTATS_ENABLE)
        N(OFFLOAD_XSTATS_REPORT_USED) N(XDP_FEAT_CHANGE)
        }
#undef N
        return "UNKNOWN";
}
EXPORT_SYMBOL_GPL(netdev_cmd_to_name);
struct recovered_tag { int value; };
static int recovered_global;
static int recovery_sacrifice(void) { return discarded_call(); }
static __always_inline struct result *later_helper(void)
{ return later_call(); }
static int trigger(void)
{
        int local_only = 0;
        for_each_net(net) { loop_call(); }
again:
        for_each_net(net) { loop_call(); }
        goto again;
}
EXPORT_SYMBOL(trigger);
''', KINDS | {"prototype"}, calls=True)
    functions = {s.name: s for s in syms if s.kind == "function"}
    outer = functions["netdev_cmd_to_name"]
    assert outer.is_exported and outer.end_line < functions["later_helper"].start_line
    assert "later_call" not in outer.calls and "loop_call" not in outer.calls
    assert functions["later_helper"].calls == ("later_call",)
    assert functions["later_helper"].is_static
    assert functions["later_helper"].is_inline
    assert functions["trigger"].is_exported
    assert "net" not in functions and "N" not in functions
    all_symbols = by_name(syms)
    assert all_symbols["recovered_tag"].kind == "struct"
    assert all_symbols["recovered_global"].kind == "variable"
    assert "local_only" not in all_symbols


def test_split_trailing_attribute_keeps_exported_variable():
    syms = by_name(parse("""
        struct kernel_mapping kernel_map __ro_after_init;
        EXPORT_SYMBOL(kernel_map);
    """))
    assert syms["kernel_map"].kind == "variable"
    assert syms["kernel_map"].is_exported
    assert "__ro_after_init" not in syms


def test_exported_alias_prototype_is_marked_exported():
    syms = by_name(parse("""
        void *__hwasan_memset(void *p, int c, long n) __alias(__asan_memset);
        EXPORT_SYMBOL(__hwasan_memset);
    """))
    assert syms["__hwasan_memset"].kind == "function"
    assert syms["__hwasan_memset"].is_exported
    function_only = by_name(parse("""
        void *__hwasan_memset(void *p, int c, long n) __alias(__asan_memset);
        EXPORT_SYMBOL(__hwasan_memset);
    """, frozenset({"function"})))
    assert function_only["__hwasan_memset"].is_exported


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


def test_semicolonless_module_macros_do_not_truncate_the_next_function():
    syms = by_name(parse("""\
module_init(driver_init)
module_exit(driver_exit)

static int efs_validate_vh(struct volume_header *vh)
{
        int error = 0;
        if (first_check(vh)) {
                error = -1;
        }
        final_check(vh);
        return error;
}
    """, calls=True))
    symbol = syms["efs_validate_vh"]
    assert symbol.signature == \
        "static int efs_validate_vh(struct volume_header *vh)"
    assert symbol.end_line == 12
    # This call occurs after the inner block tree-sitter had mistaken for the
    # function body, so it specifically guards the recovered call range.
    assert "final_check" in symbol.calls


def test_recovery_macros_before_enums_are_not_functions():
    syms = by_name(parse("""\
BTF_ID_LIST(ids)
BTF_ID(struct, module)
enum object_ids { FIRST_ID, LAST_ID };
static int later(void) { return 0; }
    """))
    assert syms["later"].kind == "function"
    assert "BTF_ID" not in syms and "BTF_ID_LIST" not in syms


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


def test_initializer_recovery_does_not_emit_values_as_declarators():
    """Preprocessor directives in a large initializer can make later values
    look like direct declarator fields."""
    src = """const struct operand operands[] = {
        { 0, 0, NULL, NULL, 0 },
    #define SLOT_B 1
        { SLOT_B, 16, NULL, NULL, FLAG },
    #define SLOT_C 2
        { SLOT_C, 16, insert_c, extract_c, FLAG },
    #define SLOT_D 3
        { SLOT_D, 16, NULL, NULL, FLAG },
    };"""
    variables = [s.name for s in parse(src) if s.kind == "variable"]
    assert all(name not in variables for name in (
        "NULL", "FLAG", "insert_c", "extract_c", "SLOT_B", "SLOT_C",
        "SLOT_D"))


def test_attributed_initializer_recovers_the_real_exported_object():
    syms = by_name(parse("""
        __visible const u32 crypto_ft_tab[4][256] ____cacheline_aligned = {
                { 1, 2, 3 },
        };
        struct task_struct init_task __aligned(L1_CACHE_BYTES) = {
                .flags = 0,
        };
        extern const u32 crypto_sm4_fk[4] __alias(fk);
        EXPORT_SYMBOL(crypto_ft_tab);
        EXPORT_SYMBOL(init_task);
        EXPORT_SYMBOL(crypto_sm4_fk);
    """))
    for name in ("crypto_ft_tab", "init_task", "crypto_sm4_fk"):
        assert syms[name].kind == "variable"
        assert syms[name].is_exported
    assert "____cacheline_aligned" not in syms


def test_export_guides_recovery_when_root_error_hides_direct_functions():
    syms = by_name(parse("""\
ATOMIC64_OPS(add, +=)
s64 generic_atomic64_cmpxchg(atomic64_t *v, s64 old, s64 new)
{
        s64 value = old;
        if (value == old)
                value = new;
        return value;
}
EXPORT_SYMBOL(generic_atomic64_cmpxchg);

u64 siphash_1u32(const u32 first, const siphash_key_t *key)
{
        PREAMBLE(4)
        b |= first;
        POSTAMBLE
}
EXPORT_SYMBOL(siphash_1u32);
    """))
    for name in ("generic_atomic64_cmpxchg", "siphash_1u32"):
        assert syms[name].kind == "function"
        assert syms[name].is_exported


def test_export_guided_function_handles_preprocessor_alternate_braces():
    syms = by_name(parse("""\
int rproc_elf_sanity_check(struct rproc *rproc, const struct firmware *fw)
{
#ifdef LITTLE_ENDIAN
        if (is_little(fw)) {
#else
        if (is_big(fw)) {
#endif
                return 0;
        }
        return -1;
}
EXPORT_SYMBOL(rproc_elf_sanity_check);
    """))
    assert syms["rproc_elf_sanity_check"].kind == "function"
    assert syms["rproc_elf_sanity_check"].is_exported
    assert syms["rproc_elf_sanity_check"].end_line == 11


def test_export_guides_recovery_of_multiline_object_initializer():
    syms = by_name(parse("""\
const struct snd_soc_dai_ops rt5682_aif1_dai_ops = {
        .hw_params = rt5682_hw_params,
        .set_fmt = rt5682_set_dai_fmt,
};
EXPORT_SYMBOL_GPL(rt5682_aif1_dai_ops);
    """))
    assert syms["rt5682_aif1_dai_ops"].kind == "variable"
    assert syms["rt5682_aif1_dai_ops"].is_exported
    assert syms["rt5682_aif1_dai_ops"].end_line == 4


def test_export_guides_recovery_of_split_and_macro_suffixed_objects():
    syms = by_name(parse("""\
unsigned long empty_zero_page[PAGE_SIZE / sizeof(unsigned long)]
                        __page_aligned_bss;
EXPORT_SYMBOL(empty_zero_page);
enum reboot_mode reboot_mode DEFAULT_REBOOT_MODE;
EXPORT_SYMBOL_GPL(reboot_mode);
    """))
    assert syms["empty_zero_page"].kind == "variable"
    assert syms["empty_zero_page"].is_exported
    assert syms["empty_zero_page"].end_line == 2
    assert syms["reboot_mode"].kind == "variable"
    assert syms["reboot_mode"].is_exported


def test_export_guides_functions_with_return_type_on_previous_line():
    syms = by_name(parse("""\
void __acquires(&lock->lock)
__libeth_xdpsq_lock(struct libeth_xdpsq_lock *lock)
{
        spin_lock(&lock->lock);
}
EXPORT_SYMBOL_GPL(__libeth_xdpsq_lock);

struct mt76_phy *
mt76_alloc_phy(struct mt76_dev *dev)
{
        return alloc_phy(dev);
}
EXPORT_SYMBOL_GPL(mt76_alloc_phy);

__be32
svc_generic_init_request(struct svc_rqst *rqstp)
{
        return rpc_success;
}
EXPORT_SYMBOL_GPL(svc_generic_init_request);
    """, calls=True))
    lock = syms["__libeth_xdpsq_lock"]
    assert lock.kind == "function" and lock.is_exported
    assert lock.start_line == 1 and lock.end_line == 5
    assert lock.signature.startswith("void __acquires")
    assert "spin_lock" in lock.calls
    for name, call in (("mt76_alloc_phy", "alloc_phy"),
                       ("svc_generic_init_request", None)):
        assert syms[name].kind == "function"
        assert syms[name].is_exported
        if call is not None:
            assert call in syms[name].calls


def test_export_guides_conditional_alternate_function_header():
    syms = by_name(parse("""\
#if DEBUG_QMGR
int qmgr_request_queue(unsigned int queue, unsigned int len)
#else
int __qmgr_request_queue(unsigned int queue, unsigned int len)
#endif
{
        helper(queue);
        return 0;
}

void unrelated(void) { }

#if DEBUG_QMGR
EXPORT_SYMBOL(qmgr_request_queue);
#else
EXPORT_SYMBOL(__qmgr_request_queue);
#endif
    """, calls=True))
    request = syms["qmgr_request_queue"]
    assert request.kind == "function" and request.is_exported
    assert request.start_line == 2 and request.end_line == 9
    assert "helper" in request.calls
    alternate = syms["__qmgr_request_queue"]
    assert alternate.kind == "function" and alternate.is_exported
    assert "#endif" not in alternate.signature


def test_split_attribute_initializer_keeps_exported_variable():
    syms = by_name(parse("""\
int percpu_counter_batch __read_mostly = 32;
EXPORT_SYMBOL(percpu_counter_batch);
volatile unsigned long latent_entropy __latent_entropy;
EXPORT_SYMBOL(latent_entropy);
struct pglist_data __refdata contig_page_data;
int threads_per_core, threads_per_subcore, threads_shift __read_mostly;
    """))
    assert syms["percpu_counter_batch"].kind == "variable"
    assert syms["percpu_counter_batch"].is_exported
    assert syms["latent_entropy"].kind == "variable"
    assert syms["latent_entropy"].is_exported
    assert syms["contig_page_data"].kind == "variable"
    assert {"threads_per_core", "threads_per_subcore", "threads_shift"} \
        <= set(syms)
    assert "__latent_entropy" not in syms


def test_exported_variables_with_recovered_type_and_attribute_declarators():
    syms = by_name(parse("""\
__iomem void *rt_sysc_membase;
EXPORT_SYMBOL_GPL(rt_sysc_membase);
struct uv_info __bootdata_preserved(uv_info);
EXPORT_SYMBOL(uv_info);
u64 sme_me_mask __section(".data") = 0;
SYM_PIC_ALIAS(sme_me_mask);
u64 unrelated __section(".data") = 0;
EXPORT_SYMBOL(sme_me_mask);
    """))
    for name, line in (("rt_sysc_membase", 1), ("uv_info", 3),
                       ("sme_me_mask", 5)):
        assert syms[name].kind == "variable"
        assert syms[name].start_line == line
        assert syms[name].end_line == line
        assert syms[name].is_exported
    assert "__bootdata_preserved" not in syms
    assert "__section" not in syms


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


def test_multiple_typedef_declarators_are_all_indexed():
    syms = by_name(parse("typedef unsigned long first_t, second_t;"))
    assert syms["first_t"].kind == "typedef"
    assert syms["second_t"].kind == "typedef"


def test_struct_member_count_counts_comma_declarators():
    syms = by_name(parse("struct pair { int left, right; long generation; };"))
    assert "3 members" in syms["pair"].signature
    assert [member.name for member in syms["pair"].members] == [
        "left", "right", "generation",
    ]


def test_struct_details_preserve_docs_shapes_nesting_and_conditions():
    syms = by_name(parse("""\
/**
 * struct study_record - mixed structure fixture
 * @count: Number of records.
 * @name: Human-readable name.
 * @flags: State flags.
 * @callback: Invoked for each record, possibly across
 *   multiple source lines.
 * @value: Nested value.
 * @bits: Feature bitmap.
 *
 * Long-form notes remain attached to the aggregate.
 */
struct study_record {
    unsigned int count;
    char name[16];
    unsigned int flags:3, ready:1;
    int (*callback)(void *context, int value);
    union {
        long value;
        struct {
            unsigned int low;
            unsigned int high;
        };
    };
#ifdef CONFIG_STUDY
    DECLARE_BITMAP(bits, 64);
#endif
    /* private: */
    DECLARE_FLEX_ARRAY(unsigned char, payload);
};
"""))
    structure = syms["study_record"]
    assert structure.summary == "mixed structure fixture"
    assert "Long-form notes" in structure.description
    roots = [member for member in structure.members
             if member.parent_index is None]
    assert len(roots) == 8
    assert [member.name for member in roots[:5]] == [
        "count", "name", "flags", "ready", "callback",
    ]
    assert roots[1].array_dimensions == ("16",)
    assert roots[2].bit_width == "3"
    assert roots[3].bit_width == "1"
    assert roots[4].kind == "function_pointer"
    assert "(*)(void *context, int value)" in roots[4].type_text
    assert "multiple source lines" in roots[4].description

    anonymous_union = roots[5]
    assert anonymous_union.kind == "union" and anonymous_union.is_anonymous
    union_children = [
        member for member in structure.members
        if member.parent_index == structure.members.index(anonymous_union)
    ]
    assert [member.name for member in union_children] == ["value", None]
    assert roots[6].generated_by == "DECLARE_BITMAP"
    assert roots[6].array_dimensions == ("BITS_TO_LONGS(64)",)
    assert roots[6].conditions == ("#ifdef CONFIG_STUDY",)
    assert roots[7].name == "payload"
    assert roots[7].array_dimensions == ("",)
    assert roots[7].visibility == "private"


def test_struct_direct_count_excludes_nested_tag_only_declarations():
    syms = by_name(parse("""\
struct outer {
    struct inner { int hidden_from_outer_count; };
    int direct;
    union { int promoted; };
};
"""))
    outer = syms["outer"]
    assert "2 members" in outer.signature
    assert [member.name for member in outer.members
            if member.parent_index is None] == ["direct", None]
    assert syms["inner"].members[0].name == "hidden_from_outer_count"


def test_anonymous_typedef_structure_is_queryable_and_packed_is_not_a_tag():
    symbols = parse("""\
/**
 * packet_t - compact packet
 * @length: Packet length.
 */
typedef struct __packed {
    unsigned short length;
} packet_t;
""")
    assert "__packed" not in {symbol.name for symbol in symbols}
    packet = next(symbol for symbol in symbols
                  if symbol.name == "packet_t" and symbol.kind == "struct")
    assert packet.kind == "struct"
    assert packet.is_anonymous
    assert packet.aliases == ("packet_t",)
    assert packet.members[0].description == "Packet length."


def test_mismatched_kernel_doc_is_not_attached_to_structure():
    syms = by_name(parse("""\
/**
 * struct another - This belongs somewhere else.
 * @value: Wrong description.
 */
struct actual { int value; };
"""))
    actual = syms["actual"]
    assert actual.summary is None
    assert actual.members[0].description is None


def test_sysfs_function_alternatives_keep_every_callback_and_documentation():
    structure = by_name(parse("""\
/**
 * struct device_attribute - sysfs callback alternatives
 * @attr: Attribute metadata.
 * @show: Mutable show callback.
 * @show_const: Const show callback.
 */
struct device_attribute {
    int attr;
    __SYSFS_FUNCTION_ALTERNATIVE(
        int (*show)(void *dev);
        int (*show_const)(const void *dev);
    );
};
"""))["device_attribute"]
    roots = [member for member in structure.members
             if member.parent_index is None]
    assert len(roots) == 2
    assert roots[1].kind == "macro"
    assert roots[1].generated_by == "__SYSFS_FUNCTION_ALTERNATIVE"
    assert [member.name for member in structure.members] == [
        "attr", None, "show", "show_const",
    ]
    assert all(member.description for member in structure.members
               if member.name is not None)
    assert structure.parse_complete


def test_cacheline_group_macros_materialize_real_markers_and_aligned_padding():
    structure = by_name(parse("""\
struct cache_study {
    __cacheline_group_begin(hot) ____cacheline_aligned;
    int value;
    __cacheline_group_end_aligned(hot, 64);
};
"""))["cache_study"]
    assert [member.name for member in structure.members] == [
        "__cacheline_group_begin__hot", "value",
        "__cacheline_group_end__hot", "__cacheline_group_pad__hot",
    ]
    assert structure.members[0].type_text == "__u8 [0]"
    assert structure.members[0].array_dimensions == ("0",)
    assert structure.members[2].generated_by == \
        "__cacheline_group_end_aligned"
    assert structure.parse_complete


def test_struct_group_tag_creates_hierarchical_member_and_queryable_tag():
    symbols = parse("""\
struct outer {
    /*
     * struct generated_pair - reusable generated pair
     * @left: Left value.
     * @right: Right value.
     */
    __struct_group(generated_pair, pair, __packed,
        int left;
        int right;
    );
};
""")
    tagged = next(symbol for symbol in symbols
                  if symbol.name == "generated_pair"
                  and symbol.kind == "struct")
    assert tagged.summary == "reusable generated pair"
    assert tagged.signature.startswith("struct generated_pair __packed")
    assert [member.name for member in tagged.members] == ["left", "right"]
    assert all(member.description_source == "source-comment"
               for member in tagged.members)
    outer = next(symbol for symbol in symbols
                 if symbol.name == "outer" and symbol.kind == "struct")
    assert outer.members[0].kind == "struct_group"
    assert outer.members[0].name == "pair"
    assert [member.parent_index for member in outer.members] == [None, 0, 0]


def test_annotated_members_recover_real_names_shapes_and_qualifiers():
    structure = by_name(parse("""\
struct annotated {
    void *__ctx[] __aligned(16);
    int __must_check (*destroy)(void);
    __printf(2, 3) void (*fail)(const char *format, ...);
    int (*scan)(const char *input, ...) __scanf(1, 2);
    raw_spinlock_t __private lock ____cacheline_aligned_in_smp;
    unsigned int const __user *pins;
    char signature[4]
        ACPI_NONSTRING;
    void *request_ctx[] CRYPTO_MINALIGN_ATTR;
    unsigned char options[] __aligned_largest __counted_by(option_len);
};
"""))["annotated"]
    assert [member.name for member in structure.members] == [
        "__ctx", "destroy", "fail", "scan", "lock", "pins", "signature",
        "request_ctx", "options",
    ]
    assert structure.members[0].array_dimensions == ("",)
    assert "__aligned(16)" in structure.members[0].type_text
    assert structure.members[1].kind == "function_pointer"
    assert "__must_check" in structure.members[1].type_text
    assert structure.members[2].kind == "function_pointer"
    assert "__printf(2, 3)" in structure.members[2].type_text
    assert structure.members[3].kind == "function_pointer"
    assert "__scanf(1, 2)" in structure.members[3].type_text
    assert "__private" in structure.members[4].type_text
    assert "const __user" in structure.members[5].type_text
    assert "ACPI_NONSTRING" in structure.members[6].type_text
    assert "CRYPTO_MINALIGN_ATTR" in structure.members[7].type_text
    assert "__counted_by(option_len)" in structure.members[8].type_text
    assert structure.parse_complete


def test_nested_aggregate_attributes_do_not_leak_into_child_fields():
    structure = by_name(parse("""\
struct outer {
    union {
        int x;
        long y;
    } item __aligned(8);
};
"""))["outer"]
    assert [member.name for member in structure.members] == ["item", "x", "y"]
    assert "__aligned(8)" in structure.members[0].type_text
    assert structure.members[0].declaration.startswith("union {")
    assert structure.members[1].type_text == "int"
    assert structure.members[1].declaration == "int x;"
    assert structure.members[2].type_text == "long"
    assert structure.members[2].declaration == "long y;"
    assert structure.parse_complete


def test_kernel_abi_suffix_macros_preserve_anonymous_unions_and_attributes():
    structure = by_name(parse("""\
struct bpmp_message {
    union {
        int request;
        long response;
    } BPMP_UNION_ANON;
} BPMP_ABI_PACKED;
"""))["bpmp_message"]
    assert "BPMP_ABI_PACKED" in structure.signature
    assert [member.name for member in structure.members] == [
        None, "request", "response",
    ]
    assert structure.members[0].kind == "union"
    assert structure.members[0].is_anonymous
    assert "BPMP_UNION_ANON" in structure.members[0].type_text
    assert structure.parse_complete

    empty = by_name(parse("""\
#if ARCH_HAS_BPMP
/**
 * @brief Empty BPMP request.
 */
struct bpmp_empty_request {
    BPMP_ABI_EMPTY
} BPMP_ABI_PACKED;
#endif
"""))["bpmp_empty_request"]
    assert empty.signature == (
        "struct bpmp_empty_request BPMP_ABI_PACKED { 1 member }")
    assert empty.summary == "Empty BPMP request."
    assert [(member.name, member.kind, member.generated_by)
            for member in empty.members] == [
        ("empty", "macro", "BPMP_ABI_EMPTY"),
    ]
    assert empty.members[0].conditions == (
        "#if ARCH_HAS_BPMP", "#ifdef NO_GCC_EXTENSIONS",
    )
    assert empty.parse_complete

    source = b"""\
#if ARCH_HAS_BPMP
struct fallback_only {
    BPMP_ABI_EMPTY
} BPMP_ABI_PACKED;
#endif
"""
    cparse._ensure_parser()
    root = cparse._PARSER.parse(source).root_node
    recovered = cparse._recover_bpmp_empty_aggregates(source, root)
    assert [symbol.name for symbol in recovered] == ["fallback_only"]
    assert recovered[0].conditions == ("#if ARCH_HAS_BPMP",)
    assert recovered[0].members[0].conditions == (
        "#if ARCH_HAS_BPMP", "#ifdef NO_GCC_EXTENSIONS",
    )


def test_bpmp_empty_fallback_rejects_comments_macros_and_local_types():
    names = set(by_name(parse(r"""
/*
struct comment_fake {
    BPMP_ABI_EMPTY
} BPMP_ABI_PACKED;
*/
#define LOCAL_FAKE \
struct define_fake { \
    BPMP_ABI_EMPTY \
} BPMP_ABI_PACKED;
void function(void) {
struct local_fake {
    BPMP_ABI_EMPTY
} BPMP_ABI_PACKED;
}
""")))
    assert not {"comment_fake", "define_fake", "local_fake"} & names


def test_nested_callback_arguments_stay_in_the_callback_description():
    structure = by_name(parse("""\
/**
 * struct callback_ops - callback documentation
 * @run: Execute one request.
 *     @context: Caller context.
 *     @index: Request index.
 *     Return: zero on success.
 */
struct callback_ops {
    int (*run)(void *context, int index);
};
"""))["callback_ops"]
    assert "@context" in structure.members[0].description
    assert "Return: zero" in structure.members[0].description
    assert structure.unmatched_member_docs == ()
    assert structure.parse_complete


def test_ordinary_structured_comment_can_document_an_aggregate():
    structure = by_name(parse("""\
/*
 * struct source_evidence - ordinary source-comment summary
 * @first,@second: Values documented together.
 */
struct source_evidence { int first; int second; };
"""))["source_evidence"]
    assert structure.summary == "ordinary source-comment summary"
    assert [member.description for member in structure.members] == [
        "Values documented together.", "Values documented together.",
    ]
    assert all(member.description_source == "source-comment"
               for member in structure.members)


def test_anonymous_tag_member_is_retained_without_inventing_a_field_name():
    outer = by_name(parse("""\
struct promoted { int value; };
/**
 * struct outer_promoted - extension fixture
 * @promoted: Promoted tagged member.
 */
struct outer_promoted { struct promoted; };
"""))["outer_promoted"]
    assert len(outer.members) == 1
    assert outer.members[0].name is None
    assert outer.members[0].type_text == "struct promoted"
    assert outer.members[0].description == "Promoted tagged member."


def test_unknown_member_macros_are_preserved_as_partial_evidence():
    structure = by_name(parse("""\
struct macro_members {
    tc_gen;
    __bpf_md_ptr(void *, data);
};
"""))["macro_members"]
    assert [(member.name, member.kind, member.generated_by)
            for member in structure.members] == [
        (None, "macro", "tc_gen"),
        ("data", "macro", "__bpf_md_ptr"),
    ]
    assert not structure.parse_complete
    assert len(structure.parse_warnings) == 2


def test_aggregate_signature_retains_packed_and_aligned_attributes():
    symbols = by_name(parse("""\
typedef struct __packed { int value; } packet_t;
struct cacheline_record { int value; } __aligned(64);
struct crypto_record { int value; } CRYPTO_MINALIGN_ATTR;
"""))
    assert "__packed" in symbols["packet_t"].signature
    assert "__aligned(64)" in symbols["cacheline_record"].signature
    assert "CRYPTO_MINALIGN_ATTR" in symbols["crypto_record"].signature


def test_function_local_type_tags_and_typedefs_are_not_file_symbols():
    syms = by_name(parse("""
        struct global_tag { int value; };
        int outer(void)
        {
            struct local_tag { int value; } local;
            enum local_enum { LOCAL_VALUE };
            typedef int local_type;
            return local.value;
        }
    """))
    assert "global_tag" in syms
    assert "local_tag" not in syms
    assert "local_enum" not in syms
    assert "local_type" not in syms


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


def test_one_shadowed_pointer_call_does_not_hide_direct_calls_of_same_name():
    syms = by_name(parse("""\
static void target(void) { }
static void caller(void)
{
        target();
        {
                void (*target)(void) = 0;
                target();
        }
        target();
}
    """, calls=True))

    assert syms["caller"].calls == ("target",)
    assert syms["caller"].indirect_calls == ()


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
