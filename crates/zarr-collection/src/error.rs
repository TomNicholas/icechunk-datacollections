use thiserror::Error;

use crate::schema::Dtype;

#[derive(Debug, Error, PartialEq)]
pub enum LayoutError {
    #[error("unsupported store version `{found}`; this implementation writes {expected}")]
    Version { found: String, expected: String },

    #[error("`/meta` attributes are not a DataCollections table: {0}")]
    NotACollection(String),

    #[error("no cohort `{0}` in this store")]
    NoSuchCohort(String),

    #[error("column `{name}` is missing ({why})")]
    MissingColumn { name: String, why: String },

    #[error("column `{name}` has type {found}, but the constraint requires {expected}")]
    ColumnType {
        name: String,
        expected: String,
        found: String,
    },

    #[error("`{name}` is already a column; variable names claim the column namespace")]
    ColumnCollision { name: String },

    #[error("column `{column}` is {dtype:?}, but the value is {found}")]
    CellType {
        column: String,
        dtype: Dtype,
        found: String,
    },

    #[error("column `{column}`: {reason}")]
    CellDecode { column: String, reason: String },

    #[error("no value supplied for extra column `{0}`; extra columns are not recomputable from the member's group, so they must be passed to add_item")]
    MissingExtraValue(String),

    #[error("`{0}` is not a column of this table")]
    UnknownColumn(String),

    #[error("the constraint is malformed: {0}")]
    Constraint(String),

    #[error("evolve_schema refused: the new constraint does not generalise the current one — {0}")]
    NotMonotonic(String),
}
