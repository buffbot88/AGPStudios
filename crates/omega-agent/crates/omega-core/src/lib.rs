//! Omega inference engine.
//!
//! The spawn-on-demand `llama-server` pool (`demand`), bounded wait queues
//! (`queue`), request proxying (`proxy`), the row-chain walker (`router`),
//! and the bounded execution tool loop. No HTTP surface here — that lives in
//! `omega-server`.

pub mod demand;
pub mod proxy;
pub mod queue;
pub mod router;
pub mod tool_loop;
