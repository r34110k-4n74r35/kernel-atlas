"""Recovery of real declarations from kernel syntax and malformed parse trees."""

from .helpers import KINDS, parse, by_name


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
