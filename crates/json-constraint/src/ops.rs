//! The three operations: `meet`, `substitute`, `subsumes`. There are no others —
//! no least-upper-bound, no anti-unification, no inference. Constraints are
//! authored, not inferred.

use std::collections::HashMap;

use serde_json::Value;

use crate::bindings::Bindings;
use crate::domain::Domain;
use crate::error::{Mismatch, MismatchKind, SubstituteError};
use crate::node::Node;

fn child_pointer(parent: &str, token: &str) -> String {
    format!("{parent}/{}", token.replace('~', "~0").replace('/', "~1"))
}

fn brief(v: &Value) -> String {
    let s = v.to_string();
    if s.len() > 120 {
        format!("{}…", &s[..117])
    } else {
        s
    }
}

// ---------------------------------------------------------------- meet

/// Unify a constraint with a concrete description, yielding this member's bindings.
///
/// Collects *every* mismatch rather than stopping at the first, because the
/// rejection message is the user-facing part of `add_item`.
pub fn meet(node: &Node, description: &Value) -> Result<Bindings, Vec<Mismatch>> {
    let mut bindings = Bindings::new();
    let mut mismatches = Vec::new();
    meet_into(node, description, "", &mut bindings, &mut mismatches);
    if mismatches.is_empty() {
        Ok(bindings)
    } else {
        Err(mismatches)
    }
}

fn meet_into(
    node: &Node,
    value: &Value,
    pointer: &str,
    bindings: &mut Bindings,
    out: &mut Vec<Mismatch>,
) {
    match node {
        Node::Literal(expected) => {
            if expected != value {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::Literal,
                    expected: brief(expected),
                    found: brief(value),
                });
            }
        }
        Node::Wild { name } => {
            bindings.insert(name.clone(), value.clone());
        }
        Node::Var { name, domain } => {
            if let Err(reason) = domain.admits(value) {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::Domain,
                    expected: format!("`{name}`: {reason}"),
                    found: brief(value),
                });
                return;
            }
            // The co-constraint: repeated use within one member asserts equality.
            if let Some(already) = bindings.get(name) {
                if already != value {
                    out.push(Mismatch {
                        pointer: pointer.to_string(),
                        kind: MismatchKind::CoConstraint,
                        expected: format!(
                            "`{name}` = {}, bound earlier in this member",
                            brief(already)
                        ),
                        found: brief(value),
                    });
                }
                return;
            }
            bindings.insert(name.clone(), value.clone());
        }
        Node::Object(fields) => {
            let Some(obj) = value.as_object() else {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::Kind,
                    expected: "an object".into(),
                    found: brief(value),
                });
                return;
            };
            // v1 has no optionality: key sets must be equal.
            let missing: Vec<&str> = fields
                .keys()
                .filter(|k| !obj.contains_key(*k))
                .map(|k| k.as_str())
                .collect();
            let extra: Vec<&str> = obj
                .keys()
                .filter(|k| !fields.contains_key(*k))
                .map(|k| k.as_str())
                .collect();
            if !missing.is_empty() || !extra.is_empty() {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::KeySet,
                    expected: format!("keys {:?}", fields.keys().collect::<Vec<_>>()),
                    found: format!("missing {missing:?}, unexpected {extra:?}"),
                });
            }
            for (k, child) in fields {
                if let Some(v) = obj.get(k) {
                    meet_into(child, v, &child_pointer(pointer, k), bindings, out);
                }
            }
        }
        Node::Array(items) => {
            let Some(arr) = value.as_array() else {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::Kind,
                    expected: "an array".into(),
                    found: brief(value),
                });
                return;
            };
            if arr.len() != items.len() {
                out.push(Mismatch {
                    pointer: pointer.to_string(),
                    kind: MismatchKind::Length,
                    expected: format!("{} elements", items.len()),
                    found: format!("{} elements", arr.len()),
                });
                return;
            }
            for (i, (child, v)) in items.iter().zip(arr).enumerate() {
                meet_into(
                    child,
                    v,
                    &child_pointer(pointer, &i.to_string()),
                    bindings,
                    out,
                );
            }
        }
    }
}

// ---------------------------------------------------------------- substitute

/// Constraint + bindings -> the member's description, reconstructed exactly.
pub fn substitute(node: &Node, bindings: &Bindings) -> Result<Value, SubstituteError> {
    Ok(match node {
        Node::Literal(v) => v.clone(),
        Node::Wild { name } => bindings
            .get(name)
            .cloned()
            .ok_or_else(|| SubstituteError::Unbound(name.clone()))?,
        Node::Var { name, domain } => {
            let v = bindings
                .get(name)
                .ok_or_else(|| SubstituteError::Unbound(name.clone()))?;
            domain
                .admits(v)
                .map_err(|reason| SubstituteError::OutOfDomain {
                    name: name.clone(),
                    reason,
                })?;
            v.clone()
        }
        Node::Object(fields) => {
            let mut m = serde_json::Map::with_capacity(fields.len());
            for (k, child) in fields {
                m.insert(k.clone(), substitute(child, bindings)?);
            }
            Value::Object(m)
        }
        Node::Array(items) => Value::Array(
            items
                .iter()
                .map(|c| substitute(c, bindings))
                .collect::<Result<_, _>>()?,
        ),
    })
}

