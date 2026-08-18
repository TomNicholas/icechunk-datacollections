//! The two write paths, as *plans*: pure descriptions of what a transaction must
//! do, computed with no IO at all.
//!
//! Keeping them pure is what lets the storage driver (Python, over icechunk) stay
//! thin, and lets the interesting half — which columns must be created, which must
//! be backfilled, whether the evolution is even legal — be tested without a store.

use json_constraint::{Bindings, Constraint};
use serde_json::{Map, Value};

use crate::error::LayoutError;
use crate::meta::{CollectionMetadata, DEFAULT_COHORT};
use crate::schema::{Column, Role, TableSchema, MEMBER_ID};

/// One row: column name -> encoded cell.
pub type Row = Map<String, Value>;

/// What `add_item` must write. O(1) in collection size.
#[derive(Clone, Debug, PartialEq)]
pub struct AppendPlan {
    pub member_id: String,
    pub group_path: String,
    pub row: Row,
}

/// Build the row for a member that has already been `meet`-checked.
///
/// `extras` supplies values for the non-recomputable columns. **This is the MVP
/// answer to "how are extra column values supplied?"** — the caller passes them,
/// because nothing else can know them: they are by definition not derivable from the
/// member's group. See PROGRESS.md; it is a provisional choice, not a decision.
pub fn plan_append(
    meta: &CollectionMetadata,
    member_id: &str,
    bindings: &Bindings,
    extras: &Map<String, Value>,
    description: &Value,
) -> Result<AppendPlan, LayoutError> {
    let mut row = Row::new();
    row.insert(MEMBER_ID.to_string(), Value::String(member_id.to_string()));

    for col in &meta.schema.columns {
        match col.role {
            Role::MemberId => {}
            Role::Variable | Role::Wildcard => {
                let v = bindings
                    .get(&col.name)
                    .ok_or_else(|| LayoutError::MissingColumn {
                        name: col.name.clone(),
                        why: "declared by the constraint but not bound by this member".into(),
                    })?;
                row.insert(col.name.clone(), col.encode(v)?);
            }
            Role::Extra => {
                // A *retained* column — one demoted from a variable by a later
                // constraint — is still recomputable, so the caller is not asked for
                // it. Only genuinely caller-supplied extras are.
                let v = match &col.source_pointer {
                    Some(pointer) => description.pointer(pointer).cloned().ok_or_else(|| {
                        LayoutError::MissingColumn {
                            name: col.name.clone(),
                            why: format!("retained column expects a value at {pointer}"),
                        }
                    })?,
                    None => extras
                        .get(&col.name)
                        .cloned()
                        .ok_or_else(|| LayoutError::MissingExtraValue(col.name.clone()))?,
                };
                row.insert(col.name.clone(), col.encode(&v)?);
            }
        }
    }
    for name in extras.keys() {
        if meta.schema.get(name).is_none() {
            return Err(LayoutError::UnknownColumn(name.clone()));
        }
    }

    Ok(AppendPlan {
        member_id: member_id.to_string(),
        group_path: crate::meta::group_path(member_id),
        row,
    })
}

/// What `evolve_schema` must do. **O(N) in existing collection size** whenever
/// `backfill` is non-empty, and the API says so rather than hiding it: writers see
/// bimodal latency and should be told which mode they are in.
#[derive(Clone, Debug, PartialEq)]
pub struct EvolvePlan {
    pub new_metadata: CollectionMetadata,
    /// Columns to create.
    pub created: Vec<Column>,
    /// Columns whose values must be computed for **every existing member** by
    /// re-reading that member's group metadata. Real values; no nulls are involved,
    /// because a variable is by construction a hole in the group description.
    pub backfill: Vec<String>,
    /// Columns that were variables and no longer are. Kept, demoted to extras, so
    /// tightening costs no data migration.
    pub demoted: Vec<String>,
}

impl EvolvePlan {
    /// Is this the cheap kind of evolution?
    pub fn is_o1(&self) -> bool {
        self.backfill.is_empty()
    }
}

