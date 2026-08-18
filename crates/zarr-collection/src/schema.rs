//! The table schema: which columns must exist, of what type, and how a binding is
//! encoded into a cell.
//!
//! **Variables ⊆ columns** (PLAN.md layout decision 6). Every variable and wildcard
//! of the constraint must have a column; columns beyond those are permitted and are
//! marked `extra`. The constraint is therefore a *lower bound* on the table schema,
//! not the whole of it — and "which columns must this table have?" is answerable
//! from the constraint alone, without reading a single row.

use std::collections::BTreeMap;

use json_constraint::{Constraint, DeclKind, ScalarType};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::error::LayoutError;

/// The name of the column holding member ids, and the join key to `/groups/<id>`.
pub const MEMBER_ID: &str = "member_id";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    /// The `zarr.group_ref` column.
    MemberId,
    /// A `$var` of the constraint. Recomputable from the member's group.
    Variable,
    /// A `$wild` of the constraint. Recomputable; stored JSON-encoded.
    Wildcard,
    /// Not in the constraint: provenance, query convenience, view inputs.
    /// **Not** recomputable from the group, which is why it is marked distinctly.
    Extra,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Dtype {
    Int64,
    Float64,
    Bool,
    String,
}

impl Dtype {
    /// The Zarr v3 `data_type` for a column array.
    pub fn zarr_data_type(self) -> &'static str {
        match self {
            Dtype::Int64 => "int64",
            Dtype::Float64 => "float64",
            Dtype::Bool => "bool",
            Dtype::String => "string",
        }
    }

    /// The Arrow storage type. Zarr's single `string` maps to whichever of the
    /// declared Utf8 flavours the reader picks.
    pub fn arrow_type(self) -> &'static str {
        match self {
            Dtype::Int64 => "int64",
            Dtype::Float64 => "float64",
            Dtype::Bool => "bool",
            Dtype::String => "utf8_view",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Column {
    pub name: String,
    pub role: Role,
    pub dtype: Dtype,
    /// `"json"` for wildcard columns, whose cells hold the value verbatim as JSON.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub encoding: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cohort: Option<String>,
    /// Free-form note, for extra columns whose provenance is worth recording.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// A JSON Pointer into the member's description, for a column that is still
    /// recomputable even though the current constraint no longer declares it.
    ///
    /// This is what a *retained* column carries: when `evolve_schema` stops
    /// mentioning a hole, its column is kept (tightening is free — layout decision
    /// 6) and this records where its value comes from. Without it the column would
    /// become an ordinary extra, and every later `add_item` would have to supply a
    /// value for something the caller never chose and cannot know.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_pointer: Option<String>,
}

impl Column {
    pub fn member_id() -> Self {
        Column {
            name: MEMBER_ID.into(),
            role: Role::MemberId,
            dtype: Dtype::String,
            encoding: None,
            cohort: None,
            description: None,
            source_pointer: None,
        }
    }

    pub fn extra(name: impl Into<String>, dtype: Dtype) -> Self {
        Column {
            name: name.into(),
            role: Role::Extra,
            dtype,
            encoding: None,
            cohort: None,
            description: None,
            source_pointer: None,
        }
    }

    /// Is this column's value still readable out of the member's description?
    pub fn is_retained(&self) -> bool {
        self.source_pointer.is_some()
    }

    pub fn is_json_encoded(&self) -> bool {
        self.encoding.as_deref() == Some("json")
    }

    /// Encode one member's binding into the cell that goes in this column.
    pub fn encode(&self, value: &Value) -> Result<Value, LayoutError> {
        if self.is_json_encoded() {
            return Ok(json!(serde_json::to_string(value).unwrap()));
        }
        let ok = match self.dtype {
            Dtype::Int64 => value.is_i64() || value.is_u64(),
            Dtype::Float64 => value.is_number(),
            Dtype::Bool => value.is_boolean(),
            Dtype::String => value.is_string(),
        };
        if !ok {
            return Err(LayoutError::CellType {
                column: self.name.clone(),
                dtype: self.dtype,
                found: value.to_string(),
            });
        }
        Ok(value.clone())
    }

    /// Recover the binding from a cell. Inverse of [`Column::encode`].
    pub fn decode(&self, cell: &Value) -> Result<Value, LayoutError> {
        if self.is_json_encoded() {
            let s = cell.as_str().ok_or_else(|| LayoutError::CellType {
                column: self.name.clone(),
                dtype: Dtype::String,
                found: cell.to_string(),
            })?;
            return serde_json::from_str(s).map_err(|e| LayoutError::CellDecode {
                column: self.name.clone(),
                reason: e.to_string(),
            });
        }
        Ok(cell.clone())
    }
}

/// The columns a constraint requires, in document order, plus `member_id` first.
///
/// This is the mechanical "every variable has a column" check, expressed
/// constructively so the writer and the checker cannot disagree.
pub fn required_columns(cohort: &str, constraint: &Constraint) -> Vec<Column> {
    let mut cols = vec![Column::member_id()];
    for d in constraint.declarations() {
        let (dtype, encoding) = match d.kind {
            DeclKind::Wildcard => (Dtype::String, Some("json".to_string())),
            DeclKind::Variable => (
                match d.domain.ty {
                    Some(ScalarType::Integer) => Dtype::Int64,
                    Some(ScalarType::Number) => Dtype::Float64,
                    Some(ScalarType::Boolean) => Dtype::Bool,
                    // An untyped variable can hold any scalar, so it is stored as
                    // JSON text rather than guessed at.
                    Some(ScalarType::String) => Dtype::String,
                    None => Dtype::String,
                },
                match d.domain.ty {
                    Some(ScalarType::String) => None,
                    None => Some("json".to_string()),
                    _ => None,
                },
            ),
        };
        cols.push(Column {
            name: d.name.clone(),
            role: match d.kind {
                DeclKind::Variable => Role::Variable,
                DeclKind::Wildcard => Role::Wildcard,
            },
            dtype,
            encoding,
            cohort: Some(cohort.to_string()),
            description: None,
            source_pointer: None,
        });
    }
    cols
}

/// The whole table schema: required columns plus whatever extras the user declared.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct TableSchema {
    pub columns: Vec<Column>,
}

