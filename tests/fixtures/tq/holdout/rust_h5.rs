use std::mem::size_of;

struct Rng;
struct Uniform;
impl Rng {
    fn sample_iter(&self, _dist: &Uniform) -> SampleIter {
        SampleIter
    }
}
struct SampleIter;
impl Iterator for SampleIter {
    type Item = u8;
    fn next(&mut self) -> Option<u8> {
        Some(1)
    }
}

fn build_dynamic() -> Vec<i32> {
    Vec::new()
}

fn recv() -> Result<usize, i32> {
    Ok(4)
}

fn is_would_block(e: &i32) -> bool {
    *e == 11
}

fn poll_next() -> Option<Option<i32>> {
    None
}

enum PollState {
    Ok(i32),
    Err(i32),
    Pending,
}

fn poll_once() -> PollState {
    PollState::Ok(1)
}

fn assert_generic_fn<T>(_v: T) {
    assert!(true);
}

fn parse_input(s: &str) -> Result<i32, &'static str> {
    if s.is_empty() {
        Err("empty")
    } else {
        Ok(1)
    }
}

macro_rules! local_select {
    ($($tt:tt)*) => {};
}

mod checker {
    pub fn assert<T>(v: T)
    where
        T: PartialEq + std::fmt::Debug,
    {
        let _ = v;
    }
}

struct Runner;
impl Runner {
    fn verify(v: i32) {
        assert!(v > 0);
    }
}

struct Wrapper<T>(T);

struct List {
    head: usize,
}
impl List {
    fn new() -> Self {
        Self { head: 0 }
    }
    fn pop(&mut self, _tx: &()) -> Option<usize> {
        self.head += 1;
        None
    }
}

#[derive(Clone, PartialEq, Debug)]
struct Token(u32);
impl Token {
    fn new() -> Self {
        Self(1)
    }
}

fn run_model<F: FnOnce()>(f: F) {
    f();
}

fn drain_items(shards: &[usize]) -> Vec<usize> {
    shards.iter().flat_map(|&s| vec![s]).collect()
}

fn expected_items(shards: &[usize]) -> Vec<usize> {
    shards.to_vec()
}

fn submit(_events: &mut Vec<i32>) {}

#[cfg(test)]
mod tests {
    use super::*;

    const FLAGS: &[&str] = &["alpha", "beta"];

    #[test]
    fn sample_iter_take_and_zip_unbounded() {
        let rng = Rng;
        let dist = Uniform;
        let xs: Vec<u8> = rng.sample_iter(&dist).take(8).collect();
        for x in xs {
            assert!(x <= u8::MAX);
        }
        let base = [1i32, 2, 3];
        let pairs: Vec<_> = base.iter().chain(&base).cloned().zip(0..).collect();
        for (a, b) in pairs {
            assert!(a > 0 && b >= 0);
        }
    }

    #[test]
    fn nested_inclusive_len_loops() {
        let t = b"abc";
        for start in 0..=t.len() {
            for end in start..=t.len() {
                for pos in 0..=end {
                    assert!(pos <= t.len());
                }
            }
        }
    }

    #[test]
    fn retry_until_ok_loop() {
        loop {
            match recv() {
                Ok(n) => {
                    assert_eq!(n, 4);
                    break;
                }
                Err(ref e) if is_would_block(e) => continue,
                Err(e) => panic!("{e}"),
            }
        }
    }

    #[test]
    fn while_poll_panic_check() {
        while let Some(v) = poll_next() {
            if v.is_none() {
                panic!("did not yield");
            }
        }
    }

    #[test]
    fn let_else_continue_static_slice() {
        for flag in FLAGS.iter() {
            let Some(ch) = flag.chars().next() else {
                continue;
            };
            assert!(ch.is_ascii_alphanumeric());
        }
    }

    #[test]
    fn every_match_arm_checks() {
        let mut slot = Some(());
        let ready = match poll_once() {
            PollState::Ok(v) => {
                assert_eq!(1, v);
                true
            }
            PollState::Err(_) => panic!("err"),
            PollState::Pending => false,
        };
        slot = if ready { None } else { slot };
        if let Some(()) = slot {
            assert_eq!(1, recv().unwrap());
        }
    }

    #[test]
    fn real_unguarded_dynamic_loop() {
        let xs = build_dynamic();
        for x in xs {
            assert!(x > 0);
        }
    }

    #[test]
    fn unwrap_err_and_expect_err() {
        let _ = parse_input("").unwrap_err();
    }

    #[test]
    fn select_macro_panic() {
        local_select! {
            _ = poll_next() => panic!("unexpected"),
            _ = recv() => {}
        }
    }

    #[test]
    fn local_fn_alias() {
        let test = assert_generic_fn::<i64>;
        test(1);
    }

    #[test]
    fn where_clause_generic_assert() {
        checker::assert(1);
    }

    #[test]
    fn impl_associated_helper() {
        Runner::verify(5);
    }

    #[test]
    fn real_only_logs() {
        println!("done");
    }

    #[test]
    fn distinct_turbofish_layout() {
        assert_eq!(size_of::<Wrapper<u8>>(), size_of::<u8>());
    }

    #[test]
    fn snapshot_mutated_through_assert() {
        let mut list = List::new();
        let tx = ();
        let head = list.head;
        assert!(list.pop(&tx).is_none());
        assert_eq!(list.head, head);
    }

    #[test]
    fn cloned_tokens_are_considered_equal() {
        let tok = Token::new();
        let tok_clone = tok.clone();
        assert_eq!(tok, tok_clone);
    }

    #[test]
    fn real_self_eq() {
        let x = recv().unwrap();
        assert_eq!(x, x);
    }

    #[test]
    fn callback_local_vec() {
        run_model(|| {
            let mut handles = Vec::new();
            handles.push(1);
            assert_eq!(handles.len(), 1);
        });
    }

    #[test]
    fn helper_with_flat_map_and_arg() {
        assert!(drain_items(&[]).is_empty());
        for shards in [&[1usize][..], &[0, 1][..]] {
            assert_eq!(drain_items(shards), expected_items(shards));
        }
    }

    #[test]
    fn real_absence_only() {
        let mut events = Vec::new();
        submit(&mut events);
        assert!(events.is_empty());
    }
}

struct Harness;
impl Harness {
    fn qex(&self) {
        let items = build_dynamic();
        if !items.is_empty() {
            assert!(items[0] >= 0);
        }
    }
}

fn build_fixture_value() -> i32 {
    let items = build_dynamic();
    if !items.is_empty() {
        assert!(items[0] >= 0);
    }
    1
}

#[cfg(test)]
mod setup_regression_tests {
    use super::*;

    #[test]
    fn real_harness_qex_unguarded() {
        let h = Harness;
        h.qex();
    }

    #[test]
    fn real_build_fixture_unguarded() {
        let _ = build_fixture_value();
    }
}
