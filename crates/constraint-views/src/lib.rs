//! Views: constraint + one member's bindings, out to some other format.
//!
//! STAC is **one optional view over the data, not the storage format** — this crate
//! is where that claim is cashed. It calls `substitute` to obtain the member's
//! description, then maps that through a declared view mapping. It depends on
//! neither zarrs nor arrow, because by the time it runs everything it needs is plain
//! JSON. Hence the name: it would serve anyone deriving STAC Items from a constraint
//! with no Zarr stack at all.
//!
//! The mapping is itself a JSON document — a template of the target format with two
//! kinds of hole:
//!
//! ```json
//! {
//!   "type": "Feature",
//!   "id": {"$from": "column:member_id"},
//!   "properties": {
//!     "datetime": {"$from": "column:datetime"},
//!     "proj:epsg": {"$from": "description:/attributes/proj:epsg"}
//!   }
//! }
//! ```
//!
//! `column:` reads a column — variable, wildcard **or extra**; a STAC Item's
//! `datetime` and `bbox` are very often extra columns rather than variables.
//! `description:` reads a JSON Pointer into the reconstructed description. Do not
//! conflate the two directions: `substitute` reads only variable columns, so
//! derivability is unaffected by extra columns, while a *view* may read anything.

use serde_json::{Map, Value};
use thiserror::Error;

pub mod stac;

#[derive(Debug, Error, PartialEq)]
pub enum ViewError {
    #[error("no column `{0}` in this table")]
    UnknownColumn(String),

    #[error("no value at description pointer `{0}`")]
    NoSuchPointer(String),

    #[error("`$from` must be `column:<name>` or `description:<json-pointer>`, found `{0}`")]
    BadSource(String),

    #[error("$join takes an array of parts, each rendering to a scalar; got {0}")]
    BadJoin(String),

    #[error("view `{0}` is malformed: {1}")]
    Malformed(String, String),
}

/// A declared projection of constraint + bindings into a target format.
#[derive(Clone, Debug, PartialEq)]
pub struct ViewMapping {
    pub name: String,
    pub template: Value,
}

impl ViewMapping {
    pub fn new(name: impl Into<String>, template: Value) -> Self {
        ViewMapping {
            name: name.into(),
            template,
        }
    }

    /// Parse `{"name": "...", "template": {...}}`.
    pub fn parse(doc: &Value) -> Result<Self, ViewError> {
        let name = doc
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| ViewError::Malformed("<unnamed>".into(), "no `name`".into()))?;
        let template = doc
            .get("template")
            .ok_or_else(|| ViewError::Malformed(name.into(), "no `template`".into()))?;
        Ok(ViewMapping::new(name, template.clone()))
    }

    pub fn to_json(&self) -> Value {
        serde_json::json!({"name": self.name, "template": self.template})
    }

    /// Render one member.
    ///
    /// `description` is the output of `substitute` — the member's `zarr.json`,
    /// reconstructed. `columns` is that member's row, decoded.
    pub fn render(
        &self,
        description: &Value,
        columns: &Map<String, Value>,
    ) -> Result<Value, ViewError> {
        render_node(&self.template, description, columns)
    }

    /// Which columns this view reads. Useful to a query planner: a STAC search only
    /// needs to fetch these, not the whole row.
    pub fn columns_read(&self) -> Vec<String> {
        let mut out = Vec::new();
        collect_columns(&self.template, &mut out);
        out
    }
}

fn collect_columns(node: &Value, out: &mut Vec<String>) {
    match node {
        Value::Object(m) => {
            if let Some(Value::String(src)) = m.get("$from") {
                if let Some(name) = src.strip_prefix("column:") {
                    if !out.iter().any(|c| c == name) {
                        out.push(name.to_string());
                    }
                }
                return;
            }
            for v in m.values() {
                collect_columns(v, out);
            }
        }
        Value::Array(items) => items.iter().for_each(|v| collect_columns(v, out)),
        _ => {}
    }
}

