//! Property tests for the three operations.
//!
//! Randomised, but with a seeded xorshift rather than a proptest dependency — the
//! crate's whole selling point is that it has almost no dependencies, and a
//! deterministic seed sweep is reproducible in a way shrinking is not.

use json_constraint::{Constraint, Node};
use serde_json::{json, Map, Value};

// ------------------------------------------------------------------ generators

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn chance(&mut self, percent: u64) -> bool {
        self.below(100) < percent
    }
}

/// A document shaped roughly like a consolidated `zarr.json`: nested objects,
/// arrays of numbers, string leaves, the occasional null.
fn gen_document(rng: &mut Rng, depth: u32) -> Value {
    match rng.below(if depth == 0 { 4 } else { 6 }) {
        0 => json!(rng.below(1000) as i64),
        1 => json!(format!("s{}", rng.below(8))),
        2 => json!(rng.chance(50)),
        3 => Value::Null,
        4 => {
            let n = rng.below(4);
            Value::Array((0..n).map(|_| gen_document(rng, depth - 1)).collect())
        }
        _ => {
            let n = 1 + rng.below(4);
            let mut m = Map::new();
            for i in 0..n {
                m.insert(format!("k{i}"), gen_document(rng, depth - 1));
            }
            Value::Object(m)
        }
    }
}

/// Abstract a document into a constraint: scalars sometimes become variables,
/// structural leaves sometimes become wildcards. `looseness` is 0 (all literal) to
/// 2 (wildcard-happy), which gives us ladders of related constraints for free.
fn abstract_document(rng: &mut Rng, doc: &Value, looseness: u32, next_name: &mut u32) -> Value {
    let mut fresh = || {
        *next_name += 1;
        format!("v{next_name}")
    };
    match doc {
        Value::Object(m) => {
            if looseness >= 2 && rng.chance(15) {
                return json!({ "$wild": fresh() });
            }
            let mut out = Map::new();
            for (k, v) in m {
                out.insert(k.clone(), abstract_document(rng, v, looseness, next_name));
            }
            Value::Object(out)
        }
        Value::Array(items) => {
            if looseness >= 2 && rng.chance(15) {
                return json!({ "$wild": fresh() });
            }
            Value::Array(
                items
                    .iter()
                    .map(|v| abstract_document(rng, v, looseness, next_name))
                    .collect(),
            )
        }
        Value::Number(n) => {
            if looseness >= 1 && rng.chance(35) {
                let name = fresh();
                if n.is_i64() {
                    json!({"$var": name, "type": "integer", "minimum": -1000, "maximum": 100000})
                } else {
                    json!({"$var": name, "type": "number"})
                }
            } else {
                doc.clone()
            }
        }
        Value::String(_) => {
            if looseness >= 1 && rng.chance(25) {
                json!({"$var": fresh(), "type": "string"})
            } else {
                doc.clone()
            }
        }
        _ => {
            if looseness >= 2 && rng.chance(10) {
                json!({ "$wild": fresh() })
            } else {
                doc.clone()
            }
        }
    }
}

/// Every literal position in the constraint, as a JSON Pointer — the positions a
/// mutation is guaranteed to be caught at.
fn literal_pointers(c: &Constraint) -> Vec<String> {
    let mut out = Vec::new();
    c.root().walk(String::new(), &mut |ptr, n| {
        if matches!(n, Node::Literal(v) if !v.is_null()) {
            out.push(ptr.to_string());
        }
    });
    out
}

fn mutate_at(doc: &Value, pointer: &str) -> Value {
    let mut out = doc.clone();
    let target = out.pointer_mut(pointer).unwrap();
    *target = match &*target {
        Value::Number(n) => json!(n.as_f64().unwrap_or(0.0) + 1.0),
        Value::String(s) => json!(format!("{s}!")),
        Value::Bool(b) => json!(!b),
        _ => json!("mutated"),
    };
    out
}

fn seeds() -> impl Iterator<Item = u64> {
    (1..300u64).map(|i| i.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
}

// ----------------------------------------------------------------------- laws

/// `substitute(c, meet(c, description(m))) == description(m)`, exactly.
/// This is the derivability claim, and the law the whole design rests on.
#[test]
fn substitute_inverts_meet() {
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        for looseness in 0..3 {
            let c = Constraint::parse(&abstract_document(rng, &doc, looseness, &mut 0)).unwrap();
            let bindings = c.meet(&doc).unwrap_or_else(|e| {
                panic!("seed {seed}: abstraction rejected its own document: {e}")
            });
            assert_eq!(c.substitute(&bindings).unwrap(), doc, "seed {seed}");
        }
    }
}

/// A constraint accepts the members it was authored for and rejects mutations of
/// them at every position it describes literally.
#[test]
fn meet_rejects_mutations_of_literal_positions() {
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        let c = Constraint::parse(&abstract_document(rng, &doc, 1, &mut 0)).unwrap();
        assert!(c.meet(&doc).is_ok(), "seed {seed}");
        for ptr in literal_pointers(&c) {
            if ptr.is_empty() {
                continue;
            }
            let mutated = mutate_at(&doc, &ptr);
            let err = c
                .meet(&mutated)
                .unwrap_err_or_else(|| panic!("seed {seed}: mutation at {ptr} accepted"));
            assert!(
                err.mismatches().iter().any(|m| m.pointer == ptr),
                "seed {seed}: mutation at {ptr} reported as {err}"
            );
        }
    }
}

trait UnwrapErrOrElse<T, E> {
    fn unwrap_err_or_else(self, f: impl FnOnce() -> E) -> E;
}