pub fn plan_evolve(
    meta: &CollectionMetadata,
    new_constraint: &Constraint,
) -> Result<EvolvePlan, LayoutError> {
    let current = meta.sole_constraint()?;

    // Evolution is monotonic: a new constraint may only loosen. Tightening would
    // require re-validating every existing member, and is a separate operation if we
    // ever want one.
    new_constraint
        .subsumes_explain(current)
        .map_err(LayoutError::NotMonotonic)?;

    let required = crate::schema::required_columns(DEFAULT_COHORT, new_constraint);

    let mut columns: Vec<Column> = Vec::new();
    let mut created = Vec::new();
    let mut backfill = Vec::new();

    for want in &required {
        match meta.schema.get(&want.name) {
            Some(have) if have.dtype == want.dtype && have.encoding == want.encoding => {
                // Already present — either it was a variable before, or it was an
                // extra column of exactly the right shape, which is the "tightening
                // is free" case run backwards.
                columns.push(want.clone());
            }
            Some(_) => {
                // Present, but of the wrong type — widening `{"$var": "a", "type":
                // "integer"}` to `{"$wild": "a"}` turns an int64 column into a
                // JSON-encoded string one. The column is recreated and backfilled,
                // which is sound because a backfill recomputes every value anyway.
                columns.push(want.clone());
                created.push(want.clone());
                backfill.push(want.name.clone());
            }
            None => {
                columns.push(want.clone());
                created.push(want.clone());
                if want.role != Role::MemberId {
                    backfill.push(want.name.clone());
                }
            }
        }
    }

    // Anything the old schema had that the new constraint does not require survives
    // as an extra column.
    let mut demoted = Vec::new();
    for old in &meta.schema.columns {
        if columns.iter().any(|c| c.name == old.name) {
            continue;
        }
        let was_constraint_column = matches!(old.role, Role::Variable | Role::Wildcard);
        let mut retained = Column {
            role: Role::Extra,
            cohort: None,
            ..old.clone()
        };
        if was_constraint_column {
            demoted.push(old.name.clone());
            // Keep the column *and* keep it recomputable: record the position its
            // value came from, so later appends can still fill it without the caller
            // being asked for something they never chose.
            retained.source_pointer = current
                .declaration(&old.name)
                .and_then(|d| d.pointers.first().cloned());
        }
        columns.push(retained);
    }

    let new_metadata = CollectionMetadata {
        version: meta.version.clone(),
        cohorts: vec![(DEFAULT_COHORT.to_string(), new_constraint.clone())],
        schema: TableSchema { columns },
    };
    new_metadata
        .schema
        .check_covers(DEFAULT_COHORT, new_constraint)?;

    Ok(EvolvePlan {
        new_metadata,
        created,
        backfill,
        demoted,
    })
}

/// The bindings of an existing row, for `substitute`. Reads **only** variable and
/// wildcard columns: derivability is unaffected by however many extra columns a
/// table carries.
pub fn bindings_from_row(schema: &TableSchema, row: &Row) -> Result<Bindings, LayoutError> {
    let mut b = Bindings::new();
    for col in &schema.columns {
        if !matches!(col.role, Role::Variable | Role::Wildcard) {
            continue;
        }
        let cell = row
            .get(&col.name)
            .ok_or_else(|| LayoutError::MissingColumn {
                name: col.name.clone(),
                why: "absent from this row".into(),
            })?;
        b.insert(col.name.clone(), col.decode(cell)?);
    }
    Ok(b)
}

