//! A tiny constraint language over JSON.
//!
//! A **constraint** is a JSON document in the same shape as the documents it
//! describes, with named holes: [`Node::Var`] where members differ in a scalar,
//! [`Node::Wild`] where we decline to describe the leaf at all, and literals
//! everywhere members agree. Every concrete JSON document is a valid constraint.
//!
//! Three operations, and only three:
//!
//! - [`Constraint::meet`] — is this document an instance? If so, what are its bindings?
//! - [`Constraint::substitute`] — bindings back to the exact document.
//! - [`Constraint::subsumes`] — does one constraint generalise another?
//!
//! There is no inference and no generalisation operation: constraints are authored.
//!
//! The crate depends on neither Zarr, nor Arrow, nor DataFusion — JSON in, JSON out.
//! That rule is what keeps it extractable, and what makes it useful to anyone who
//! never touches a query engine.
//!
//! ```
//! use json_constraint::Constraint;
//! use serde_json::json;
//!
//! // Two arrays that must share a time length, whatever it is — the co-constraint
//! // that JSON Schema structurally cannot express.
//! let c = Constraint::parse(&json!({
//!     "temperature": {"shape": [{"$var": "nt", "type": "integer", "minimum": 1}, 100]},
//!     "time":        {"shape": [{"$var": "nt", "type": "integer", "minimum": 1}]},
//! })).unwrap();
//!
//! let member = json!({"temperature": {"shape": [42, 100]}, "time": {"shape": [42]}});
//! let bindings = c.meet(&member).unwrap();
//! assert_eq!(bindings.get("nt"), Some(&json!(42)));
//! assert_eq!(c.substitute(&bindings).unwrap(), member);
//!
//! // 42 in one place and 7 in the other is not a member of this cohort.
//! assert!(c.meet(&json!({"temperature": {"shape": [42, 100]}, "time": {"shape": [7]}})).is_err());
//! ```

mod bindings;
mod domain;
mod error;
mod node;
mod ops;

pub use bindings::Bindings;
pub use domain::{Domain, ScalarType};
pub use error::{Malformed, MeetError, Mismatch, MismatchKind, SubstituteError};
pub use node::Node;
pub use ops::{DeclKind, Declaration};

use std::collections::HashMap;

use serde_json::Value;

/// The spec version this implementation writes and understands.
pub const VERSION: &str = "0.1";

/// A well-formed constraint document.
#[derive(Clone, Debug, PartialEq)]
pub struct Constraint {
    root: Node,
    declarations: Vec<Declaration>,
}

impl Constraint {
    /// Parse and check a constraint document, including the cross-document rules:
    /// identical domains at every use site of a variable, no repeated wildcard, and
    /// disjoint variable/wildcard namespaces.
    pub fn parse(document: &Value) -> Result<Self, Malformed> {
        let root = Node::parse(document, "")?;
        let declarations = collect_declarations(&root)?;
        Ok(Constraint { root, declarations })
    }

    /// The constraint admitting exactly this one document. This is
    /// `create_collection(constraint=None)`: the first member's `zarr.json` taken
    /// verbatim, with every subsequent member having to match it exactly until the
    /// user calls `evolve_schema`.
    pub fn from_description(description: &Value) -> Self {
        Constraint {
            root: Node::all_literal(description),
            declarations: Vec::new(),
        }
    }

    pub fn to_json(&self) -> Value {
        self.root.to_json()
    }

    pub fn root(&self) -> &Node {
        &self.root
    }

    /// Every variable and wildcard, in document order. Each one claims a column.
    pub fn declarations(&self) -> &[Declaration] {
        &self.declarations
    }

    pub fn declaration(&self, name: &str) -> Option<&Declaration> {
        self.declarations.iter().find(|d| d.name == name)
    }

    /// Is `description` a member of this cohort? On success, its bindings.
    pub fn meet(&self, description: &Value) -> Result<Bindings, MeetError> {
        ops::meet(&self.root, description).map_err(MeetError)
    }