fn render_node(
    node: &Value,
    description: &Value,
    columns: &Map<String, Value>,
) -> Result<Value, ViewError> {
    match node {
        Value::Object(m) => {
            if let Some(src) = m.get("$from") {
                let src = src
                    .as_str()
                    .ok_or_else(|| ViewError::BadSource(src.to_string()))?;
                return resolve(src, description, columns);
            }
            if let Some(parts) = m.get("$join") {
                let parts = parts
                    .as_array()
                    .ok_or_else(|| ViewError::BadJoin(parts.to_string()))?;
                let mut s = String::new();
                for p in parts {
                    match render_node(p, description, columns)? {
                        Value::String(x) => s.push_str(&x),
                        Value::Number(n) => s.push_str(&n.to_string()),
                        Value::Bool(b) => s.push_str(if b { "true" } else { "false" }),
                        other => return Err(ViewError::BadJoin(other.to_string())),
                    }
                }
                return Ok(Value::String(s));
            }
            if let Some(v) = m.get("$literal") {
                return Ok(v.clone());
            }
            let mut out = Map::with_capacity(m.len());
            for (k, v) in m {
                let rendered = render_node(v, description, columns)?;
                // A view hole that resolves to null drops its key, which is how a
                // STAC Item omits an optional property rather than carrying a null.
                if rendered.is_null() && is_hole(v) {
                    continue;
                }
                out.insert(k.clone(), rendered);
            }
            Ok(Value::Object(out))
        }
        Value::Array(items) => Ok(Value::Array(
            items
                .iter()
                .map(|v| render_node(v, description, columns))
                .collect::<Result<_, _>>()?,
        )),
        scalar => Ok(scalar.clone()),
    }
}

fn is_hole(node: &Value) -> bool {
    node.as_object()
        .map(|m| m.contains_key("$from") || m.contains_key("$join"))
        .unwrap_or(false)
}

fn resolve(
    src: &str,
    description: &Value,
    columns: &Map<String, Value>,
) -> Result<Value, ViewError> {
    if let Some(name) = src.strip_prefix("column:") {
        return columns
            .get(name)
            .cloned()
            .ok_or_else(|| ViewError::UnknownColumn(name.to_string()));
    }
    if let Some(pointer) = src.strip_prefix("description:") {
        return description
            .pointer(pointer)
            .cloned()
            .ok_or_else(|| ViewError::NoSuchPointer(pointer.to_string()));
    }
    Err(ViewError::BadSource(src.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn columns() -> Map<String, Value> {
        json!({"member_id": "abc", "datetime": "2024-01-01T00:00:00Z", "nt": 42})
            .as_object()
            .unwrap()
            .clone()
    }

    fn description() -> Value {
        json!({"attributes": {"proj:epsg": 32633}, "consolidated_metadata": {"metadata": {"B04": {"shape": [10980, 10980]}}}})
    }

    #[test]
    fn holes_read_columns_and_the_description() {
        let v = ViewMapping::new(
            "t",
            json!({
                "id": {"$from": "column:member_id"},
                "epsg": {"$from": "description:/attributes/proj:epsg"},
                "shape": {"$from": "description:/consolidated_metadata/metadata/B04/shape"},
                "fixed": "literal"
            }),
        );
        let out = v.render(&description(), &columns()).unwrap();
        assert_eq!(out["id"], "abc");
        assert_eq!(out["epsg"], 32633);
        assert_eq!(out["shape"], json!([10980, 10980]));
        assert_eq!(out["fixed"], "literal");
    }

    #[test]
    fn join_builds_a_human_meaningful_id() {
        let v = ViewMapping::new(
            "t",
            json!({"id": {"$join": ["S2-", {"$from": "column:nt"}, "-", {"$from": "column:member_id"}]}}),
        );
        assert_eq!(
            v.render(&description(), &columns()).unwrap()["id"],
            "S2-42-abc"
        );
    }

    #[test]
    fn columns_read_is_the_projection_a_planner_needs() {
        let v = ViewMapping::new(
            "t",
            json!({"a": {"$from": "column:member_id"}, "b": [{"$from": "column:datetime"}]}),
        );
        assert_eq!(v.columns_read(), ["member_id", "datetime"]);
    }

    #[test]
    fn missing_sources_are_errors_not_silence() {
        let v = ViewMapping::new("t", json!({"a": {"$from": "column:nope"}}));
        assert!(matches!(
            v.render(&description(), &columns()),
            Err(ViewError::UnknownColumn(_))
        ));
        let v = ViewMapping::new("t", json!({"a": {"$from": "description:/nope"}}));
        assert!(matches!(
            v.render(&description(), &columns()),
            Err(ViewError::NoSuchPointer(_))
        ));
    }
}