impl TableSchema {
    pub fn new(
        cohort: &str,
        constraint: &Constraint,
        extras: Vec<Column>,
    ) -> Result<Self, LayoutError> {
        let mut columns = required_columns(cohort, constraint);
        for e in extras {
            if columns.iter().any(|c| c.name == e.name) {
                // Variable names claim the column namespace; extras must not collide.
                return Err(LayoutError::ColumnCollision { name: e.name });
            }
            columns.push(Column {
                role: Role::Extra,
                ..e
            });
        }
        Ok(TableSchema { columns })
    }

    pub fn get(&self, name: &str) -> Option<&Column> {
        self.columns.iter().find(|c| c.name == name)
    }

    pub fn names(&self) -> Vec<&str> {
        self.columns.iter().map(|c| c.name.as_str()).collect()
    }

    pub fn extras(&self) -> impl Iterator<Item = &Column> {
        self.columns.iter().filter(|c| c.role == Role::Extra)
    }

    /// Cheap well-formedness check between a constraint and a table: every variable
    /// and wildcard has a column of the right type. Runs on open and on every write.
    pub fn check_covers(&self, cohort: &str, constraint: &Constraint) -> Result<(), LayoutError> {
        for want in required_columns(cohort, constraint) {
            match self.get(&want.name) {
                None => {
                    return Err(LayoutError::MissingColumn {
                        name: want.name,
                        why: "declared by the constraint".into(),
                    })
                }
                Some(have) if have.dtype != want.dtype || have.encoding != want.encoding => {
                    return Err(LayoutError::ColumnType {
                        name: want.name,
                        expected: format!("{:?}/{:?}", want.dtype, want.encoding),
                        found: format!("{:?}/{:?}", have.dtype, have.encoding),
                    })
                }
                Some(_) => {}
            }
        }
        Ok(())
    }

    pub fn to_map(&self) -> BTreeMap<String, Value> {
        self.columns
            .iter()
            .map(|c| (c.name.clone(), serde_json::to_value(c).unwrap()))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn c(v: Value) -> Constraint {
        Constraint::parse(&v).unwrap()
    }

    #[test]
    fn every_hole_claims_a_column_of_the_right_type() {
        let cols = required_columns(
            "default",
            &c(json!({
                "nt": {"$var": "nt", "type": "integer"},
                "scale": {"$var": "scale", "type": "number"},
                "name": {"$var": "name", "type": "string"},
                "codecs": {"$wild": "codecs"}
            })),
        );
        let by: Vec<(&str, Dtype)> = cols.iter().map(|c| (c.name.as_str(), c.dtype)).collect();
        assert_eq!(
            by,
            [
                ("member_id", Dtype::String),
                ("nt", Dtype::Int64),
                ("scale", Dtype::Float64),
                ("name", Dtype::String),
                ("codecs", Dtype::String),
            ]
        );
        assert!(cols.last().unwrap().is_json_encoded());
    }

    #[test]
    fn wildcard_cells_round_trip_verbatim() {
        let col = &required_columns("default", &c(json!({"codecs": {"$wild": "codecs"}})))[1];
        let v = json!([{"name": "bytes"}, {"name": "zstd", "configuration": {"level": 3}}]);
        let cell = col.encode(&v).unwrap();
        assert!(cell.is_string());
        assert_eq!(col.decode(&cell).unwrap(), v);
    }

    #[test]
    fn extras_may_not_shadow_a_variable() {
        let constraint = c(json!({"nt": {"$var": "nt", "type": "integer"}}));
        assert!(TableSchema::new(
            "default",
            &constraint,
            vec![Column::extra("nt", Dtype::Int64)]
        )
        .is_err());
        assert!(TableSchema::new(
            "default",
            &constraint,
            vec![Column::extra("shot", Dtype::Int64)]
        )
        .is_ok());
    }

    #[test]
    fn check_covers_catches_a_table_missing_a_variables_column() {
        let old = c(json!({"a": 1}));
        let new = c(json!({"a": {"$var": "a", "type": "integer"}}));
        let table = TableSchema::new("default", &old, vec![]).unwrap();
        assert!(table.check_covers("default", &old).is_ok());
        assert!(matches!(
            table.check_covers("default", &new),
            Err(LayoutError::MissingColumn { .. })
        ));
    }
}
