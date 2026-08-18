//! The fixture suite in `spec/fixtures/constraints`. These files are the contract
//! between crates; the Python package runs the same assertions against the same
//! files, which is what keeps the two implementations honest.

use std::path::PathBuf;

use json_constraint::{Bindings, Constraint};
use serde_json::Value;

fn fixtures() -> Vec<(String, Value)> {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../spec/fixtures/constraints")
        .canonicalize()
        .expect("fixtures directory; run `python scripts/make_fixtures.py`");
    let mut out: Vec<(String, Value)> = std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| {
            let path = e.unwrap().path();
            (path.extension()? == "json").then(|| {
                let text = std::fs::read_to_string(&path).unwrap();
                (
                    path.file_stem().unwrap().to_string_lossy().into_owned(),
                    serde_json::from_str(&text).unwrap(),
                )
            })
        })
        .collect();
    out.sort_by(|a, b| a.0.cmp(&b.0));
    assert!(!out.is_empty(), "no fixtures found");
    out
}

#[test]
fn every_fixture_constraint_is_well_formed() {
    for (name, f) in fixtures() {
        Constraint::parse(&f["constraint"]).unwrap_or_else(|e| panic!("{name}: {e}"));
    }
}

#[test]
fn members_meet_with_the_stated_bindings() {
    for (name, f) in fixtures() {
        let c = Constraint::parse(&f["constraint"]).unwrap();
        for (i, m) in f["members"].as_array().unwrap().iter().enumerate() {
            let bindings = c
                .meet(&m["description"])
                .unwrap_or_else(|e| panic!("{name} member {i}: {e}"));
            let expected = Bindings::from_json(&m["bindings"]).unwrap();
            assert_eq!(
                bindings.to_json(),
                expected.to_json(),
                "{name} member {i}: bindings differ"
            );
        }
    }
}

/// The round-trip law: a constraint plus a row's bindings reconstructs the member's
/// `zarr.json` in full. Because nothing is dropped, derivability is literal.
#[test]
fn substitute_inverts_meet_exactly() {
    for (name, f) in fixtures() {
        let c = Constraint::parse(&f["constraint"]).unwrap();
        for (i, m) in f["members"].as_array().unwrap().iter().enumerate() {
            let d = &m["description"];
            let bindings = c.meet(d).unwrap();
            assert_eq!(&c.substitute(&bindings).unwrap(), d, "{name} member {i}");
        }
    }
}

#[test]
fn non_members_are_rejected_with_a_reason() {
    for (name, f) in fixtures() {
        let c = Constraint::parse(&f["constraint"]).unwrap();
        for (i, m) in f["non_members"].as_array().unwrap().iter().enumerate() {
            let why = m["why"].as_str().unwrap();
            let err = c.meet(&m["description"]).expect_err(&format!(
                "{name} non-member {i} was accepted; expected: {why}"
            ));
            assert!(!err.mismatches().is_empty());
            assert!(!err.mismatches()[0].pointer.is_empty() || err.mismatches().len() == 1);
        }
    }
}

#[test]
fn declared_subsumption_holds() {
    for (name, f) in fixtures() {
        let c = Constraint::parse(&f["constraint"]).unwrap();
        let Some(cases) = f["subsumes"].as_array() else {
            continue;
        };
        for (i, case) in cases.iter().enumerate() {
            let loosened = Constraint::parse(&case["loosened"]).unwrap();
            let holds = case["holds"].as_bool().unwrap();
            assert_eq!(
                loosened.subsumes(&c),
                holds,
                "{name} subsumes case {i} ({}): {:?}",
                case["why"].as_str().unwrap_or(""),
                loosened.subsumes_explain(&c)
            );
        }
    }
}

/// Every variable and wildcard claims a column, and their names are the column
/// namespace. The storage layer relies on this being derivable from the constraint
/// alone — "which columns must this table have?" answerable without reading the data.
#[test]
fn declarations_cover_every_binding() {
    for (name, f) in fixtures() {
        let c = Constraint::parse(&f["constraint"]).unwrap();
        let declared: Vec<&str> = c.declarations().iter().map(|d| d.name.as_str()).collect();
        for m in f["members"].as_array().unwrap() {
            let bindings = c.meet(&m["description"]).unwrap();
            let bound: Vec<&str> = bindings.names().map(|s| s.as_str()).collect();
            assert_eq!(
                declared.len(),
                bound.len(),
                "{name}: declared {declared:?} but bound {bound:?}"
            );
            for b in bound {
                assert!(declared.contains(&b), "{name}: {b} bound but not declared");
            }
        }
    }
}
