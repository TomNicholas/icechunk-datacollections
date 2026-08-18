//! Member ids are **generated, not supplied**: 128 random bits, hex-encoded.
//!
//! Uniqueness then needs no coordination between concurrent writers, which is what
//! uncoordinated parallel appends require. Deliberately *not* content-addressed:
//! two members may legitimately have identical metadata and must stay distinct, and
//! hashing would reintroduce the canonicalisation problem that taking `zarr.json`
//! as-is avoids.
//!
//! The generator is injectable and seedable so that tests can assert an incremental
//! build is byte-equivalent to a from-scratch one — "modulo ids" would be a much
//! weaker assertion.

use std::collections::hash_map::RandomState;
use std::hash::{BuildHasher, Hasher};

/// 32 hex characters.
pub const ID_LEN: usize = 32;

pub trait IdGenerator: Send {
    fn next_id(&mut self) -> String;
}

/// splitmix64, seeded. Deterministic, and good enough for ids we only need to be
/// distinct — this is not a security boundary.
pub struct SeededIds {
    state: u64,
}

impl SeededIds {
    pub fn new(seed: u64) -> Self {
        Self { state: seed }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
}

impl IdGenerator for SeededIds {
    fn next_id(&mut self) -> String {
        format!("{:016x}{:016x}", self.next_u64(), self.next_u64())
    }
}

/// Seeded from the process's own hash randomisation plus the clock. Adequate for
/// 128-bit ids whose only requirement is distinctness.
pub struct RandomIds(SeededIds);

impl Default for RandomIds {
    fn default() -> Self {
        let mut h = RandomState::new().build_hasher();
        h.write_u64(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos() as u64)
                .unwrap_or(0),
        );
        h.write_usize(&h as *const _ as usize);
        RandomIds(SeededIds::new(h.finish()))
    }
}

impl IdGenerator for RandomIds {
    fn next_id(&mut self) -> String {
        self.0.next_id()
    }
}

pub fn is_member_id(s: &str) -> bool {
    s.len() == ID_LEN
        && s.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn seeded_ids_are_reproducible_and_distinct() {
        let a: Vec<String> = (0..1000)
            .map(|_| SeededIds::new(7).next_id())
            .take(1)
            .collect();
        let mut g = SeededIds::new(7);
        assert_eq!(g.next_id(), a[0]);

        let mut g = SeededIds::new(1);
        let ids: HashSet<String> = (0..10_000).map(|_| g.next_id()).collect();
        assert_eq!(ids.len(), 10_000);
        assert!(ids.iter().all(|i| is_member_id(i)));
    }

    #[test]
    fn different_seeds_diverge() {
        assert_ne!(SeededIds::new(1).next_id(), SeededIds::new(2).next_id());
    }
}