// ---------------------------------------------------------------- subsumes

/// Does `a` generalise `b` — is every document matching `b` also matched by `a`?
///
/// A cheap structural comparison, deliberately: no synthesis, no leastness proof.
/// Returns the reason on failure so `evolve_schema` can explain its refusal.
pub fn subsumes_explain(a: &Node, b: &Node) -> Result<(), String> {
    structural_subsumes(a, b, "")?;
    co_constraints_preserved(a, b)
}

pub fn subsumes(a: &Node, b: &Node) -> bool {
    subsumes_explain(a, b).is_ok()
}

fn structural_subsumes(a: &Node, b: &Node, pointer: &str) -> Result<(), String> {
    let at = |msg: String| {
        format!(
            "at {}: {msg}",
            if pointer.is_empty() { "/" } else { pointer }
        )
    };
    match (a, b) {
        // A wildcard admits everything.
        (Node::Wild { .. }, _) => Ok(()),
        (_, Node::Wild { .. }) => Err(at(format!(
            "{} cannot generalise a wildcard",
            a.kind_name()
        ))),
        (Node::Var { name, domain }, Node::Literal(v)) => domain.admits(v).map_err(|r| {
            at(format!(
                "literal {} is outside the domain of `{name}`: {r}",
                brief(v)
            ))
        }),
        (
            Node::Var { name, domain },
            Node::Var {
                name: bname,
                domain: bdomain,
            },
        ) => {
            if domain.contains(bdomain) {
                Ok(())
            } else {
                Err(at(format!(
                    "the domain of `{bname}` is not contained in that of `{name}`"
                )))
            }
        }
        (Node::Var { name, .. }, other) => Err(at(format!(
            "`{name}` is a scalar variable and cannot generalise {}",
            other.kind_name()
        ))),
        (Node::Literal(x), Node::Literal(y)) => {
            if x == y {
                Ok(())
            } else {
                Err(at(format!(
                    "literal {} differs from {}",
                    brief(x),
                    brief(y)
                )))
            }
        }
        (Node::Literal(_), other) => Err(at(format!(
            "a literal cannot generalise {}",
            other.kind_name()
        ))),
        (Node::Object(x), Node::Object(y)) => {
            let missing: Vec<&String> = x.keys().filter(|k| !y.contains_key(*k)).collect();
            let extra: Vec<&String> = y.keys().filter(|k| !x.contains_key(*k)).collect();
            if !missing.is_empty() || !extra.is_empty() {
                return Err(at(format!(
                    "key sets differ: only in the generalisation {missing:?}, only in the specialisation {extra:?}"
                )));
            }
            for (k, av) in x {
                structural_subsumes(av, &y[k], &child_pointer(pointer, k))?;
            }
            Ok(())
        }
        (Node::Array(x), Node::Array(y)) => {
            if x.len() != y.len() {
                return Err(at(format!(
                    "array lengths differ ({} vs {}); a rank change must be a whole-leaf wildcard",
                    x.len(),
                    y.len()
                )));
            }
            for (i, (av, bv)) in x.iter().zip(y).enumerate() {
                structural_subsumes(av, bv, &child_pointer(pointer, &i.to_string()))?;
            }
            Ok(())
        }
        (x, y) => Err(at(format!(
            "{} cannot generalise {}",
            x.kind_name(),
            y.kind_name()
        ))),
    }
}

/// The one non-local check: an equality `a` asserts by repeating a variable must
/// also be forced by `b`, or `a` would reject documents `b` accepts.
fn co_constraints_preserved(a: &Node, b: &Node) -> Result<(), String> {
    let mut positions: HashMap<&str, Vec<String>> = HashMap::new();
    a.walk(String::new(), &mut |ptr, n| {
        if let Node::Var { name, .. } = n {
            positions.entry(name).or_default().push(ptr.to_string());
        }
    });
    for (name, ptrs) in positions {
        if ptrs.len() < 2 {
            continue;
        }
        let mut seen: Option<&Node> = None;
        for p in &ptrs {
            let Some(bn) = node_at(b, p) else {
                return Err(format!("no node at {p} in the specialisation"));
            };
            match seen {
                None => seen = Some(bn),
                Some(first) if first == bn => {}
                Some(_) => {
                    return Err(format!(
                        "`{name}` is repeated, asserting equality across {ptrs:?}, but the \
                         specialisation does not force those positions equal"
                    ))
                }
            }
        }
    }
    Ok(())
}

fn node_at<'a>(node: &'a Node, pointer: &str) -> Option<&'a Node> {
    let mut cur = node;
    for token in pointer.split('/').skip(1) {
        let token = token.replace("~1", "/").replace("~0", "~");
        cur = match cur {
            Node::Object(fields) => fields.get(&token)?,
            Node::Array(items) => items.get(token.parse::<usize>().ok()?)?,
            _ => return None,
        };
    }
    Some(cur)
}

/// What a variable or wildcard declares. The storage layer turns these into columns.
#[derive(Clone, Debug, PartialEq)]
pub struct Declaration {
    pub name: String,
    pub kind: DeclKind,
    pub domain: Domain,
    /// Every position the name occurs at, in document order.
    pub pointers: Vec<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeclKind {
    Variable,
    Wildcard,
}
