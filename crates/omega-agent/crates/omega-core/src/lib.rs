//! Omega inference engine.
//!
//! The spawn-on-demand `llama-server` pool (`demand`), bounded wait queues
//! (`queue`), the intent-router orchestrator (`orchestrator`), request proxying
//! (`proxy`), the row-chain walker (`router`), and baseline supervision
//! (`supervision`). No HTTP surface here — that lives in `omega-server`.

pub mod demand;
pub mod orchestrator;
pub mod proxy;
pub mod queue;
pub mod router;
pub mod skill_db;
pub mod supervision;
pub mod tool_loop;
