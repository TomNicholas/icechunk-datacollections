//! The constraint tree, and its JSON encoding.
//!
//! A constraint document is a JSON document in the same shape as the thing it
//! describes, with named holes. Every concrete JSON document is a valid constraint
//! — the "superset of JSON" property that makes `create_collection(constraint=None)`
//! possible.

use indexmap::IndexMap;
use serde_json::{Map, Value};

use crate::domain::Domain;
use crate::error::Malformed;

/// Keys reserved for deferred language features (spec §5). Rejected in 0.1 so that
/// adding them later is not a breaking change.
pub const DEFERRED_KEYS: [&str; 4] = ["$each", "$count", "$expr", "$present"];

#[derive(Clone, Debug, PartialEq)]
pub enum Node {
    /// Invariant across the cohort. No column.
    Literal(Value),
    /// A named hole matching scalars in a domain. Gets a column.
    Var {
        name: String,
        domain: Domain,
    },
    /// A leaf we decline to describe. Matches any JSON value; stored verbatim.
    Wild {
        name: String,
    },
    Object(IndexMap<String, Node>),
    Array(Vec<Node>),
}

fn valid_name(s: &str) -> bool {
    let mut cs = s.chars();
    match cs.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    cs.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn child_pointer(parent: &str, token: &str) -> String {
    format!("{parent}/{}", token.replace('~', "~0").replace('/', "~1"))
}

impl Node {
    /// Parse a constraint document. Structural checks only; cross-document rules
    /// (identical domains, no repeated wildcards) are enforced in [`crate::Constraint`].
    pub fn parse(value: &Value, pointer: &str) -> Result<Node, Malformed> {
        match value {
            Value::Object(obj) => Self::parse_object(obj, pointer),
            Value::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for (i, item) in items.iter().enumerate() {
                    out.push(Node::parse(item, &child_pointer(pointer, &i.to_string()))?);
                }
                Ok(Node::Array(out))
            }
            scalar => Ok(Node::Literal(scalar.clone())),
        }
    }

    fn parse_object(obj: &Map<String, Value>, pointer: &str) -> Result<Node, Malformed> {
        for key in DEFERRED_KEYS {
            if obj.contains_key(key) {
                return Err(Malformed::DeferredSyntax {
                    pointer: pointer.to_string(),
                    key: key.to_string(),
                });
            }
        }

        let marks = ["$var", "$wild", "$literal"]
            .into_iter()
            .filter(|k| obj.contains_key(*k))
            .count();
        if marks > 1 {
            return Err(Malformed::ConflictingLeafKeys {
                pointer: pointer.to_string(),
            });
        }

        if let Some(v) = obj.get("$literal") {
            if obj.len() != 1 {
                return Err(Malformed::ConflictingLeafKeys {
                    pointer: pointer.to_string(),
                });
            }
            return Ok(Node::Literal(v.clone()));
        }

        if let Some(v) = obj.get("$wild") {
            let name = v
                .as_str()
                .filter(|s| valid_name(s))
                .ok_or_else(|| Malformed::BadName {
                    pointer: pointer.to_string(),
                    kind: "wild".into(),
                    found: v.to_string(),
                })?;
            if obj.len() != 1 {
                return Err(Malformed::BadName {
                    pointer: pointer.to_string(),
                    kind: "wild".into(),
                    found: "a $wild leaf takes no other keys".into(),
                });
            }
            return Ok(Node::Wild {
                name: name.to_string(),
            });
        }

        if let Some(v) = obj.get("$var") {
            let name = v
                .as_str()
                .filter(|s| valid_name(s))
                .ok_or_else(|| Malformed::BadName {
                    pointer: pointer.to_string(),
                    kind: "var".into(),
                    found: v.to_string(),
                })?;
            let domain = Domain::parse(obj, name)?;
            return Ok(Node::Var {
                name: name.to_string(),
                domain,
            });
        }

        let mut out = IndexMap::with_capacity(obj.len());
        for (k, v) in obj {
            out.insert(k.clone(), Node::parse(v, &child_pointer(pointer, k))?);
        }
        Ok(Node::Object(out))
    }

    /// Serialise back to a constraint document.
    pub fn to_json(&self) -> Value {
        match self {
            Node::Literal(v) => match v {
                // Round-trip an escaped literal back through `$literal`, so that a
                // description containing a `$var` key survives.
                Value::Object(o) if needs_escaping(o) => {
                    let mut m = Map::new();
                    m.insert("$literal".into(), v.clone());
                    Value::Object(m)
                }
                other => other.clone(),
            },
            Node::Var { name, domain } => {
                let mut m = Map::new();
                m.insert("$var".into(), Value::String(name.clone()));
                for (k, v) in domain.to_map() {
                    m.insert(k, v);
                }
                Value::Object(m)
            }
            Node::Wild { name } => {
                let mut m = Map::new();
                m.insert("$wild".into(), Value::String(name.clone()));
                Value::Object(m)
            }
            Node::Object(fields) => {
                let mut m = Map::new();
                for (k, v) in fields {
                    m.insert(k.clone(), v.to_json());
                }
                Value::Object(m)
            }
            Node::Array(items) => Value::Array(items.iter().map(Node::to_json).collect()),
        }
    }

    /// Depth-first walk, yielding (JSON Pointer, node) for every node.
    pub fn walk<'a>(&'a self, pointer: String, f: &mut impl FnMut(&str, &'a Node)) {
        f(&pointer, self);
        match self {
            Node::Object(fields) => {
                for (k, v) in fields {
                    v.walk(child_pointer(&pointer, k), f);
                }
            }
            Node::Array(items) => {
                for (i, v) in items.iter().enumerate() {
                    v.walk(child_pointer(&pointer, &i.to_string()), f);
                }
            }
            _ => {}
        }
    }

    /// The constraint that describes exactly this document and nothing else.
    /// Used by `create_collection(constraint=None)`, which takes the first member's
    /// `zarr.json` verbatim.
    pub fn all_literal(value: &Value) -> Node {
        match value {
            Value::Object(obj) => Node::Object(
                obj.iter()
                    .map(|(k, v)| (k.clone(), Node::all_literal(v)))
                    .collect(),
            ),
            Value::Array(items) => Node::Array(items.iter().map(Node::all_literal).collect()),
            scalar => Node::Literal(scalar.clone()),
        }
    }

    pub fn kind_name(&self) -> &'static str {
        match self {
            Node::Literal(Value::Object(_)) => "object",
            Node::Literal(Value::Array(_)) => "array",
            Node::Literal(_) => "scalar",
            Node::Var { .. } => "variable",
            Node::Wild { .. } => "wildcard",
            Node::Object(_) => "object",
            Node::Array(_) => "array",
        }
    }
}

