//! Python bindings.
//!
//! Deliberately thin, and deliberately JSON-in/JSON-out: the Rust crates own the
//! constraint algebra and the layout decisions, Python owns the IO and the ingest
//! ecosystem (xarray, VirtualiZarr, icechunk, zarr-python). Passing JSON text across
//! the boundary costs a negligible amount at these document sizes and keeps the
//! binding layer free of a type-conversion crate that would have to track two
//! object models.
//!
//! The ergonomic surface — `create_collection`, `add_item`, `evolve_schema`,
//! `check` — is Python, in `python/datacollections/`. This module is what it calls.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use serde_json::{Map, Value};

use constraint_views::{stac, ViewMapping};
use json_constraint::{Bindings, Constraint};
use zarr_collection::{
    bindings_from_row as rs_bindings_from_row, describe as rs_describe, ids::IdGenerator,
    plan_append as rs_plan_append, plan_evolve as rs_plan_evolve, CollectionMetadata, Column,
    Dtype, RandomIds, Role, SeededIds,
};

fn parse_json(text: &str, what: &str) -> PyResult<Value> {
    serde_json::from_str(text).map_err(|e| PyValueError::new_err(format!("{what}: {e}")))
}

fn dump(v: &Value) -> String {
    serde_json::to_string(v).expect("serialisable")
}

