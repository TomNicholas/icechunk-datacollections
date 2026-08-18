//! STAC, expressed as *one view* in the general mapping language.
//!
//! Nothing here is privileged: `item_mapping` builds an ordinary [`ViewMapping`],
//! and a domain with no STAC vocabulary uses the same machinery with a different
//! template. If this module were deleted the rest of the stack would not notice,
//! which is the property the factoring is supposed to have.

use json_constraint::{Constraint, DeclKind, ScalarType};
use serde_json::{json, Map, Value};

use crate::ViewMapping;

/// How to fill the fields a STAC Item requires but a Zarr group has no opinion about.
///
/// Every field here is a *source string* in the mapping language, so `datetime` may
/// be an extra column (`column:datetime`) or dug out of the member's own attributes
/// (`description:/attributes/acquisition_time`) with no code change.
#[derive(Clone, Debug)]
pub struct ItemViewConfig {
    pub collection: String,
    /// Source for the Item `id`. Opaque member ids are not human-meaningful, so this
    /// usually reads an extra column — that is the point of extra columns existing.
    pub id: Value,
    pub datetime: Value,
    /// `bbox` is very often a derived extra column (the same one an R-tree indexes),
    /// not a variable.
    pub bbox: Option<Value>,
    pub geometry: Option<Value>,
    /// Extra Item properties, as `property name -> source`.
    pub properties: Vec<(String, Value)>,
    pub assets: Value,
}

impl ItemViewConfig {
    pub fn new(collection: impl Into<String>, id: Value, datetime: Value) -> Self {
        ItemViewConfig {
            collection: collection.into(),
            id,
            datetime,
            bbox: None,
            geometry: None,
            properties: Vec::new(),
            assets: json!({}),
        }
    }

    pub fn with_bbox(mut self, source: Value) -> Self {
        self.bbox = Some(source);
        self
    }

    pub fn with_geometry(mut self, source: Value) -> Self {
        self.geometry = Some(source);
        self
    }

    pub fn with_property(mut self, name: impl Into<String>, source: Value) -> Self {
        self.properties.push((name.into(), source));
        self
    }
}

/// A source that reads a column.
pub fn column(name: &str) -> Value {
    json!({ "$from": format!("column:{name}") })
}

/// A source that reads a JSON Pointer into the member's reconstructed description.
pub fn description(pointer: &str) -> Value {
    json!({ "$from": format!("description:{pointer}") })
}

pub fn item_mapping(cfg: &ItemViewConfig) -> ViewMapping {
    let mut properties = Map::new();
    properties.insert("datetime".into(), cfg.datetime.clone());
    for (k, v) in &cfg.properties {
        properties.insert(k.clone(), v.clone());
    }

    let mut template = Map::new();
    template.insert("type".into(), json!("Feature"));
    template.insert("stac_version".into(), json!("1.1.0"));
    template.insert("id".into(), cfg.id.clone());
    template.insert("collection".into(), json!(cfg.collection));
    template.insert(
        "geometry".into(),
        cfg.geometry.clone().unwrap_or(Value::Null),
    );
    if let Some(b) = &cfg.bbox {
        template.insert("bbox".into(), b.clone());
    }
    template.insert("properties".into(), Value::Object(properties));
    template.insert("assets".into(), cfg.assets.clone());
    template.insert("links".into(), json!([]));

    ViewMapping::new("stac-item", Value::Object(template))
}

/// A STAC Collection derived from the constraint itself.
///
/// The interesting half is `summaries`: a variable's declared domain *is* a summary,
/// so the collection-level description of what varies falls out of the constraint
/// with no extra authoring. Wildcards are deliberately absent — a wildcard is a leaf
/// we declined to describe, so there is nothing truthful to summarise.
pub fn collection_document(id: &str, description_text: &str, constraint: &Constraint) -> Value {
    let mut summaries = Map::new();
    for d in constraint.declarations() {
        if d.kind != DeclKind::Variable {
            continue;
        }
        let mut s = Map::new();
        if let Some(t) = d.domain.ty {
            s.insert(
                "type".into(),
                json!(match t {
                    ScalarType::Integer => "integer",
                    ScalarType::Number => "number",
                    ScalarType::String => "string",
                    ScalarType::Boolean => "boolean",
                }),
            );
        }
        if let Some(min) = d.domain.minimum {
            s.insert("minimum".into(), json!(min));
        }
        if let Some(max) = d.domain.maximum {
            s.insert("maximum".into(), json!(max));
        }
        if !s.is_empty() {
            summaries.insert(d.name.clone(), Value::Object(s));
        }
    }

    json!({
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": id,
        "description": description_text,
        "license": "proprietary",
        // Deliberately unbounded: a real extent is a query over the table, not a
        // property of the constraint, and inventing one here would be a lie.
        "extent": {
            "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {"interval": [[null, null]]}
        },
        "summaries": Value::Object(summaries),
        "links": []
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn constraint() -> Constraint {
        Constraint::parse(&json!({
            "attributes": {
                "proj:epsg": {"$var": "proj_epsg", "type": "integer", "minimum": 1024, "maximum": 32766},
                "cube:dimensions": {"$wild": "cube_dimensions"}
            }
        }))
        .unwrap()
    }

    #[test]
    fn an_item_is_just_a_rendered_template() {
        let cfg = ItemViewConfig::new(
            "sentinel-2-l2a",
            json!({"$join": ["S2-", {"$from": "column:granule"}]}),
            column("datetime"),
        )
        .with_bbox(column("bbox"))
        .with_property("proj:epsg", description("/attributes/proj:epsg"));

        let view = item_mapping(&cfg);
        let desc = json!({"attributes": {"proj:epsg": 32633}});
        let cols = json!({
            "granule": "T33UUP_20240102",
            "datetime": "2024-01-02T10:20:30Z",
            "bbox": [12.0, 45.0, 13.0, 46.0]
        });
        let item = view.render(&desc, cols.as_object().unwrap()).unwrap();

        assert_eq!(item["type"], "Feature");
        assert_eq!(item["id"], "S2-T33UUP_20240102");
        assert_eq!(item["collection"], "sentinel-2-l2a");
        assert_eq!(item["properties"]["datetime"], "2024-01-02T10:20:30Z");
        assert_eq!(item["properties"]["proj:epsg"], 32633);
        assert_eq!(item["bbox"][0], 12.0);
        assert!(item["geometry"].is_null());
    }

    #[test]
    fn collection_summaries_fall_out_of_the_variable_domains() {
        let c = collection_document("s2", "Sentinel-2 L2A tiles", &constraint());
        assert_eq!(c["summaries"]["proj_epsg"]["minimum"], 1024.0);
        assert_eq!(c["summaries"]["proj_epsg"]["type"], "integer");
        // a wildcard declines to describe its leaf, so it summarises nothing
        assert!(c["summaries"].get("cube_dimensions").is_none());
    }

    #[test]
    fn the_view_declares_which_columns_a_search_must_fetch() {
        let cfg = ItemViewConfig::new("c", column("granule"), column("datetime"))
            .with_bbox(column("bbox"));
        assert_eq!(
            item_mapping(&cfg).columns_read(),
            ["granule", "bbox", "datetime"]
        );
    }
}
