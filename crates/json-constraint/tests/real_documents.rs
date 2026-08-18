//! The three laws, over **real published metadata** rather than generated documents.
//!
//! `spec/fixtures/real/documents.json` is a cached corpus of published metadata —
//! MAST-U's fusion archive, the IDR's OME-Zarr images, and HST observation records
//! from MAST — fetched by `python scripts/fetch_real_documents.py`. If the cache is
//! absent these tests skip, so the suite stays offline-runnable; CI should fetch it.
//!
//! This is the test PLAN.md asks for. One honest limit, checked rather than assumed:
//! **the corpus does not currently contain a float that trips serde_json's default
//! parser.** All 618 of its fractional values round-trip even without
//! `float_roundtrip`, so this corpus would *not* have caught the one-ULP bug that
//! made three HST members fail `verify()`. The value that does trip it — an HST
//! `EXPTIME` of `1305.8754880000001` — is pinned in `laws.rs` instead. A corpus is
//! only as good as its worst case, and a bigger corpus is not automatically a
//! better one.
//!
//! The corpus is Zarr **v2**, deliberately. The constraint language is a language
//! over JSON, and nothing in this crate is Zarr-version-specific — or Zarr-specific.

use std::collections::BTreeMap;
use std::path::PathBuf;

use json_constraint::{Constraint, Node};
use serde_json::{json, Map, Value};

fn corpus() -> Vec<(String, Value)> {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../spec/fixtures/real/documents.json");
    let Ok(text) = std::fs::read_to_string(&path) else {
        eprintln!(
            "skipping: no real-document corpus at {}; run `python scripts/fetch_real_documents.py`",
            path.display()
        );
        return Vec::new();
    };
    let entries: Vec<Value> = serde_json::from_str(&text).expect("corpus is JSON");
    entries
        .into_iter()
        .map(|e| {
            (
                e["url"].as_str().unwrap_or("<unknown>").to_string(),
                e["document"].clone(),
            )
        })
        .collect()
}

/// Abstract every scalar leaf into a variable named after its position. Deliberately
/// extreme: it maximises the number of holes, so the round-trip law is exercised on
/// every scalar the corpus contains — floats with awkward decimal expansions, empty
/// strings, unicode, nulls.
fn abstract_every_scalar(
    value: &Value,
    pointer: &str,
    names: &mut BTreeMap<String, String>,
) -> Value {
    match value {
        Value::Object(m) => {
            let mut out = Map::new();
            for (k, v) in m {
                out.insert(
                    k.clone(),
                    abstract_every_scalar(v, &format!("{pointer}_{k}"), names),
                );
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .enumerate()
                .map(|(i, v)| abstract_every_scalar(v, &format!("{pointer}_{i}"), names))
                .collect(),
        ),
        Value::Null => value.clone(), // an untyped variable would admit null anyway
        scalar => {
            let name = ident(pointer, names.len());
            names.insert(name.clone(), pointer.to_string());
            let ty = match scalar {
                Value::Bool(_) => "boolean",
                Value::String(_) => "string",
                Value::Number(n) if n.is_i64() || n.is_u64() => "integer",
                _ => "number",
            };
            json!({"$var": name, "type": ty})
        }
    }
}

fn ident(pointer: &str, n: usize) -> String {
    let cleaned: String = pointer
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    format!("v{n}_{}", cleaned.trim_matches('_'))
}

/// Wildcard the value of every top-level key: the "decline to describe this subtree"
/// case, on documents whose subtrees are real OME `multiscales` and `omero` blocks.
fn wildcard_top_level(value: &Value) -> Option<Value> {
    let obj = value.as_object()?;
    let mut out = Map::new();
    for (i, k) in obj.keys().enumerate() {
        out.insert(k.clone(), json!({"$wild": format!("w{i}")}));
    }
    Some(Value::Object(out))
}

#[test]
fn every_real_document_is_itself_a_valid_constraint() {
    let docs = corpus();
    for (url, doc) in &docs {
        let c = Constraint::from_description(doc);
        assert_eq!(&c.to_json(), doc, "{url}");
        assert!(c.meet(doc).is_ok(), "{url}");
    }
    eprintln!("checked {} real documents", docs.len());
}

#[test]
fn substitute_inverts_meet_on_real_documents() {
    for (url, doc) in corpus() {
        let mut names = BTreeMap::new();
        let abstracted = abstract_every_scalar(&doc, "", &mut names);
        let c = Constraint::parse(&abstracted).unwrap_or_else(|e| panic!("{url}: {e}"));
        let bindings = c.meet(&doc).unwrap_or_else(|e| panic!("{url}: {e}"));
        assert_eq!(c.substitute(&bindings).unwrap(), doc, "{url}");

        if let Some(wild) = wildcard_top_level(&doc) {
            let c = Constraint::parse(&wild).unwrap();
            let bindings = c.meet(&doc).unwrap_or_else(|e| panic!("{url}: {e}"));
            assert_eq!(c.substitute(&bindings).unwrap(), doc, "{url} (wildcarded)");
        }
    }
}

#[test]
fn meet_rejects_mutations_of_real_documents() {
    for (url, doc) in corpus() {
        let c = Constraint::from_description(&doc);
        let mut literals = Vec::new();
        c.root().walk(String::new(), &mut |ptr, n| {
            if matches!(n, Node::Literal(v) if v.is_number() || v.is_string() || v.is_boolean()) {
                literals.push(ptr.to_string());
            }
        });
        for pointer in literals.iter().take(20) {
            let mut mutated = doc.clone();
            let target = mutated.pointer_mut(pointer).unwrap();
            *target = match &*target {
                Value::Number(n) => json!(n.as_f64().unwrap_or(0.0) + 1.5),
                Value::String(s) => json!(format!("{s}~")),
                Value::Bool(b) => json!(!b),
                other => other.clone(),
            };
            let err = c
                .meet(&mutated)
                .expect_err(&format!("{url}: mutation at {pointer} was accepted"));
            assert!(
                err.mismatches().iter().any(|m| &m.pointer == pointer),
                "{url}"
            );
        }
    }
}

/// Real attribute documents carry floats whose decimal expansion is long. A parser
/// that is a ULP out on any of them breaks derivability silently, which is exactly
/// what happened before `float_roundtrip` was enabled.
#[test]
fn real_floats_survive_the_round_trip() {
    let mut checked = 0;
    for (url, doc) in corpus() {
        let mut floats = Vec::new();
        collect_floats(&doc, &mut floats);
        for f in floats {
            let c = Constraint::parse(&json!({"x": {"$var": "x", "type": "number"}})).unwrap();
            let member = json!({ "x": f });
            let bindings = c.meet(&member).unwrap();
            assert_eq!(c.substitute(&bindings).unwrap(), member, "{url}");
            checked += 1;
        }
    }
    eprintln!("checked {checked} real float values");
}

fn collect_floats(value: &Value, out: &mut Vec<Value>) {
    match value {
        Value::Number(n) if n.as_f64().map(|f| f.fract() != 0.0).unwrap_or(false) => {
            out.push(value.clone())
        }
        Value::Object(m) => m.values().for_each(|v| collect_floats(v, out)),
        Value::Array(items) => items.iter().for_each(|v| collect_floats(v, out)),
        _ => {}
    }
}
