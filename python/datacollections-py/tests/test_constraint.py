"""The constraint language from Python, and the fixture suite.

These are the same assertions the Rust conformance test makes against the same
files. Two implementations of the *bindings* against one set of fixtures is what
keeps the binding layer honest.
"""

import jsonschema
import pytest

from datacollections import Constraint, ConstraintError, var, wild


def test_fixture_constraints_satisfy_the_meta_schema(fixtures, meta_schema):
    for f in fixtures:
        jsonschema.validate(f["constraint"], meta_schema)


def test_meta_schema_rejects_deferred_syntax(meta_schema):
    for key in ("$each", "$count", "$expr", "$present"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"a": {key: 1}}, meta_schema)


def test_meta_schema_rejects_enums_and_unknown_domain_keys(meta_schema):
    # No enums: categorical variation is a cohort, not a domain.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"a": {"$var": "x", "enum": [1, 2]}}, meta_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"a": {"$var": "x", "pattern": "^a"}}, meta_schema)


def test_members_meet_and_substitute_inverts_exactly(fixtures):
    for f in fixtures:
        c = Constraint(f["constraint"])
        for member in f["members"]:
            bindings = c.meet(member["description"])
            assert bindings == member["bindings"]
            # the round-trip law: nothing is dropped, so this is exact
            assert c.substitute(bindings) == member["description"]


def test_non_members_are_rejected_with_the_offending_leaf(fixtures):
    for f in fixtures:
        c = Constraint(f["constraint"])
        for case in f["non_members"]:
            mismatches = c.mismatches(case["description"])
            assert mismatches, f"{f['name']}: expected rejection — {case['why']}"
            assert all(m.message for m in mismatches)
            with pytest.raises(ConstraintError):
                c.meet(case["description"])


def test_declared_subsumption_holds(fixtures):
    for f in fixtures:
        c = Constraint(f["constraint"])
        for case in f.get("subsumes", []):
            loosened = Constraint(case["loosened"])
            assert loosened.subsumes(c) is case["holds"], f"{f['name']}: {case['why']}"


def test_every_hole_is_declared_and_bound(fixtures):
    for f in fixtures:
        c = Constraint(f["constraint"])
        names = {d["name"] for d in c.declarations}
        for member in f["members"]:
            assert set(member["bindings"]) == names


def test_the_co_constraint_is_within_a_member_not_across_members():
    c = Constraint(
        {
            "a": {"shape": [var("nt", type="integer", minimum=1)]},
            "b": {"shape": [var("nt", type="integer", minimum=1)]},
        }
    )
    # equal within a member: fine, and different members may differ freely
    assert c.meet({"a": {"shape": [42]}, "b": {"shape": [42]}}) == {"nt": 42}
    assert c.meet({"a": {"shape": [7]}, "b": {"shape": [7]}}) == {"nt": 7}
    # unequal within one member: not a member of this cohort
    with pytest.raises(ConstraintError, match="co-constraint"):
        c.meet({"a": {"shape": [42]}, "b": {"shape": [7]}})


def test_wildcards_decline_to_describe_and_reinstate_verbatim():
    c = Constraint({"codecs": wild("codecs")})
    value = [{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "zstd"}]
    bindings = c.meet({"codecs": value})
    assert bindings == {"codecs": value}
    assert c.substitute(bindings) == {"codecs": value}


def test_a_repeated_variable_must_declare_identical_domains():
    with pytest.raises(ConstraintError, match="identical domains"):
        Constraint({"a": var("n", type="integer", minimum=1), "b": var("n", type="integer")})


def test_evolution_is_monotonic():
    tight = Constraint({"a": 1})
    loose = Constraint({"a": var("a", type="integer")})
    assert loose.subsumes(tight)
    assert not tight.subsumes(loose)
    assert "cannot generalise" in tight.explain_subsumes(loose)


def test_from_description_is_the_bootstrapping_path():
    d = {"zarr_format": 3, "shape": [10, 20]}
    c = Constraint.from_description(d)
    assert c.declarations == []
    assert c.meet(d) == {}
    assert c.mismatches({"zarr_format": 3, "shape": [10, 21]})