    /// Bindings back to the member's description, exactly.
    pub fn substitute(&self, bindings: &Bindings) -> Result<Value, SubstituteError> {
        ops::substitute(&self.root, bindings)
    }

    /// Does `self` generalise `other`? Gates `evolve_schema`: evolution is monotonic,
    /// so a new constraint may only loosen.
    pub fn subsumes(&self, other: &Constraint) -> bool {
        ops::subsumes(&self.root, &other.root)
    }

    /// As [`Constraint::subsumes`], with the reason on failure.
    pub fn subsumes_explain(&self, other: &Constraint) -> Result<(), String> {
        ops::subsumes_explain(&self.root, &other.root)
    }
}

fn collect_declarations(root: &Node) -> Result<Vec<Declaration>, Malformed> {
    let mut order: Vec<String> = Vec::new();
    let mut decls: HashMap<String, Declaration> = HashMap::new();

    let mut error: Option<Malformed> = None;
    root.walk(String::new(), &mut |pointer, node| {
        if error.is_some() {
            return;
        }
        let (name, kind, domain) = match node {
            Node::Var { name, domain } => (name, DeclKind::Variable, domain.clone()),
            Node::Wild { name } => (name, DeclKind::Wildcard, Domain::default()),
            _ => return,
        };
        match decls.get_mut(name) {
            None => {
                order.push(name.clone());
                decls.insert(
                    name.clone(),
                    Declaration {
                        name: name.clone(),
                        kind,
                        domain,
                        pointers: vec![pointer.to_string()],
                    },
                );
            }
            Some(existing) => {
                if existing.kind != kind {
                    error = Some(Malformed::NameCollision { name: name.clone() });
                    return;
                }
                if kind == DeclKind::Wildcard {
                    error = Some(Malformed::RepeatedWildcard {
                        name: name.clone(),
                        first: existing.pointers[0].clone(),
                        second: pointer.to_string(),
                    });
                    return;
                }
                if !existing.domain.same_as(&domain) {
                    error = Some(Malformed::DomainDisagreement {
                        var: name.clone(),
                        first: existing.pointers[0].clone(),
                        second: pointer.to_string(),
                    });
                    return;
                }
                existing.pointers.push(pointer.to_string());
            }
        }
    });

    if let Some(e) = error {
        return Err(e);
    }
    Ok(order
        .into_iter()
        .map(|n| decls.remove(&n).unwrap())
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn c(v: Value) -> Constraint {
        Constraint::parse(&v).unwrap()
    }

    #[test]
    fn repeated_variable_must_declare_identical_domains() {
        let err = Constraint::parse(&json!({
            "a": {"$var": "nt", "type": "integer", "minimum": 1},
            "b": {"$var": "nt", "type": "integer"}
        }))
        .unwrap_err();
        assert!(matches!(err, Malformed::DomainDisagreement { .. }));

        assert!(Constraint::parse(&json!({
            "a": {"$var": "nt", "type": "integer", "minimum": 1},
            "b": {"$var": "nt", "minimum": 1, "type": "integer"}
        }))
        .is_ok());
    }

    #[test]
    fn wildcards_may_not_repeat_and_may_not_collide_with_variables() {
        assert!(matches!(
            Constraint::parse(&json!({"a": {"$wild": "w"}, "b": {"$wild": "w"}})).unwrap_err(),
            Malformed::RepeatedWildcard { .. }
        ));
        assert!(matches!(
            Constraint::parse(&json!({"a": {"$wild": "x"}, "b": {"$var": "x"}})).unwrap_err(),
            Malformed::NameCollision { .. }
        ));
    }

    #[test]
    fn declarations_are_in_document_order() {
        let c = c(json!({"a": {"$var": "nt"}, "b": {"$wild": "codecs"}, "c": {"$var": "nt"}}));
        let names: Vec<_> = c.declarations().iter().map(|d| d.name.as_str()).collect();
        assert_eq!(names, ["nt", "codecs"]);
        assert_eq!(c.declaration("nt").unwrap().pointers.len(), 2);
    }

    #[test]
    fn key_sets_must_match_exactly_there_is_no_optionality() {
        let c = c(json!({"a": 1, "b": 2}));
        let e = c.meet(&json!({"a": 1})).unwrap_err();
        assert_eq!(e.mismatches()[0].kind, MismatchKind::KeySet);
        let e = c.meet(&json!({"a": 1, "b": 2, "extra": 3})).unwrap_err();
        assert_eq!(e.mismatches()[0].kind, MismatchKind::KeySet);
    }

    #[test]
    fn wildcards_absorb_whole_subtrees_verbatim() {
        let c = c(json!({"codecs": {"$wild": "codecs"}}));
        let member =
            json!({"codecs": [{"name": "bytes"}, {"name": "zstd", "configuration": {"level": 3}}]});
        let b = c.meet(&member).unwrap();
        assert_eq!(c.substitute(&b).unwrap(), member);
    }

    #[test]
    fn mismatch_messages_name_the_leaf() {
        let c = c(json!({"data_type": "float32", "shape": [{"$var": "nt", "type": "integer"}]}));
        let e = c
            .meet(&json!({"data_type": "int16", "shape": ["nope"]}))
            .unwrap_err();
        let msg = e.to_string();
        assert!(msg.contains("/data_type"), "{msg}");
        assert!(msg.contains("/shape/0"), "{msg}");
        assert!(msg.contains("float32"), "{msg}");
    }

    #[test]
    fn subsumes_table_from_the_spec() {
        let wild = c(json!({"a": {"$wild": "w"}}));
        let var = c(json!({"a": {"$var": "v", "type": "integer"}}));
        let wide = c(json!({"a": {"$var": "v", "type": "integer", "minimum": 0, "maximum": 10}}));
        let narrow = c(json!({"a": {"$var": "v", "type": "integer", "minimum": 2, "maximum": 5}}));
        let lit = c(json!({"a": 3}));
        let other_lit = c(json!({"a": 4}));

        assert!(wild.subsumes(&var));
        assert!(wild.subsumes(&lit));
        assert!(!var.subsumes(&wild));
        assert!(var.subsumes(&lit));
        assert!(wide.subsumes(&narrow));
        assert!(!narrow.subsumes(&wide));
        assert!(!lit.subsumes(&var));
        assert!(!lit.subsumes(&other_lit));
        assert!(lit.subsumes(&lit));
    }

    #[test]
    fn subsumes_requires_repeated_variables_to_stay_forced_equal() {
        let coupled = c(json!({"a": {"$var": "n"}, "b": {"$var": "n"}}));
        let decoupled = c(json!({"a": {"$var": "p"}, "b": {"$var": "q"}}));
        let still_coupled = c(json!({"a": {"$var": "m"}, "b": {"$var": "m"}}));
        let both_three = c(json!({"a": 3, "b": 3}));
        let three_and_four = c(json!({"a": 3, "b": 4}));

        assert!(!coupled.subsumes(&decoupled));
        assert!(coupled.subsumes(&still_coupled));
        assert!(coupled.subsumes(&both_three));
        assert!(!coupled.subsumes(&three_and_four));
    }

    #[test]
    fn from_description_admits_that_description_and_nothing_else() {
        let d = json!({"zarr_format": 3, "shape": [10, 20]});
        let c = Constraint::from_description(&d);
        assert!(c.declarations().is_empty());
        assert!(c.meet(&d).unwrap().is_empty());
        assert!(c
            .meet(&json!({"zarr_format": 3, "shape": [10, 21]}))
            .is_err());
        assert_eq!(c.to_json(), d);
    }
}
