//! Compact, top-down mini-DAGs for the Threads board.
//!
//! One thumbnail per row: the run's recipe drawn top-down, each node as the
//! logo of the model that served it, with the recipe's **real** edges
//! (`depends_on`, `verifies`, `escalates_to`, `retries`, `supplies_context_to`)
//! rather than the run graph's column-to-column fan. Data comes from
//! `mini-ork board page dags` — see `mini_ork/ide_pages/dags.py` for the
//! contract, and `board::fetch_thumbs` for the read.
//!
//! Layering runs here, not in Python: the page ships raw nodes + edges, and
//! the plan is to keep its shape the same as the existing
//! `GET /api/v1/task-runs/{id}/dag` fingerprint. Longest path gives the same
//! top-down order the run page's columns use — see [`layer_nodes`] for why it
//! is depth-first rather than a topological sort.

use std::collections::HashMap;

use gpui::{AnyElement, Bounds, PathBuilder, Point, canvas, point, px, svg};
use serde::Deserialize;
use ui::prelude::*;

use crate::palette::{self as p, c};

/// Node mark diameter, in px, at compact density.
const NODE_D: f32 = 8.;
/// The tallest a thumbnail grows before the pitch is squeezed instead.
const MAX_H: f32 = 64.;
/// The most two layers are ever apart; `MIN_PITCH` is the floor, past which
/// the DAG is truncated rather than squeezed into mush.
const LAYER_PITCH: f32 = 11.;
const MIN_PITCH: f32 = 6.;
/// Padding around the drawn area, all four sides.
const PAD: f32 = 6.;
/// Past this many layers the tail is dropped and a `+N` marker is drawn — a
/// silently clipped DAG is worse than a labelled one.
const MAX_LAYERS: usize = 12;
/// The narrowest thumbnail worth drawing. The board's own floor (`BOARD_MIN`)
/// keeps us well above this; it guards a degenerate `set_width`.
pub const MIN_W: f32 = 120.;

#[derive(Debug, Clone, Default)]
pub struct TNode {
    pub id: String,
    pub lane: String,
    pub state: String,
    /// The workflow node type (verifier, implementer, …); empty when unknown.
    pub kind: String,
    /// node_start epoch seconds and the node_end duration, when seen.
    pub started_at: Option<i64>,
    pub duration_ms: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct TEdge {
    pub from: usize,
    pub to: usize,
    pub kind: String,
}

/// One run's recipe, laid out. `layers[i]` holds node indices, top to bottom.
#[derive(Debug, Clone, Default)]
pub struct Thumb {
    pub nodes: Vec<TNode>,
    pub edges: Vec<TEdge>,
    pub layers: Vec<Vec<usize>>,
}

// ── the page's JSON ──────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct RawDag {
    #[serde(default)]
    nodes: Vec<RawNode>,
    #[serde(default)]
    edges: Vec<RawEdge>,
}

