//! `/meta` group attributes: the constraint, the cohort map, and the column list.
//!
//! The constraint lives here, not in the extension type, because **it defines which
//! columns must exist** and a document specifying the table's whole column set
//! cannot live inside one column's type annotation (PLAN.md layout decision 5).
//! `/meta` attributes map 1:1 onto Arrow `Schema` metadata, so DataFusion gets the
//! constraint as a planner input for free.

use serde_json::{json, Map, Value};

use json_constraint::Constraint;

use crate::error::LayoutError;
use crate::schema::{Column, TableSchema, MEMBER_ID};

/// The spec version this implementation writes.
pub const VERSION: &str = "0.1";

/// The one attribute key everything lives under, so the namespace does not collide
/// with whatever a user puts in group attributes.
pub const NAMESPACE: &str = "datacollections";

/// v1 is one store = one constraint = one implicit cohort. The map is keyed from day
/// one anyway, so a second cohort is an additive change rather than a breaking one.
pub const DEFAULT_COHORT: &str = "default";

/// The Arrow extension type declared on `/meta/member_id`.
pub const EXTENSION_NAME: &str = "zarr.group_ref";

/// Everything a reader needs to open a collection.
#[derive(Clone, Debug, PartialEq)]
pub struct CollectionMetadata {
    pub version: String,
    /// cohort id -> constraint. Exactly one entry in v1.
    pub cohorts: Vec<(String, Constraint)>,
    pub schema: TableSchema,
}

impl CollectionMetadata {
    pub fn new(constraint: Constraint, extras: Vec<Column>) -> Result<Self, LayoutError> {
        let schema = TableSchema::new(DEFAULT_COHORT, &constraint, extras)?;
        Ok(CollectionMetadata {
            version: VERSION.to_string(),
            cohorts: vec![(DEFAULT_COHORT.to_string(), constraint)],
            schema,
        })
    }

    pub fn constraint(&self, cohort: &str) -> Result<&Constraint, LayoutError> {
        self.cohorts
            .iter()
            .find(|(k, _)| k == cohort)
            .map(|(_, c)| c)
            .ok_or_else(|| LayoutError::NoSuchCohort(cohort.to_string()))
    }

    /// The only cohort. v1 stores are single-cohort by construction.
    pub fn sole_constraint(&self) -> Result<&Constraint, LayoutError> {
        match self.cohorts.as_slice() {
            [(_, c)] => Ok(c),
            _ => Err(LayoutError::NotACollection(format!(
                "expected exactly one cohort, found {}",
                self.cohorts.len()
            ))),
        }
    }

    /// Serialise to the `/meta` group's attributes.
    pub fn to_attributes(&self) -> Value {
        let mut cohorts = Map::new();
        for (id, c) in &self.cohorts {
            cohorts.insert(id.clone(), json!({ "constraint": c.to_json() }));
        }
        let mut columns = Map::new();
        for c in &self.schema.columns {
            columns.insert(c.name.clone(), serde_json::to_value(c).unwrap());
        }
        json!({
            NAMESPACE: {
                "version": self.version,
                "cohorts": Value::Object(cohorts),
                "columns": Value::Object(columns),
            }
        })
    }

    pub fn from_attributes(attrs: &Value) -> Result<Self, LayoutError> {
        let root = attrs
            .get(NAMESPACE)
            .ok_or_else(|| LayoutError::NotACollection(format!("no `{NAMESPACE}` key")))?;
        let version = root
            .get("version")
            .and_then(Value::as_str)
            .ok_or_else(|| LayoutError::NotACollection("no version".into()))?;
        if version != VERSION {
            return Err(LayoutError::Version {
                found: version.to_string(),
                expected: VERSION.to_string(),
            });
        }
        let cohorts_obj = root
            .get("cohorts")
            .and_then(Value::as_object)
            .ok_or_else(|| LayoutError::NotACollection("no cohorts map".into()))?;
        let mut cohorts = Vec::new();
        for (id, entry) in cohorts_obj {
            let doc = entry.get("constraint").ok_or_else(|| {
                LayoutError::NotACollection(format!("cohort {id} has no constraint"))
            })?;
            let c = Constraint::parse(doc).map_err(|e| LayoutError::Constraint(e.to_string()))?;
            cohorts.push((id.clone(), c));
        }
        let columns_obj = root
            .get("columns")
            .and_then(Value::as_object)
            .ok_or_else(|| LayoutError::NotACollection("no columns map".into()))?;
        let mut columns = Vec::new();
        for (name, spec) in columns_obj {
            let mut col: Column = serde_json::from_value(spec.clone())
                .map_err(|e| LayoutError::NotACollection(format!("column {name}: {e}")))?;
            col.name = name.clone();
            columns.push(col);
        }
        if !columns.iter().any(|c| c.name == MEMBER_ID) {
            return Err(LayoutError::MissingColumn {
                name: MEMBER_ID.into(),
                why: "every collection has one".into(),
            });
        }
        let meta = CollectionMetadata {
            version: version.to_string(),
            cohorts,
            schema: TableSchema { columns },
        };
        // Variables ⊆ columns, checked on open as well as on write.
        for (id, c) in &meta.cohorts {
            meta.schema.check_covers(id, c)?;
        }
        Ok(meta)
    }
}

