//! Errors. Rejection messages are user-facing: `add_item` refusing a member must
//! say *which leaf* failed, expected versus found — not "constraint violation".

use thiserror::Error;

/// A constraint document that is not well-formed. See spec §4.
#[derive(Debug, Error, PartialEq)]
pub enum Malformed {
    #[error("at {pointer}: object carries more than one of $var / $wild / $literal")]
    ConflictingLeafKeys { pointer: String },

    #[error("at {pointer}: `{key}` is reserved for a deferred language feature and is not implemented in 0.1")]
    DeferredSyntax { pointer: String, key: String },

    #[error(
        "at {pointer}: `${kind}` must be a string matching ^[A-Za-z_][A-Za-z0-9_]*$, found {found}"
    )]
    BadName {
        pointer: String,
        kind: String,
        found: String,
    },

    #[error(
        "variable `{var}`: unrecognised domain keyword `{key}` (0.1 allows type, minimum, maximum)"
    )]
    UnknownDomainKey { var: String, key: String },

    #[error("variable `{var}`: {reason}")]
    BadDomain { var: String, reason: String },

    #[error("variable `{var}` declares different domains at {first} and {second}; a repeated variable must declare identical domains")]
    DomainDisagreement {
        var: String,
        first: String,
        second: String,
    },

    #[error("wildcard `{name}` occurs at both {first} and {second}; a wildcard asserts nothing, so it may not be reused")]
    RepeatedWildcard {
        name: String,
        first: String,
        second: String,
    },

    #[error("`{name}` is used as both a variable and a wildcard")]
    NameCollision { name: String },
}

/// One reason a description failed to `meet` a constraint.
#[derive(Debug, Clone, PartialEq)]
pub struct Mismatch {
    /// JSON Pointer into the description.
    pub pointer: String,
    pub kind: MismatchKind,
    pub expected: String,
    pub found: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MismatchKind {
    /// A literal leaf did not compare equal.
    Literal,
    /// Object key sets differ. v1 has no optionality.
    KeySet,
    /// Arrays of different lengths. Rank changes must be a whole-leaf wildcard.
    Length,
    /// Structural kind differs — object where an array was expected, etc.
    Kind,
    /// A scalar outside its variable's declared domain.
    Domain,
    /// One variable bound to two different values within a single member.
    CoConstraint,
}

impl std::fmt::Display for Mismatch {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let what = match self.kind {
            MismatchKind::Literal => "literal mismatch",
            MismatchKind::KeySet => "key set mismatch",
            MismatchKind::Length => "length mismatch",
            MismatchKind::Kind => "structural mismatch",
            MismatchKind::Domain => "domain violation",
            MismatchKind::CoConstraint => "co-constraint violation",
        };
        write!(
            f,
            "{} at {}: expected {}, found {}",
            what,
            if self.pointer.is_empty() {
                "/"
            } else {
                &self.pointer
            },
            self.expected,
            self.found
        )
    }
}

/// A member did not match the constraint.
#[derive(Debug, Clone, PartialEq, Error)]
#[error("{}", .0.iter().map(|m| m.to_string()).collect::<Vec<_>>().join("; "))]
pub struct MeetError(pub Vec<Mismatch>);

impl MeetError {
    pub fn mismatches(&self) -> &[Mismatch] {
        &self.0
    }
}

#[derive(Debug, Error, PartialEq)]
pub enum SubstituteError {
    #[error("no binding for `{0}`")]
    Unbound(String),

    #[error("binding for `{name}` violates its declared domain: {reason}")]
    OutOfDomain { name: String, reason: String },
}