#[derive(Debug, Deserialize)]
struct RawNode {
    #[serde(default)]
    id: String,
    /// The provider lane (glm, minimax, deepseek, …) or `null` when the
    /// recipe node names no `model_lane`.
    #[serde(default)]
    lane: Option<String>,
    #[serde(default)]
    state: String,
    #[serde(default, rename = "type")]
    kind: Option<String>,
    #[serde(default)]
    started_at: Option<f64>,
    #[serde(default)]
    duration_ms: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct RawEdge {
    #[serde(default)]
    from: String,
    #[serde(default)]
    to: String,
    #[serde(default)]
    edge_type: String,
}

/// Read the `dags` object of a `board page dags` payload into thumbnails.
/// Runs the page reported in `errors` are simply absent from the map: a run
/// whose recipe will not resolve must not cost the others their thumbnail.
pub fn dags_from_value(value: &serde_json::Value) -> HashMap<String, Thumb> {
    let Some(dags) = value.get("dags").cloned() else {
        return HashMap::new();
    };
    serde_json::from_value::<HashMap<String, RawDag>>(dags)
        .map(|raw| {
            raw.into_iter()
                .map(|(run_id, dag)| (run_id, Thumb::build(dag)))
                .collect()
        })
        .unwrap_or_default()
}

impl Thumb {
    fn build(raw: RawDag) -> Self {
        let nodes: Vec<TNode> = raw
            .nodes
            .into_iter()
            .filter(|n| !n.id.is_empty())
            .map(|n| TNode {
                id: n.id,
                lane: n.lane.unwrap_or_default(),
                state: n.state,
                kind: n.kind.unwrap_or_default(),
                started_at: n.started_at.map(|t| t as i64),
                duration_ms: n.duration_ms.map(|d| d as i64),
            })
            .collect();
        let index: HashMap<&str, usize> =
            nodes.iter().enumerate().map(|(i, n)| (n.id.as_str(), i)).collect();

        // One (from, to) pair can carry several `edge_type`s — `retries` and
        // `depends_on` often land on the same pair — and drawing the same
        // curve three times only piles up alpha. Keep the most meaningful.
        let mut edges: Vec<TEdge> = Vec::new();
        // Separately, the pairs that decide *placement*. Two differences from
        // `edges`: a `retries` edge is left out entirely (see `layer_nodes`),
        // and the pair is kept even when `edge_priority` drops it from the
        // drawing list — an ordering that a retry out-ranked would otherwise
        // stop ordering anything.
        let mut order: Vec<(usize, usize)> = Vec::new();
        for edge in raw.edges {
            let (Some(&from), Some(&to)) = (index.get(edge.from.as_str()), index.get(edge.to.as_str()))
            else {
                continue;
            };
            if from == to {
                continue;
            }
            if edge.edge_type != "retries" {
                order.push((from, to));
            }
            match edges.iter_mut().find(|e| e.from == from && e.to == to) {
                Some(existing) => {
                    if edge_priority(&edge.edge_type) > edge_priority(&existing.kind) {
                        existing.kind = edge.edge_type;
                    }
                }
                None => edges.push(TEdge { from, to, kind: edge.edge_type }),
            }
        }

        let layers = layer_nodes(nodes.len(), &order);
        Self { nodes, edges, layers }
    }
}

/// Which of a pair's several edge kinds is worth drawing, when they collapse
/// onto one curve. `depends_on` is the backbone and the default; the rest say
/// something the thumbnail could not otherwise show.
fn edge_priority(kind: &str) -> u8 {
    match kind {
        "verifies" => 5,
        "escalates_to" => 4,
        "retries" => 3,
        "supplies_context_to" => 2,
        "depends_on" => 1,
        _ => 0,
    }
}

/// Longest-path layering: node `u`'s layer is one past its deepest
/// predecessor, so a source sits at layer 0 and every edge runs downwards.
///
/// `order` is the pairs that decide placement, already free of `retries` — a
/// retry re-enters a node the flow has already passed, so it is a loop, not an
/// order. (`framework-edit` wires both verifiers and the reviewer back to the
/// implementer that way.) Left in, that loop makes the graph cyclic and a
/// topological sort would strand the whole cycle in layer 0 as one wide blob —
/// and even a depth-first walk answers by entry order rather than by shape.
///
/// Walking *up* the graph (to predecessors) rather than down is what makes the
/// answer "distance from a source"; walking down would give height above the
/// leaves and invert every row. Back edges are still dropped, as a net for a
/// recipe whose cycle comes from something other than `retries`, and every
/// node is visited once, so no workflow can hang the board.
fn layer_nodes(count: usize, order: &[(usize, usize)]) -> Vec<Vec<usize>> {
    #[derive(Clone, Copy, PartialEq)]
    enum Mark {
        Unseen,
        OnStack,
        Done,
    }

    fn visit(node: usize, waits_on: &[Vec<usize>], marks: &mut [Mark], depth: &mut [usize]) {
        marks[node] = Mark::OnStack;
        for index in 0..waits_on[node].len() {
            let previous = waits_on[node][index];
            if marks[previous] == Mark::OnStack {
                continue; // a back edge — the loop, not the order
            }
            if marks[previous] == Mark::Unseen {
                visit(previous, waits_on, marks, depth);
            }
            depth[node] = depth[node].max(depth[previous] + 1);
        }
        marks[node] = Mark::Done;
    }

    let mut waits_on = vec![Vec::new(); count];
    for &(from, to) in order {
        waits_on[to].push(from);
    }
    let mut marks = vec![Mark::Unseen; count];
    let mut depth = vec![0usize; count];
    for node in 0..count {
        if marks[node] == Mark::Unseen {
            visit(node, &waits_on, &mut marks, &mut depth);
        }
    }
    let deepest = depth.iter().copied().max().unwrap_or(0);
    let mut layers = vec![Vec::new(); deepest + 1];
    for (node, layer) in depth.into_iter().enumerate() {
        layers[layer].push(node);
    }
    layers
}

// ── layout ───────────────────────────────────────────────────────────────

/// Where a node sits, and how big its mark is, in thumbnail coordinates.
#[derive(Clone, Copy)]
struct Spot {
    x: f32,
    y: f32,
    d: f32,
}

struct Layout {
    /// `None` for a node past `MAX_LAYERS` — truncated, so no mark and no edge.
    spots: Vec<Option<Spot>>,
    height: f32,
    hidden_layers: usize,
}

fn layout(thumb: &Thumb, w: f32) -> Layout {
    let layers = &thumb.layers;
    // Squeeze the pitch before truncating: the silhouette matters more than a
    // full-size mark, and a 30-layer recipe would otherwise be 330px tall.
    let pitch = ((MAX_H - 2. * PAD) / layers.len().max(1) as f32).clamp(MIN_PITCH, LAYER_PITCH);
    // `MIN_PITCH` is a floor and `MAX_H` a ceiling, and the two disagree at the
    // tail: 12 layers at the 6px floor is 72px, past the cap. When both cannot
    // hold it is the *count* that gives, not the cap — a deep recipe is
    // truncated and labelled `+N` rather than quietly growing the row. At the
    // floor, 8 layers is the most that fits inside `MAX_H`.
    let fits = (((MAX_H - 2. * PAD) / MIN_PITCH).floor() as usize).max(1);
    let shown = layers.len().min(MAX_LAYERS).min(fits);
    let height = 2. * PAD + pitch * shown as f32;
    let usable = (w - 2. * PAD).max(NODE_D);

    let mut spots = vec![None; thumb.nodes.len()];
    for (depth, layer) in layers.iter().take(shown).enumerate() {
        let count = layer.len();
        if count == 0 {
            continue;
        }
        let y = PAD + pitch * (depth as f32 + 0.5);
        // Equal gaps *and* equal margins, so a fan-out stays symmetric about
        // the strip's centre; a crowded layer shrinks its marks rather than
        // letting them overlap.
        let d = if count > 1 {
            (usable / count as f32 - 1.).clamp(4., NODE_D)
        } else {
            NODE_D
        };
        // Widths only decide how *crowded* a layer is. Vertically the mark has
        // to fit the pitch too: a deep recipe squeezes the pitch towards
        // `MIN_PITCH`, and at 6px pitch an 8px logo would bleed into the mark
        // above and below it and the shape would stop reading at all.
        let d = d.min(pitch).max(4.);
        for (position, &node) in layer.iter().enumerate() {
            let x = if count == 1 {
                w / 2.
            } else {
                PAD + usable * (position as f32 + 0.5) / count as f32
            };
            spots[node] = Some(Spot { x, y, d });
        }
    }
    Layout {
        spots,
        height,
        hidden_layers: layers.len().saturating_sub(shown),
    }
}

/// The mark colour of a node: its model family, with the two states the
/// family tint would hide promoted — a failed node reads red at thumbnail
/// scale, and one that has not run yet stays dim so the shape of what *did*
/// run stands out.
fn mark_colour(node: &TNode) -> gpui::Hsla {
    match node.state.as_str() {
        "failed" | "error" => c(p::RED),
        "" | "never_seen" | "pending" | "skipped" => c(p::DIM),
        _ => c(p::family(&node.lane)),
    }
}

/// The colour of one edge, by what it means. `depends_on` is the backbone and
/// stays quiet; the exceptions are the ones worth seeing at this size.
fn edge_colour(kind: &str) -> gpui::Hsla {
    match kind {
        "verifies" => c(p::PURPLE),
        "escalates_to" => c(p::YELLOW),
        "retries" => c(p::ORANGE),
        "supplies_context_to" => c(p::CYAN),
        _ => c(p::DIM),
    }
}

/// A thumbnail of `thumb`, `w` px wide and as tall as its layer count needs.
// Not drawn on the Threads board any more (the user asked for Orca-style node
// lists only, 2026-10-07); the parsed nodes still feed those lists.
#[allow(dead_code)]
pub fn render_thumb(thumb: &Thumb, w: f32) -> AnyElement {
    let w = w.max(MIN_W);
    let layout = layout(thumb, w);
    if thumb.layers.is_empty() {
        return div().w(px(w)).h(px(0.)).into_any_element();
    }

    // The edges are painted by a canvas under the marks — GPUI paints
    // children in insertion order, so the canvas must be the first child.
    let spots: Vec<Option<Spot>> = layout.spots.clone();
    let edges: Vec<TEdge> = thumb.edges.clone();
    let painter = canvas(
        |_, _, _| (),
        move |bounds: Bounds<gpui::Pixels>, _, window, _| {
            let origin = bounds.origin;
            let at = |x: f32, y: f32| -> Point<gpui::Pixels> { point(origin.x + px(x), origin.y + px(y)) };
            for edge in &edges {
                let (Some(from), Some(to)) = (spots[edge.from], spots[edge.to]) else {
                    continue;
                };
                // Bottom-centre of the source to top-centre of the target:
                // every edge leaves and arrives vertically, which is what
                // keeps a fan-out from reading as a solid wedge.
                let (x1, y1) = (from.x, from.y + from.d / 2.);
                let (x2, y2) = (to.x, to.y - to.d / 2.);
                let mut path = PathBuilder::stroke(px(1.));
                path.move_to(at(x1, y1));
                if (x2 - x1).abs() < 0.5 {
                    // A straight drop needs no curve, and skipping it saves
                    // tessellating a bezier per spine edge per frame.
                    path.line_to(at(x2, y2));
                } else {
                    let bend = ((y2 - y1) * 0.5).clamp(2., 8.);
                    path.cubic_bezier_to(at(x2, y2), at(x1, y1 + bend), at(x2, y2 - bend));
                }
                if let Ok(path) = path.build() {
                    window.paint_path(path, edge_colour(&edge.kind));
                }
            }
        },
    )
    .absolute()
    .top_0()
    .left_0()
    .size_full();

    let marks = thumb.nodes.iter().enumerate().filter_map(|(i, node)| {
        let spot = layout.spots[i]?;
        let colour = mark_colour(node);
        // Deliberately no `.id()`, `.hover()` or `.on_click()`: each would add
        // a hitbox, and the row's own listener should own the click.
        Some(match p::model_icon(&node.lane) {
            Some(icon) => svg()
                .path(icon.path())
                .w(px(spot.d))
                .h(px(spot.d))
                .flex_none()
                // Mandatory: all three of `Svg::paint`'s arms gate on
                // `style.text.color`, and an untinted icon is invisible.
                .text_color(colour)
                .absolute()
                .left(px(spot.x - spot.d / 2.))
                .top(px(spot.y - spot.d / 2.))
                .into_any_element(),
            // An unmapped lane falls back to today's dot rather than to
            // nothing, so a new provider degrades instead of disappearing.
            None => div()
                .absolute()
                .left(px(spot.x - spot.d / 2.))
                .top(px(spot.y - spot.d / 2.))
                .w(px(spot.d))
                .h(px(spot.d))
                .rounded_full()
                .bg(colour)
                .into_any_element(),
        })
    });

    div()
        .relative()
        .w(px(w))
        .h(px(layout.height))
        .flex_none()
        // Nothing else in the row clips, and the truncation marker and edge
        // curves both rely on being clipped at the strip's bounds.
        .overflow_hidden()
        .child(painter)
        .children(marks)
        .when(layout.hidden_layers > 0, |this| {
            this.child(
                div()
                    .absolute()
                    .right(px(2.))
                    .bottom(px(0.))
                    .text_size(px(9.))
                    .text_color(c(p::DIM))
                    .child(format!("+{}", layout.hidden_layers)),
            )
        })
        .into_any_element()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The index of a node id in `thumb.nodes`.
    fn at(thumb: &Thumb, id: &str) -> usize {
        thumb.nodes.iter().position(|n| n.id == id).expect("node id")
    }

    /// A `planner → {a, b} → join` diamond: the fan-out/fan-in the thumbnail
    /// exists to show.
    fn diamond() -> Thumb {
        Thumb::build(RawDag {
            nodes: ["planner", "a", "b", "join"]
                .iter()
                .map(|id| RawNode {
                    id: (*id).into(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: [("planner", "a"), ("planner", "b"), ("a", "join"), ("b", "join")]
                .iter()
                .map(|(from, to)| RawEdge {
                    from: (*from).into(),
                    to: (*to).into(),
                    edge_type: "depends_on".into(),
                })
                .collect(),
        })
    }

    #[test]
    fn diamond_layers_are_the_two_middles_between_the_ends() {
        let thumb = diamond();
        assert_eq!(thumb.layers.len(), 3, "planner | a b | join");
        assert_eq!(thumb.layers[0], vec![at(&thumb, "planner")]);
        assert_eq!(thumb.layers[2], vec![at(&thumb, "join")]);
        let mut middle = thumb.layers[1].clone();
        middle.sort_unstable();
        let mut expected = vec![at(&thumb, "a"), at(&thumb, "b")];
        expected.sort_unstable();
        assert_eq!(middle, expected);
    }

    /// `framework-edit` wires implementer ↔ verifier with `max_rounds: 2`, so
    /// the workflow is genuinely cyclic. The retry edge is a loop-back: it must
    /// not strand the whole cycle in layer 0 (which is what a topological sort
    /// would do), and the layering must still terminate.
    #[test]
    fn a_retry_loop_does_not_collapse_the_layering() {
        let thumb = Thumb::build(RawDag {
            nodes: ["planner", "implementer", "verifier", "publisher"]
                .iter()
                .map(|id| RawNode {
                    id: (*id).into(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: vec![
                RawEdge { from: "planner".into(), to: "implementer".into(), edge_type: "depends_on".into() },
                RawEdge { from: "implementer".into(), to: "verifier".into(), edge_type: "verifies".into() },
                // The back edge.
                RawEdge { from: "verifier".into(), to: "implementer".into(), edge_type: "retries".into() },
                RawEdge { from: "verifier".into(), to: "publisher".into(), edge_type: "depends_on".into() },
            ],
        });
        assert_eq!(thumb.layers.len(), 4, "one node per layer, not one blob");
        assert_eq!(thumb.layers[0], vec![at(&thumb, "planner")]);
        assert_eq!(thumb.layers[1], vec![at(&thumb, "implementer")]);
        assert_eq!(thumb.layers[2], vec![at(&thumb, "verifier")]);
        assert_eq!(thumb.layers[3], vec![at(&thumb, "publisher")]);
    }

    /// `framework-edit`'s real edges, verbatim from `workflow.yaml`: a
    /// two-lens fan-out that then fan-ins, one node per layer after that, and
    /// the three `retries` back to the implementer. Seven layers, spine-shaped.
    #[test]
    fn the_real_framework_edit_workflow_lays_out_as_a_spine() {
        let ids = [
            "planner",
            "code_impact_lens",
            "prior_art_lens",
            "implementer",
            "static_check_verifier",
            "test_verifier",
            "reviewer",
            "publisher",
            "rollback",
        ];
        let raw = |from: &str, to: &str, kind: &str| RawEdge {
            from: from.into(),
            to: to.into(),
            edge_type: kind.into(),
        };
        let thumb = Thumb::build(RawDag {
            nodes: ids
                .iter()
                .map(|id| RawNode {
                    id: (*id).into(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: vec![
                raw("planner", "code_impact_lens", "depends_on"),
                raw("planner", "prior_art_lens", "depends_on"),
                raw("code_impact_lens", "implementer", "supplies_context_to"),
                raw("prior_art_lens", "implementer", "supplies_context_to"),
                raw("implementer", "static_check_verifier", "verifies"),
                raw("implementer", "test_verifier", "verifies"),
                raw("static_check_verifier", "reviewer", "depends_on"),
                raw("test_verifier", "reviewer", "depends_on"),
                raw("reviewer", "publisher", "depends_on"),
                raw("reviewer", "rollback", "escalates_to"),
                raw("static_check_verifier", "rollback", "escalates_to"),
                raw("test_verifier", "rollback", "escalates_to"),
                raw("static_check_verifier", "implementer", "retries"),
                raw("test_verifier", "implementer", "retries"),
                raw("reviewer", "implementer", "retries"),
            ],
        });
        assert_eq!(thumb.layers.len(), 6, "planner | 2 lenses | impl | 2 verifiers | reviewer | publisher+rollback");
        assert_eq!(thumb.layers[0], vec![at(&thumb, "planner")]);
        assert_eq!(thumb.layers[1].len(), 2, "the lens fan-out");
        assert_eq!(thumb.layers[2], vec![at(&thumb, "implementer")]);
        assert_eq!(thumb.layers[3].len(), 2, "the verifier fan-in");
        assert_eq!(thumb.layers[4], vec![at(&thumb, "reviewer")]);
        // `rollback` is escalated to straight from both verifiers, so its
        // longest path is the same length as `publisher`'s — the two are
        // siblings at the bottom, not one after the other.
        let mut bottom = thumb.layers[5].clone();
        bottom.sort_unstable();
        let mut expected = vec![at(&thumb, "publisher"), at(&thumb, "rollback")];
        expected.sort_unstable();
        assert_eq!(bottom, expected);
        // 6 layers of a 64px cap are inside the approved ~40-65px row growth.
        let height = layout(&thumb, 244.).height;
        assert!((height - 64.).abs() < 0.01, "6 layers should hit the cap, got {height}");
    }

    /// A recipe deep enough to squeeze the pitch must shrink its marks with
    /// it: an 8px logo on a 6px pitch overlaps the mark above and below and the
    /// silhouette stops reading.
    #[test]
    fn a_squeezed_pitch_shrinks_the_marks_to_match() {
        let ids: Vec<String> = (0..12).map(|i| format!("n{i}")).collect();
        let thumb = Thumb::build(RawDag {
            nodes: ids
                .iter()
                .map(|id| RawNode {
                    id: id.clone(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            // A plain chain: one node per layer, so `usable` never limits `d`.
            edges: (0..11)
                .map(|i| RawEdge {
                    from: format!("n{i}"),
                    to: format!("n{}", i + 1),
                    edge_type: "depends_on".into(),
                })
                .collect(),
        });
        assert_eq!(thumb.layers.len(), 12);
        let spots = layout(&thumb, 244.).spots;
        let pitch = ((MAX_H - 2. * PAD) / 12.).clamp(MIN_PITCH, LAYER_PITCH);
        for spot in spots.iter().flatten() {
            assert!(
                spot.d <= pitch + 0.001,
                "a mark of {} on a pitch of {pitch} would overlap its neighbour",
                spot.d
            );
        }
    }

    /// `verification-technique-review` is 14 layers deep — the deepest recipe
    /// in the tree. The pitch floor alone would put it at 84px, past the cap
    /// the row was designed around, so the layer *count* gives instead.
    #[test]
    fn a_deep_recipe_is_truncated_to_the_cap_rather_than_stretching_the_row() {
        let ids: Vec<String> = (0..14).map(|i| format!("n{i}")).collect();
        let thumb = Thumb::build(RawDag {
            nodes: ids
                .iter()
                .map(|id| RawNode {
                    id: id.clone(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: (0..13)
                .map(|i| RawEdge {
                    from: format!("n{i}"),
                    to: format!("n{}", i + 1),
                    edge_type: "depends_on".into(),
                })
                .collect(),
        });
        assert_eq!(thumb.layers.len(), 14);
        let laid_out = layout(&thumb, 244.);
        assert!(
            laid_out.height <= MAX_H + 0.001,
            "the row must not grow past the cap, got {}",
            laid_out.height
        );
        assert_eq!(laid_out.hidden_layers, 6, "14 layers shown as 8, plus a `+6` marker");
        // The truncation has to be real, or an edge would run to a mark that
        // was never drawn:
        assert!(laid_out.spots[..8].iter().all(Option::is_some), "the head is what is shown");
        assert!(laid_out.spots[8..].iter().all(Option::is_none), "the tail is dropped");
        // An edge runs bottom-centre to top-centre, so the last mark must sit
        // inside the strip or its edge would be clipped mid-curve.
        for spot in laid_out.spots.iter().flatten() {
            assert!(spot.y + spot.d / 2. <= laid_out.height, "a mark spills out of the strip");
        }
    }

    /// The compact case must not pay for the fix: at a comfortable pitch the
    /// mark is still the full 8px the design asks for.
    #[test]
    fn a_comfortable_pitch_keeps_the_full_marks() {
        let thumb = diamond();
        for spot in layout(&thumb, 244.).spots.iter().flatten() {
            assert_eq!(spot.d, NODE_D, "a 3-layer recipe has room for the full mark");
        }
    }

    #[test]
    fn one_pair_of_nodes_draws_one_curve() {
        let thumb = Thumb::build(RawDag {
            nodes: ["a", "b"]
                .iter()
                .map(|id| RawNode {
                    id: (*id).into(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: vec![
                RawEdge { from: "a".into(), to: "b".into(), edge_type: "depends_on".into() },
                RawEdge { from: "a".into(), to: "b".into(), edge_type: "verifies".into() },
            ],
        });
        assert_eq!(thumb.edges.len(), 1, "the same pair collapses to one curve");
        assert_eq!(thumb.edges[0].kind, "verifies", "the more meaningful kind wins");
    }

    #[test]
    fn a_dangling_or_self_edge_is_dropped() {
        let thumb = Thumb::build(RawDag {
            nodes: ["a", "b"]
                .iter()
                .map(|id| RawNode {
                    id: (*id).into(),
                    lane: None,
                    state: "done".into(),
                    kind: None,
                    started_at: None,
                    duration_ms: None,
                })
                .collect(),
            edges: vec![
                RawEdge { from: "a".into(), to: "ghost".into(), edge_type: "depends_on".into() },
                RawEdge { from: "a".into(), to: "a".into(), edge_type: "retries".into() },
            ],
        });
        assert!(thumb.edges.is_empty(), "an edge to a node that is not here is not drawn");
    }
}