/// The `zarr.group_ref` declaration, written into `/meta/member_id`'s **array**
/// attributes so it lands where Arrow `Field` metadata does.
///
/// It carries only genuinely column-scoped things — supported storage types, the
/// id→location resolution rule, the spec version — so a routine append never mutates
/// the column's type identity while the constraint evolves per commit.
pub fn group_ref_attributes() -> Value {
    json!({
        "ARROW:extension:name": EXTENSION_NAME,
        "ARROW:extension:metadata": {
            "version": VERSION,
            "storage_types": ["utf8_view", "utf8", "large_utf8"],
            "resolve": "/groups/{id}",
        }
    })
}

/// Arrow requires `ARROW:extension:metadata` to be a *string*, but Zarr attributes
/// are JSON — so it is stored as real JSON (readable in `zarr.json`, no double
/// escaping) and stringified here, when the `Field` is constructed.
pub fn arrow_field_metadata(attrs: &Value) -> Vec<(String, String)> {
    let mut out = Vec::new();
    if let Some(obj) = attrs.as_object() {
        for (k, v) in obj {
            if !k.starts_with("ARROW:") {
                continue;
            }
            out.push((
                k.clone(),
                match v {
                    Value::String(s) => s.clone(),
                    other => serde_json::to_string(other).unwrap(),
                },
            ));
        }
    }
    out
}

/// Where a member's group lives, given its id.
pub fn group_path(member_id: &str) -> String {
    format!("/groups/{member_id}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::{Dtype, Role};
    use serde_json::json;

    fn meta() -> CollectionMetadata {
        let c = Constraint::parse(&json!({
            "nt": {"$var": "nt", "type": "integer", "minimum": 1},
            "codecs": {"$wild": "codecs"}
        }))
        .unwrap();
        CollectionMetadata::new(c, vec![Column::extra("shot", Dtype::Int64)]).unwrap()
    }

    #[test]
    fn attributes_round_trip() {
        let m = meta();
        let attrs = m.to_attributes();
        assert_eq!(CollectionMetadata::from_attributes(&attrs).unwrap(), m);
    }

    #[test]
    fn cohorts_are_a_keyed_map_from_day_one() {
        let attrs = meta().to_attributes();
        assert!(attrs[NAMESPACE]["cohorts"]["default"]["constraint"].is_object());
    }

    #[test]
    fn opening_checks_that_every_variable_has_a_column() {
        let mut attrs = meta().to_attributes();
        attrs[NAMESPACE]["columns"]
            .as_object_mut()
            .unwrap()
            .remove("nt");
        assert!(matches!(
            CollectionMetadata::from_attributes(&attrs),
            Err(LayoutError::MissingColumn { .. })
        ));
    }

    #[test]
    fn extension_metadata_is_json_in_zarr_and_a_string_in_arrow() {
        let attrs = group_ref_attributes();
        assert!(attrs["ARROW:extension:metadata"].is_object());
        let fields = arrow_field_metadata(&attrs);
        assert_eq!(fields.len(), 2);
        let md = fields
            .iter()
            .find(|(k, _)| k == "ARROW:extension:metadata")
            .unwrap();
        let parsed: Value = serde_json::from_str(&md.1).unwrap();
        assert_eq!(parsed["resolve"], "/groups/{id}");
    }

    #[test]
    fn extras_survive_the_round_trip_marked_as_extras() {
        let m = CollectionMetadata::from_attributes(&meta().to_attributes()).unwrap();
        assert_eq!(m.schema.get("shot").unwrap().role, Role::Extra);
        assert_eq!(m.schema.get("nt").unwrap().role, Role::Variable);
        assert_eq!(m.schema.get("codecs").unwrap().role, Role::Wildcard);
    }
}