fn constraint(text: &str) -> PyResult<Constraint> {
    let doc = parse_json(text, "constraint document")?;
    Constraint::parse(&doc).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn metadata(text: &str) -> PyResult<CollectionMetadata> {
    let attrs = parse_json(text, "/meta attributes")?;
    CollectionMetadata::from_attributes(&attrs).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn object(text: &str, what: &str) -> PyResult<Map<String, Value>> {
    match parse_json(text, what)? {
        Value::Object(m) => Ok(m),
        other => Err(PyValueError::new_err(format!(
            "{what} must be an object, found {other}"
        ))),
    }
}

// ------------------------------------------------------------------ constraint

/// Check a constraint document and return it normalised.
#[pyfunction]
fn constraint_check(document: &str) -> PyResult<String> {
    Ok(dump(&constraint(document)?.to_json()))
}

/// The all-literal constraint admitting exactly this description — what
/// `create_collection(constraint=None)` uses on the first member.
#[pyfunction]
fn constraint_from_description(description: &str) -> PyResult<String> {
    let d = parse_json(description, "description")?;
    Ok(dump(&Constraint::from_description(&d).to_json()))
}

/// Every variable and wildcard, with its kind, domain and use sites.
#[pyfunction]
fn constraint_declarations(document: &str) -> PyResult<String> {
    let c = constraint(document)?;
    let decls: Vec<Value> = c
        .declarations()
        .iter()
        .map(|d| {
            serde_json::json!({
                "name": d.name,
                "kind": match d.kind {
                    json_constraint::DeclKind::Variable => "variable",
                    json_constraint::DeclKind::Wildcard => "wildcard",
                },
                "domain": Value::Object(d.domain.to_map()),
                "pointers": d.pointers,
            })
        })
        .collect();
    Ok(dump(&Value::Array(decls)))
}

/// Is `description` a member? Returns its bindings, or raises with the specific
/// leaf that failed — which is the whole user-facing value of a rejection.
#[pyfunction]
fn meet(document: &str, description: &str) -> PyResult<String> {
    let c = constraint(document)?;
    let d = parse_json(description, "description")?;
    match c.meet(&d) {
        Ok(b) => Ok(dump(&b.to_json())),
        Err(e) => Err(PyValueError::new_err(e.to_string())),
    }
}

/// The mismatches, as data rather than a message — for `check(ds)`.
#[pyfunction]
fn mismatches(document: &str, description: &str) -> PyResult<String> {
    let c = constraint(document)?;
    let d = parse_json(description, "description")?;
    let out: Vec<Value> = match c.meet(&d) {
        Ok(_) => vec![],
        Err(e) => e
            .mismatches()
            .iter()
            .map(|m| {
                serde_json::json!({
                    "pointer": m.pointer,
                    "kind": format!("{:?}", m.kind).to_lowercase(),
                    "expected": m.expected,
                    "found": m.found,
                    "message": m.to_string(),
                })
            })
            .collect(),
    };
    Ok(dump(&Value::Array(out)))
}

#[pyfunction]
fn substitute(document: &str, bindings: &str) -> PyResult<String> {
    let c = constraint(document)?;
    let b = Bindings::from_json(&parse_json(bindings, "bindings")?)
        .ok_or_else(|| PyValueError::new_err("bindings must be an object"))?;
    c.substitute(&b)
        .map(|v| dump(&v))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Does `a` generalise `b`? `None` if it does, otherwise the reason.
#[pyfunction]
fn subsumes_explain(a: &str, b: &str) -> PyResult<Option<String>> {
    Ok(constraint(a)?.subsumes_explain(&constraint(b)?).err())
}

// ---------------------------------------------------------------------- layout

fn parse_extras(extras: &str) -> PyResult<Vec<Column>> {
    let list = match parse_json(extras, "extra columns")? {
        Value::Array(a) => a,
        other => {
            return Err(PyValueError::new_err(format!(
                "extra columns must be a list, found {other}"
            )))
        }
    };
    list.into_iter()
        .map(|spec| {
            let name = spec
                .get("name")
                .and_then(Value::as_str)
                .ok_or_else(|| PyValueError::new_err("an extra column needs a name"))?;
            let dtype = match spec
                .get("dtype")
                .and_then(Value::as_str)
                .unwrap_or("string")
            {
                "int64" => Dtype::Int64,
                "float64" => Dtype::Float64,
                "bool" => Dtype::Bool,
                "string" => Dtype::String,
                other => {
                    return Err(PyValueError::new_err(format!(
                        "unsupported dtype `{other}` for extra column `{name}`"
                    )))
                }
            };
            Ok(Column {
                name: name.to_string(),
                role: Role::Extra,
                dtype,
                encoding: spec
                    .get("encoding")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                cohort: None,
                description: spec
                    .get("description")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                source_pointer: None,
            })
        })
        .collect()
}

/// Build the `/meta` group attributes for a new collection.
#[pyfunction]
fn metadata_new(document: &str, extras: &str) -> PyResult<String> {
    let c = constraint(document)?;
    let extras = parse_extras(extras)?;
    let m = CollectionMetadata::new(c, extras).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(dump(&m.to_attributes()))
}

/// Read `/meta` attributes, checking that every variable has a column.
#[pyfunction]
fn metadata_read(attributes: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    Ok(dump(&serde_json::json!({
        "version": m.version,
        "constraint": m.sole_constraint().map_err(|e| PyValueError::new_err(e.to_string()))?.to_json(),
        "columns": m.schema.columns.iter().map(|c| serde_json::to_value(c).unwrap()).collect::<Vec<_>>(),
    })))
}

/// The `zarr.group_ref` attributes for `/meta/member_id`.
#[pyfunction]
fn group_ref_attributes() -> String {
    dump(&zarr_collection::group_ref_attributes())
}

/// Arrow `Field` metadata, with `ARROW:extension:metadata` stringified as Arrow
/// requires — it is stored as real JSON in Zarr, and flattened only here.
#[pyfunction]
fn arrow_field_metadata(attributes: &str) -> PyResult<String> {
    let attrs = parse_json(attributes, "array attributes")?;
    let pairs: Map<String, Value> = zarr_collection::arrow_field_metadata(&attrs)
        .into_iter()
        .map(|(k, v)| (k, Value::String(v)))
        .collect();
    Ok(dump(&Value::Object(pairs)))
}

#[pyfunction]
#[pyo3(signature = (attributes, member_id, bindings, description, extras="{}"))]
fn plan_append(
    attributes: &str,
    member_id: &str,
    bindings: &str,
    description: &str,
    extras: &str,
) -> PyResult<String> {
    let m = metadata(attributes)?;
    let b = Bindings::from_json(&parse_json(bindings, "bindings")?)
        .ok_or_else(|| PyValueError::new_err("bindings must be an object"))?;
    let extras = object(extras, "extra column values")?;
    // Retained columns — demoted from a variable by an earlier evolution — read their
    // value straight out of the description, so the caller is not asked for them.
    let description = parse_json(description, "description")?;
    let plan = rs_plan_append(&m, member_id, &b, &extras, &description)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(dump(&serde_json::json!({
        "member_id": plan.member_id,
        "group_path": plan.group_path,
        "row": Value::Object(plan.row),
    })))
}

/// What `evolve_schema` must do, including which columns need an O(N) backfill.
#[pyfunction]
fn plan_evolve(attributes: &str, new_constraint: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    let c = constraint(new_constraint)?;
    let plan = rs_plan_evolve(&m, &c).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(dump(&serde_json::json!({
        "attributes": plan.new_metadata.to_attributes(),
        "created": plan.created.iter().map(|c| serde_json::to_value(c).unwrap()).collect::<Vec<_>>(),
        "backfill": plan.backfill,
        "demoted": plan.demoted,
        "is_o1": plan.is_o1(),
    })))
}

/// A member's description, reconstructed from the constraint and its row. Reads
/// only the variable and wildcard columns.
#[pyfunction]
fn describe(attributes: &str, row: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    let row = object(row, "row")?;
    rs_describe(&m, &row)
        .map(|v| dump(&v))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn bindings_from_row(attributes: &str, row: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    let row = object(row, "row")?;
    rs_bindings_from_row(&m.schema, &row)
        .map(|b| dump(&b.to_json()))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Encode one binding or extra value into its column's cell.
#[pyfunction]
fn encode_cell(attributes: &str, column: &str, value: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    let col = m
        .schema
        .get(column)
        .ok_or_else(|| PyValueError::new_err(format!("no column `{column}`")))?;
    col.encode(&parse_json(value, "value")?)
        .map(|v| dump(&v))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pyfunction]
fn decode_cell(attributes: &str, column: &str, cell: &str) -> PyResult<String> {
    let m = metadata(attributes)?;
    let col = m
        .schema
        .get(column)
        .ok_or_else(|| PyValueError::new_err(format!("no column `{column}`")))?;
    col.decode(&parse_json(cell, "cell")?)
        .map(|v| dump(&v))
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Generate member ids. `seed` makes them reproducible, which is what lets a test
/// assert an incremental build is byte-equivalent to a from-scratch one.
#[pyfunction]
#[pyo3(signature = (n, seed=None))]
fn generate_ids(n: usize, seed: Option<u64>) -> Vec<String> {
    match seed {
        Some(s) => {
            let mut g = SeededIds::new(s);
            (0..n).map(|_| g.next_id()).collect()
        }
        None => {
            let mut g = RandomIds::default();
            (0..n).map(|_| g.next_id()).collect()
        }
    }
}

// ----------------------------------------------------------------------- views

#[pyfunction]
fn render_view(mapping: &str, description: &str, columns: &str) -> PyResult<String> {
    let m = ViewMapping::parse(&parse_json(mapping, "view mapping")?)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let d = parse_json(description, "description")?;
    let cols = object(columns, "columns")?;
    m.render(&d, &cols)
        .map(|v| dump(&v))
        .map_err(|e| PyRuntimeError::new_err(e.to_string()))
}

#[pyfunction]
fn view_columns_read(mapping: &str) -> PyResult<Vec<String>> {
    let m = ViewMapping::parse(&parse_json(mapping, "view mapping")?)
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(m.columns_read())
}

/// Build the STAC Item view. Nothing about it is privileged — it is an ordinary
/// mapping document, and a domain with no STAC vocabulary writes its own.
#[pyfunction]
fn stac_item_mapping(config: &str) -> PyResult<String> {
    let cfg = parse_json(config, "STAC item view config")?;
    let get = |k: &str| cfg.get(k).cloned();
    let mut c = stac::ItemViewConfig::new(
        cfg.get("collection")
            .and_then(Value::as_str)
            .unwrap_or("collection"),
        get("id").ok_or_else(|| PyValueError::new_err("an item view needs an `id` source"))?,
        get("datetime")
            .ok_or_else(|| PyValueError::new_err("an item view needs a `datetime` source"))?,
    );
    if let Some(b) = get("bbox") {
        c = c.with_bbox(b);
    }
    if let Some(g) = get("geometry") {
        c = c.with_geometry(g);
    }
    if let Some(Value::Object(props)) = get("properties") {
        for (k, v) in props {
            c = c.with_property(k, v);
        }
    }
    if let Some(a) = get("assets") {
        c.assets = a;
    }
    Ok(dump(&stac::item_mapping(&c).to_json()))
}

/// A STAC Collection derived from the constraint: the variable domains *are* the
/// summaries, so no extra authoring is needed.
#[pyfunction]
fn stac_collection(id: &str, description: &str, document: &str) -> PyResult<String> {
    Ok(dump(&stac::collection_document(
        id,
        description,
        &constraint(document)?,
    )))
}

#[pymodule]
fn _datacollections(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("SPEC_VERSION", json_constraint::VERSION)?;
    m.add_function(wrap_pyfunction!(constraint_check, m)?)?;
    m.add_function(wrap_pyfunction!(constraint_from_description, m)?)?;
    m.add_function(wrap_pyfunction!(constraint_declarations, m)?)?;
    m.add_function(wrap_pyfunction!(meet, m)?)?;
    m.add_function(wrap_pyfunction!(mismatches, m)?)?;
    m.add_function(wrap_pyfunction!(substitute, m)?)?;
    m.add_function(wrap_pyfunction!(subsumes_explain, m)?)?;
    m.add_function(wrap_pyfunction!(metadata_new, m)?)?;
    m.add_function(wrap_pyfunction!(metadata_read, m)?)?;
    m.add_function(wrap_pyfunction!(group_ref_attributes, m)?)?;
    m.add_function(wrap_pyfunction!(arrow_field_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(plan_append, m)?)?;
    m.add_function(wrap_pyfunction!(plan_evolve, m)?)?;
    m.add_function(wrap_pyfunction!(describe, m)?)?;
    m.add_function(wrap_pyfunction!(bindings_from_row, m)?)?;
    m.add_function(wrap_pyfunction!(encode_cell, m)?)?;
    m.add_function(wrap_pyfunction!(decode_cell, m)?)?;
    m.add_function(wrap_pyfunction!(generate_ids, m)?)?;
    m.add_function(wrap_pyfunction!(render_view, m)?)?;
    m.add_function(wrap_pyfunction!(view_columns_read, m)?)?;
    m.add_function(wrap_pyfunction!(stac_item_mapping, m)?)?;
    m.add_function(wrap_pyfunction!(stac_collection, m)?)?;
    Ok(())
}
