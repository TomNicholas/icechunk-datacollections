//! One member's values for the variables and wildcards of its cohort's constraint.
//!
//! A binding's *scope* is one member (one row). Its *storage* is a column of N
//! bindings — but that is the storage layer's business, not this crate's.

use indexmap::IndexMap;
use serde_json::{Map, Value};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Bindings(IndexMap<String, Value>);

impl Bindings {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get(&self, name: &str) -> Option<&Value> {
        self.0.get(name)
    }

    pub fn insert(&mut self, name: impl Into<String>, value: Value) -> Option<Value> {
        self.0.insert(name.into(), value)
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&String, &Value)> {
        self.0.iter()
    }

    pub fn names(&self) -> impl Iterator<Item = &String> {
        self.0.keys()
    }

    pub fn to_json(&self) -> Value {
        let mut m = Map::new();
        for (k, v) in &self.0 {
            m.insert(k.clone(), v.clone());
        }
        Value::Object(m)
    }

    pub fn from_json(value: &Value) -> Option<Self> {
        let obj = value.as_object()?;
        let mut b = Bindings::new();
        for (k, v) in obj {
            b.insert(k.clone(), v.clone());
        }
        Some(b)
    }
}

impl FromIterator<(String, Value)> for Bindings {
    fn from_iter<T: IntoIterator<Item = (String, Value)>>(iter: T) -> Self {
        Bindings(iter.into_iter().collect())
    }
}