impl<T: std::fmt::Debug, E> UnwrapErrOrElse<T, E> for Result<T, E> {
    fn unwrap_err_or_else(self, f: impl FnOnce() -> E) -> E {
        match self {
            Ok(_) => f(),
            Err(e) => e,
        }
    }
}

#[test]
fn subsumes_is_reflexive() {
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        for looseness in 0..3 {
            let c = Constraint::parse(&abstract_document(rng, &doc, looseness, &mut 0)).unwrap();
            assert!(c.subsumes(&c), "seed {seed}: {:?}", c.subsumes_explain(&c));
        }
    }
}

/// Whenever a ⊒ b and b ⊒ c both hold, a ⊒ c must too. Checked over ladders built by
/// abstracting one document at increasing looseness, which is where the interesting
/// pairs actually are.
#[test]
fn subsumes_is_transitive() {
    let mut checked = 0;
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        let ladder: Vec<Constraint> = (0..3)
            .map(|l| Constraint::parse(&abstract_document(rng, &doc, l, &mut 0)).unwrap())
            .collect();
        for a in &ladder {
            for b in &ladder {
                for c in &ladder {
                    if a.subsumes(b) && b.subsumes(c) {
                        assert!(
                            a.subsumes(c),
                            "seed {seed}: transitivity broken: {:?}",
                            a.subsumes_explain(c)
                        );
                        checked += 1;
                    }
                }
            }
        }
    }
    assert!(checked > 500, "only {checked} transitive triples exercised");
}

/// Antisymmetry, up to renaming: two constraints that generalise each other describe
/// the same set of documents, so they differ at most in the names of their holes.
#[test]
fn subsumes_is_antisymmetric_up_to_renaming() {
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        let ladder: Vec<Constraint> = (0..3)
            .map(|l| Constraint::parse(&abstract_document(rng, &doc, l, &mut 0)).unwrap())
            .collect();
        for a in &ladder {
            for b in &ladder {
                if a.subsumes(b) && b.subsumes(a) {
                    assert_eq!(
                        normalise(a),
                        normalise(b),
                        "seed {seed}: mutually subsuming but structurally different"
                    );
                }
            }
        }
    }
}

/// Rename holes to their order of first occurrence, so two constraints differing
/// only in variable names compare equal.
fn normalise(c: &Constraint) -> Value {
    let mut order: Vec<String> = Vec::new();
    c.root().walk(String::new(), &mut |_, n| match n {
        Node::Var { name, .. } | Node::Wild { name }
            if !order.contains(name) => {
                order.push(name.clone());
            }
        _ => {}
    });
    fn rewrite(v: &Value, order: &[String]) -> Value {
        match v {
            Value::Object(m) => {
                let mut out = Map::new();
                for (k, val) in m {
                    if (k == "$var" || k == "$wild") && val.is_string() {
                        let name = val.as_str().unwrap();
                        let i = order.iter().position(|n| n == name).unwrap();
                        out.insert(k.clone(), json!(format!("_{i}")));
                    } else {
                        out.insert(k.clone(), rewrite(val, order));
                    }
                }
                Value::Object(out)
            }
            Value::Array(items) => Value::Array(items.iter().map(|i| rewrite(i, order)).collect()),
            other => other.clone(),
        }
    }
    rewrite(&c.to_json(), &order)
}

/// Round-tripping a constraint through its JSON encoding changes nothing.
#[test]
fn json_encoding_round_trips() {
    for seed in seeds() {
        let rng = &mut Rng(seed);
        let doc = gen_document(rng, 4);
        let encoded = abstract_document(rng, &doc, 2, &mut 0);
        let c = Constraint::parse(&encoded).unwrap();
        assert_eq!(c.to_json(), encoded, "seed {seed}");
        assert_eq!(Constraint::parse(&c.to_json()).unwrap(), c, "seed {seed}");
    }
}

/// Floats must survive parse → serialise unchanged, bit for bit.
///
/// Found by running the HST example at 100 members: three members failed
/// `verify()` because an `EXPTIME` attribute of `1305.8754880000001` came back as
/// `1305.875488` — one ULP out. serde_json's default parser is permitted that, and
/// the `float_roundtrip` feature is what forbids it. For a project whose central
/// claim is that a constraint plus a row reconstructs the member's `zarr.json`
/// *exactly*, a ULP is not a rounding detail; it is the claim failing.
#[test]
fn floats_round_trip_bit_for_bit() {
    let awkward = [
        "1305.8754880000001",
        "955.8724970000001",
        "0.10000000000000002",
        "2.2250738585072014e-308",
        "1.7976931348623157e308",
        "-0.0",
    ];
    for text in awkward {
        let doc: Value = serde_json::from_str(&format!("{{\"a\": {text}}}")).unwrap();
        let c = Constraint::parse(&json!({"a": {"$var": "a", "type": "number"}})).unwrap();
        let bindings = c.meet(&doc).unwrap();
        assert_eq!(c.substitute(&bindings).unwrap(), doc, "{text}");
        // Compared as parsed values, not as bytes: `1.7976931348623157e308` may be
        // re-spelled `1.7976931348623157e+308`, which is the same double. Equality
        // of descriptions is JSON-value equality throughout — see
        // spec/constraint-language.md §3.1.
        let reparsed: Value = serde_json::from_str(&serde_json::to_string(&bindings.to_json()).unwrap()).unwrap();
        assert_eq!(reparsed["a"], doc["a"], "{text} did not survive serialisation");
    }
}
