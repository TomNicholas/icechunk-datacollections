//! The DataCollections store layout.
//!
//! A store is one Icechunk repository holding a metadata table **and** the groups it
//! describes, so that a member and its row land in one atomic commit. That
//! same-store coupling is the thesis, not a convenience: it is what makes
//! `evolve_schema`'s backfill a local read rather than N reads across the internet,
//! and it is the whole answer to the synchronisation problem that motivates the
//! project.
//!
//! ```text
//! /meta                    group — the table; its attributes hold the constraint
//! /meta/member_id          the zarr.group_ref column, and the join key
//! /meta/<column>           one 1D array per column
//! /groups/<member_id>      one group per member, with consolidated metadata
//! ```
//!
//! **Scope note.** This crate is the layout *logic*: which columns must exist, what
//! the attributes say, what a write must do. It performs no IO and depends on
//! neither zarrs nor icechunk — the storage driver lives in `python/datacollections`,
//! where the ingest ecosystem is. See `IMPLEMENTATION.md`.

pub mod error;
pub mod ids;
pub mod meta;
pub mod plan;
pub mod schema;

pub use error::LayoutError;
pub use ids::{IdGenerator, RandomIds, SeededIds};
pub use meta::{
    arrow_field_metadata, group_path, group_ref_attributes, CollectionMetadata, DEFAULT_COHORT,
    EXTENSION_NAME, NAMESPACE, VERSION,
};
pub use plan::{
    bindings_from_row, describe, plan_append, plan_evolve, AppendPlan, EvolvePlan, Row,
};
pub use schema::{required_columns, Column, Dtype, Role, TableSchema, MEMBER_ID};