/// A member's description, reconstructed from the constraint and its row. This is
/// the derivability claim in one function.
pub fn describe(meta: &CollectionMetadata, row: &Row) -> Result<Value, LayoutError> {
    let constraint = meta.sole_constraint()?;
    let bindings = bindings_from_row(&meta.schema, row)?;
    constraint
        .substitute(&bindings)
        .map_err(|e| LayoutError::Constraint(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::Dtype;
    use serde_json::json;

    fn constraint(v: Value) -> Constraint {
        Constraint::parse(&v).unwrap()
    }

    fn collection() -> CollectionMetadata {
        CollectionMetadata::new(
            constraint(json!({"shape": [{"$var": "nt", "type": "integer", "minimum": 1}], "codecs": {"$wild": "codecs"}})),
            vec![Column::extra("shot", Dtype::Int64)],
        )
        .unwrap()
    }

    #[test]
    fn append_builds_one_row_per_member() {
        let meta = collection();
        let member = json!({"shape": [42], "codecs": [{"name": "bytes"}]});
        let bindings = meta.sole_constraint().unwrap().meet(&member).unwrap();
        let plan = plan_append(
            &meta,
            "0123456789abcdef0123456789abcdef",
            &bindings,
            json!({"shot": 30420}).as_object().unwrap(),
            &member,
        )
        .unwrap();
        assert_eq!(plan.group_path, "/groups/0123456789abcdef0123456789abcdef");
        assert_eq!(plan.row["nt"], json!(42));
        assert_eq!(plan.row["shot"], json!(30420));
        assert_eq!(plan.row["codecs"], json!("[{\"name\":\"bytes\"}]"));
        // and the row reconstructs the member exactly
        assert_eq!(describe(&meta, &plan.row).unwrap(), member);
    }

    #[test]
    fn extra_columns_must_be_supplied_and_must_exist() {
        let meta = collection();
        let member = json!({"shape": [42], "codecs": []});
        let b = meta.sole_constraint().unwrap().meet(&member).unwrap();
        assert!(matches!(
            plan_append(&meta, "x", &b, &Map::new(), &member),
            Err(LayoutError::MissingExtraValue(_))
        ));
        assert!(matches!(
            plan_append(
                &meta,
                "x",
                &b,
                json!({"shot": 1, "nope": 2}).as_object().unwrap(),
                &member
            ),
            Err(LayoutError::UnknownColumn(_))
        ));
    }

    #[test]
    fn evolution_must_loosen() {
        let meta = CollectionMetadata::new(constraint(json!({"a": 1, "b": 2})), vec![]).unwrap();
        let looser = constraint(json!({"a": {"$var": "a", "type": "integer"}, "b": 2}));
        let tighter = constraint(json!({"a": 1, "b": 1}));

        let plan = plan_evolve(&meta, &looser).unwrap();
        assert_eq!(plan.backfill, ["a"]);
        assert!(!plan.is_o1());

        assert!(matches!(
            plan_evolve(&meta, &tighter),
            Err(LayoutError::NotMonotonic(_))
        ));
    }

    #[test]
    fn widening_a_variable_to_a_wildcard_recreates_its_column() {
        let meta = CollectionMetadata::new(
            constraint(json!({"a": {"$var": "a", "type": "integer"}})),
            vec![],
        )
        .unwrap();
        assert_eq!(meta.schema.get("a").unwrap().dtype, Dtype::Int64);

        let plan = plan_evolve(&meta, &constraint(json!({"a": {"$wild": "a"}}))).unwrap();
        assert!(plan.backfill.contains(&"a".to_string()));
        let col = plan.new_metadata.schema.get("a").unwrap();
        assert_eq!(col.role, Role::Wildcard);
        assert!(col.is_json_encoded());
    }

    #[test]
    fn a_retained_column_is_filled_from_the_description_not_from_the_caller() {
        // The trap this closes: without a source pointer, a column demoted by an
        // evolution becomes an ordinary extra, and every later append demands a value
        // for something the caller never chose and cannot know.
        let meta = CollectionMetadata::new(
            constraint(json!({"campaign": {"$var": "campaign", "type": "string"}})),
            vec![],
        )
        .unwrap();
        let plan = plan_evolve(
            &meta,
            &constraint(json!({"campaign": {"$var": "campaign_v2", "type": "string"}})),
        )
        .unwrap();
        assert_eq!(plan.demoted, ["campaign"]);
        let retained = plan.new_metadata.schema.get("campaign").unwrap();
        assert!(retained.is_retained());
        assert_eq!(retained.source_pointer.as_deref(), Some("/campaign"));

        let member = json!({"campaign": "M09"});
        let bindings = plan
            .new_metadata
            .sole_constraint()
            .unwrap()
            .meet(&member)
            .unwrap();
        let append = plan_append(&plan.new_metadata, "x", &bindings, &Map::new(), &member).unwrap();
        assert_eq!(append.row["campaign"], json!("M09"));
        assert_eq!(append.row["campaign_v2"], json!("M09"));
    }

    #[test]
    fn a_variable_the_new_constraint_drops_survives_as_an_extra_column() {
        // Not reachable by `subsumes` in one step — a constraint that stops mentioning
        // a hole is not a loosening — so this exercises the plan directly. What
        // matters is that the column is kept, so no data is thrown away.
        let meta = CollectionMetadata::new(
            constraint(json!({"a": {"$var": "a", "type": "integer"}, "b": 1})),
            vec![],
        )
        .unwrap();
        let plan = plan_evolve(&meta, &constraint(json!({"a": {"$wild": "w"}, "b": 1}))).unwrap();
        assert_eq!(plan.demoted, ["a"]);
        assert_eq!(plan.new_metadata.schema.get("a").unwrap().role, Role::Extra);
        assert_eq!(
            plan.new_metadata.schema.get("w").unwrap().role,
            Role::Wildcard
        );
    }

    #[test]
    fn an_evolution_needing_no_new_column_is_cheap() {
        let meta = CollectionMetadata::new(
            constraint(json!({"a": {"$var": "a", "type": "integer", "minimum": 5}})),
            vec![],
        )
        .unwrap();
        let plan = plan_evolve(
            &meta,
            &constraint(json!({"a": {"$var": "a", "type": "integer", "minimum": 0}})),
        )
        .unwrap();
        assert!(plan.is_o1());
        assert!(plan.created.is_empty());
    }
}