fn needs_escaping(obj: &Map<String, Value>) -> bool {
    ["$var", "$wild", "$literal"]
        .iter()
        .any(|k| obj.contains_key(*k))
        || DEFERRED_KEYS.iter().any(|k| obj.contains_key(*k))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn every_concrete_document_is_a_constraint() {
        let doc = json!({"zarr_format": 3, "shape": [10, 20], "attrs": {"a": [1, {"b": null}]}});
        let n = Node::parse(&doc, "").unwrap();
        assert_eq!(n.to_json(), doc);
    }

    #[test]
    fn leaves_parse() {
        let n = Node::parse(&json!({"$var": "nt", "type": "integer", "minimum": 1}), "").unwrap();
        assert!(matches!(n, Node::Var { .. }));
        let n = Node::parse(&json!({"$wild": "codecs"}), "").unwrap();
        assert!(matches!(n, Node::Wild { .. }));
        let n = Node::parse(&json!({"$literal": {"$var": "not a var"}}), "").unwrap();
        assert!(matches!(n, Node::Literal(Value::Object(_))));
    }

    #[test]
    fn escaped_literals_round_trip() {
        let doc = json!({"attributes": {"$literal": {"$var": "x"}}});
        let n = Node::parse(&doc, "").unwrap();
        assert_eq!(n.to_json(), doc);
    }

    #[test]
    fn deferred_syntax_is_rejected() {
        for k in DEFERRED_KEYS {
            let doc = json!({ k: 1 });
            assert!(matches!(
                Node::parse(&doc, ""),
                Err(Malformed::DeferredSyntax { .. })
            ));
        }
    }

    #[test]
    fn bad_names_are_rejected() {
        assert!(Node::parse(&json!({"$var": "2bad"}), "").is_err());
        assert!(Node::parse(&json!({"$wild": "has space"}), "").is_err());
        assert!(Node::parse(&json!({"$var": "a", "$wild": "b"}), "").is_err());
    }
}
