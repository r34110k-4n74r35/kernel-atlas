"""Direct and indirect calls, occurrence evidence, and macro lifetimes."""

from .helpers import parse, by_name


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


def test_call_sites_retain_member_dereference_and_direct_evidence():
    caller = by_name(parse("""\
int caller(void (*fp)(void), struct ops *ops)
{
    fn();
    ops->fn();
    usb_hcd->driver->alloc_dev();
    (*fp)();
    return 0;
}
    """, calls=True))["caller"]

    assert caller.calls == (
        "fn", "ops->fn", "usb_hcd->driver->alloc_dev", "*fp",
    )
    assert caller.indirect_calls == (
        "ops->fn", "usb_hcd->driver->alloc_dev", "*fp",
    )
    assert [(site.name, site.kind) for site in caller.call_sites] == [
        ("fn", "direct"),
        ("ops->fn", "indirect"),
        ("usb_hcd->driver->alloc_dev", "indirect"),
        ("*fp", "indirect"),
    ]


def test_call_sites_retain_subscript_and_conditional_indirect_expressions():
    caller = by_name(parse("""\
int caller(int (**check_part)(void), int condition,
           int (*left)(void), int (*right)(void))
{
    check_part[2]();
    ((int (*)(void))left)();
    return (condition ? left : right)();
}
    """, calls=True))["caller"]

    assert caller.calls == (
        "check_part[2]", "(int (*)(void))left", "condition ? left : right",
    )
    assert caller.indirect_calls == caller.calls
    assert {site.kind for site in caller.call_sites} == {"indirect"}


def test_macro_call_state_respects_future_defines_and_undef_boundaries():
    syms = by_name(parse("""\
int target(void);
int before(void) { return target(); }
#define target() 1
int during(void) { return target(); }
#undef target
int after(void) { return target(); }
    """, calls=True))

    assert syms["before"].call_sites[0].kind == "direct"
    assert syms["during"].call_sites[0].kind == "macro"
    assert syms["after"].call_sites[0].kind == "direct"


def test_conditional_macro_transitions_never_promote_a_possible_macro():
    defined = by_name(parse("""\
#ifdef CONFIG_TARGET
#define target() 1
#endif
int caller(void) { return target(); }
    """, calls=True))["caller"]
    maybe_undefined = by_name(parse("""\
#define target() 1
#ifdef CONFIG_TARGET
#undef target
#endif
int caller(void) { return target(); }
    """, calls=True))["caller"]
    definitely_undefined = by_name(parse("""\
#ifdef CONFIG_TARGET
#define target() 1
#endif
#undef target
int caller(void) { return target(); }
    """, calls=True))["caller"]

    assert defined.call_sites[0].kind == "macro"
    assert maybe_undefined.call_sites[0].kind == "macro"
    assert definitely_undefined.call_sites[0].kind == "direct"


def test_macro_state_is_scoped_to_its_conditional_branch():
    syms = by_name(parse("""\
#if CONFIG_TARGET
#define target() 1
int macro_branch(void) { return target(); }
#else
int direct_branch(void) { return target(); }
#endif
int after_branches(void) { return target(); }
    """, calls=True))

    assert syms["macro_branch"].call_sites[0].kind == "macro"
    assert syms["direct_branch"].call_sites[0].kind == "direct"
    assert syms["after_branches"].call_sites[0].kind == "macro"


def test_exhaustive_conditional_undefs_clear_a_preexisting_macro():
    caller = by_name(parse("""\
#define target() 1
#if CONFIG_A
#undef target
#else
#undef target
#endif
int caller(void) { return target(); }
    """, calls=True))["caller"]

    assert caller.call_sites[0].kind == "direct"


def test_elif_and_else_are_distinct_macro_state_branches():
    syms = by_name(parse("""\
#if CONFIG_A
int first_branch(void) { return target(); }
#elif CONFIG_B
#define target() 1
int macro_branch(void) { return target(); }
#else
int final_branch(void) { return target(); }
#endif
    """, calls=True))

    assert syms["first_branch"].call_sites[0].kind == "direct"
    assert syms["macro_branch"].call_sites[0].kind == "macro"
    assert syms["final_branch"].call_sites[0].kind == "direct"


def test_function_like_macro_precedes_a_same_named_pointer_binding():
    caller = by_name(parse("""\
#define callback() 1
int caller(int (*callback)(void))
{
    return callback() + (callback)();
}
    """, calls=True))["caller"]

    assert [site.kind for site in caller.call_sites] == ["macro", "indirect"]


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
    assert syms["caller"].indirect_calls == ("target",)
    assert [site.kind for site in syms["caller"].call_sites] == [
        "direct", "indirect", "direct",
    ]
