//! Per-variable domains: a deliberately small subset of JSON Schema.
//!
//! `type` (integer / number / string / boolean) plus inclusive `minimum` and
//! `maximum`. No `enum` — categorical variation is a cohort, not a domain. No
//! `pattern`, no composition keywords. See `spec/constraint-language.md` §1.1.

use serde_json::{Map, Value};

use crate::error::Malformed;

/// The recognised domain keywords. Anything else in a `$var` object is malformed,
/// so that a later version can add keywords without silently weakening old readers.
pub const DOMAIN_KEYS: [&str; 3] = ["type", "minimum", "maximum"];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ScalarType {
    Integer,
    Number,
    String,
    Boolean,
}

impl ScalarType {
    fn parse(s: &str) -> Option<Self> {
        match s {
            "integer" => Some(Self::Integer),
            "number" => Some(Self::Number),
            "string" => Some(Self::String),
            "boolean" => Some(Self::Boolean),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Integer => "integer",
            Self::Number => "number",
            Self::String => "string",
            Self::Boolean => "boolean",
        }
    }

    /// `number` admits `integer`; otherwise types must agree.
    fn admits_type(self, other: ScalarType) -> bool {
        self == other || (self == Self::Number && other == Self::Integer)
    }
}

/// An inline domain. `raw` is kept so that "identical domains at every use site"
/// is checked against what the author actually wrote.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Domain {
    pub ty: Option<ScalarType>,
    pub minimum: Option<f64>,
    pub maximum: Option<f64>,
    raw: Map<String, Value>,
}

impl Domain {
    /// Parse the sibling keys of a `$var` leaf.
    pub fn parse(obj: &Map<String, Value>, var_name: &str) -> Result<Self, Malformed> {
        let mut d = Domain::default();
        for (k, v) in obj {
            if k == "$var" {
                continue;
            }
            if !DOMAIN_KEYS.contains(&k.as_str()) {
                return Err(Malformed::UnknownDomainKey {
                    var: var_name.to_string(),
                    key: k.clone(),
                });
            }
            match k.as_str() {
                "type" => {
                    let s = v.as_str().and_then(ScalarType::parse).ok_or_else(|| {
                        Malformed::BadDomain {
                            var: var_name.to_string(),
                            reason: format!(
                                "`type` must be one of integer/number/string/boolean, found {v}"
                            ),
                        }
                    })?;
                    d.ty = Some(s);
                }
                "minimum" | "maximum" => {
                    let n = v.as_f64().ok_or_else(|| Malformed::BadDomain {
                        var: var_name.to_string(),
                        reason: format!("`{k}` must be a number, found {v}"),
                    })?;
                    if k == "minimum" {
                        d.minimum = Some(n)
                    } else {
                        d.maximum = Some(n)
                    }
                }
                _ => unreachable!(),
            }
            d.raw.insert(k.clone(), v.clone());
        }
        if let (Some(lo), Some(hi)) = (d.minimum, d.maximum) {
            if lo > hi {
                return Err(Malformed::BadDomain {
                    var: var_name.to_string(),
                    reason: format!("minimum {lo} exceeds maximum {hi}"),
                });
            }
        }
        if (d.ty == Some(ScalarType::String) || d.ty == Some(ScalarType::Boolean))
            && (d.minimum.is_some() || d.maximum.is_some())
        {
            return Err(Malformed::BadDomain {
                var: var_name.to_string(),
                reason: "bounds are only meaningful for numeric types".to_string(),
            });
        }
        Ok(d)
    }

    /// The domain keywords, for round-tripping a `$var` leaf back to JSON.
    pub fn to_map(&self) -> Map<String, Value> {
        self.raw.clone()
    }

    pub fn is_unrestricted(&self) -> bool {
        self.ty.is_none() && self.minimum.is_none() && self.maximum.is_none()
    }

    /// Does this domain admit `value`? Returns a user-facing reason if not.
    ///
    /// A variable matches **scalars only**; anything structural must be a wildcard.
    pub fn admits(&self, value: &Value) -> Result<(), String> {
        let observed = match value {
            Value::Null => None, // null satisfies an unrestricted domain only
            Value::Bool(_) => Some(ScalarType::Boolean),
            Value::String(_) => Some(ScalarType::String),
            Value::Number(n) => Some(if n.is_i64() || n.is_u64() {
                ScalarType::Integer
            } else {
                ScalarType::Number
            }),
            Value::Array(_) | Value::Object(_) => {
                return Err(
                    "a variable matches scalars only; use a wildcard for a structural leaf"
                        .to_string(),
                )
            }
        };
        match (self.ty, observed) {
            (None, _) => {}
            (Some(want), None) => {
                return Err(format!("expected {}, found null", want.as_str()));
            }
            (Some(want), Some(got)) => {
                if !want.admits_type(got) {
                    return Err(format!(
                        "expected {}, found {}",
                        want.as_str(),
                        got.as_str()
                    ));
                }
            }
        }
        if let Some(n) = value.as_f64() {
            if let Some(lo) = self.minimum {
                if n < lo {
                    return Err(format!("{n} is below minimum {lo}"));
                }
            }
            if let Some(hi) = self.maximum {
                if n > hi {
                    return Err(format!("{n} is above maximum {hi}"));
                }
            }
        }
        Ok(())
    }

    /// Domain containment: does `self` admit everything `other` admits?
    /// Used by `subsumes`. An unrestricted domain contains every domain.
    pub fn contains(&self, other: &Domain) -> bool {
        match (self.ty, other.ty) {
            (None, _) => {}
            (Some(_), None) => return false,
            (Some(a), Some(b)) => {
                if !a.admits_type(b) {
                    return false;
                }
            }
        }
        if let Some(lo) = self.minimum {
            match other.minimum {
                Some(other_lo) if other_lo >= lo => {}
                _ => return false,
            }
        }
        if let Some(hi) = self.maximum {
            match other.maximum {
                Some(other_hi) if other_hi <= hi => {}
                _ => return false,
            }
        }
        true
    }

    /// Two use sites of one variable must declare the same domain.
    pub fn same_as(&self, other: &Domain) -> bool {
        self.raw == other.raw
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dom(v: Value) -> Domain {
        let mut m = v.as_object().unwrap().clone();
        m.insert("$var".into(), json!("x"));
        Domain::parse(&m, "x").unwrap()
    }

    #[test]
    fn admits_respects_type_and_bounds() {
        let d = dom(json!({"type": "integer", "minimum": 1}));
        assert!(d.admits(&json!(3)).is_ok());
        assert!(d.admits(&json!(0)).is_err());
        assert!(d.admits(&json!("3")).is_err());
        assert!(d.admits(&json!([1])).is_err());
    }

    #[test]
    fn number_admits_integer_but_not_the_reverse() {
        assert!(dom(json!({"type": "number"})).admits(&json!(3)).is_ok());
        assert!(dom(json!({"type": "integer"})).admits(&json!(3.5)).is_err());
    }

    #[test]
    fn containment_is_interval_containment() {
        let wide = dom(json!({"type": "integer", "minimum": 1, "maximum": 10}));
        let narrow = dom(json!({"type": "integer", "minimum": 2, "maximum": 9}));
        assert!(wide.contains(&narrow));
        assert!(!narrow.contains(&wide));
        assert!(Domain::default().contains(&wide));
        assert!(!wide.contains(&Domain::default()));
    }

    #[test]
    fn unknown_domain_keys_are_malformed() {
        let m = json!({"$var": "x", "enum": [1, 2]});
        assert!(Domain::parse(m.as_object().unwrap(), "x").is_err());
    }
}
