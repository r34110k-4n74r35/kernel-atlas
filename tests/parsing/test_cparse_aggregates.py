"""Aggregate aliases, member shapes, attributes, and documentation."""

from kernel_atlas.parsing import cparse

from .helpers import parse, by_name


def test_multiple_typedef_declarators_are_all_indexed():
    syms = by_name(parse("typedef unsigned long first_t, second_t;"))
    assert syms["first_t"].kind == "typedef"
    assert syms["second_t"].kind == "typedef"


def test_typedef_tagged_type_names_and_direct_aliases_are_preserved():
    symbols = parse("""\
typedef enum study_mode {
    STUDY_OFF,
    STUDY_ON,
} study_mode_t;
typedef enum {
    STUDY_LOW,
    STUDY_HIGH,
} anonymous_mode_t;
typedef struct study_record {
    int value;
} study_record_t;
""")

    tagged_enum = next(
        symbol for symbol in symbols
        if symbol.kind == "enum" and symbol.name == "study_mode")
    anonymous_enum = next(
        symbol for symbol in symbols
        if symbol.kind == "enum" and symbol.name == "anonymous_mode_t")
    tagged_struct = next(
        symbol for symbol in symbols
        if symbol.kind == "struct" and symbol.name == "study_record")

    assert tagged_enum.aliases == ("study_mode_t",)
    assert not tagged_enum.is_anonymous
    assert anonymous_enum.aliases == ("anonymous_mode_t",)
    assert anonymous_enum.is_anonymous
    assert tagged_struct.aliases == ("study_record_t",)
    assert not tagged_struct.is_anonymous

    typedef_names = {
        symbol.name for symbol in symbols if symbol.kind == "typedef"
    }
    assert typedef_names == {
        "study_mode_t", "anonymous_mode_t", "study_record_t",
    }


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


def test_license_and_copyright_headers_are_not_aggregate_summaries():
    syms = by_name(parse("""\
/* Copyright 2026 Example Authors. Licensed under GPL-2.0. */
struct adjacent_header { int value; };

/* A detached prose block that is not structure documentation. */

struct detached_header { int value; };

/* struct detached_named - also physically detached. */

struct detached_named { int value; };
"""))

    assert syms["adjacent_header"].summary is None
    assert syms["detached_header"].summary is None
    assert syms["detached_named"].summary is None


def test_ordinary_aggregate_docs_can_describe_author_and_license_fields():
    structure = by_name(parse("""\
/*
 * struct publication - Source metadata.
 * @author: Person responsible for the source.
 * @license_name: License selected for redistribution.
 */
struct publication { const char *author; const char *license_name; };
"""))["publication"]

    assert structure.summary == "Source metadata."
    assert [member.description for member in structure.members] == [
        "Person responsible for the source.",
        "License selected for redistribution.",
    ]


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
