//! The run flow map: one horizontal track from **Ticket in** through
//! **Design** and the **Build** fan-out to the **Integrate** pill, then a
//! return track back left along a lower line carrying the **Review** hub, its
//! spokes, the **Fix** loop, the **Merge** bar (or a **Rolled back** marker)
//! and, below the bar, the **Human** who owns the final merge.
//!
//! The data is the run page's top-level `flow` object (`mini_ork/ide_pages/
//! run_flow.py::build_flow`): the ticket, the design steps with their revise
//! loops, the build packages with an Implementer / critic lane each, the
//! integrate node with its check tiers, and the review / merge / rollback /
//! legend / stages that close the run out.
//!
//! Connectors are one `gpui::canvas` painting stroked paths — the same idiom
//! `graph.rs` and `thumb.rs` use — while every marker, label, package box and
//! pill is an absolutely positioned div over it. That keeps the labels
//! selectable and lets a marker carry the click that opens its node.
//!
//! Geometry is decided once in [`Layout::compute`] so the canvas (which needs
//! the endpoints) and the divs (which need the same anchors) cannot drift.

use std::{
    collections::{HashMap, HashSet},
    time::Duration,
};

use gpui::{
    AnyElement, Bounds, ClickEvent, Context, Div, Font, FontWeight, Hsla, PathBuilder, Pixels,
    Point, ScrollHandle, SharedString, Window, canvas, point, px,
};
use serde_json::Value;
use ui::{ScrollAxes, Scrollbars, WithScrollbar as _, prelude::*};

use crate::{
    page::{MiniOrkPage, arr, bool_of, str_of},
    palette::{self as p, c, ca},
};

/// The y of the track's centre line, in the map's own coordinates.
const TRACK_Y: f32 = 108.;
/// Edges are 3px; an edge into a pending (or skipped) step is 2px.
const EDGE_W: f32 = 3.;
const EDGE_W_PENDING: f32 = 2.;
/// A design marker: the 22px ring and the 9px dot inside it.
const MARKER_D: f32 = 22.;
const DOT_D: f32 = 9.;
/// Horizontal pitch between two design markers.
const STEP_PITCH: f32 = 132.;
/// The ticket's bar, and the block of text beside it.
const TICKET_W: f32 = 6.;
const TICKET_H: f32 = 56.;
const TICKET_LABEL_W: f32 = 150.;
/// A build package box: a fixed head (the role-label row) plus one 30px row
/// per lane, and the width its Implementer / critic marker pair needs.
const PKG_W: f32 = 236.;
const PKG_HEAD: f32 = 26.;
const LANE_H: f32 = 30.;
const PKG_GAP: f32 = 14.;
/// The integrate pill, and the width its tier rows need below it.
const PILL_W: f32 = 100.;
const PILL_H: f32 = 40.;
const TIERS_W: f32 = 176.;
/// A revise arc leaves the track this far above it, peaks at
/// `TRACK_Y - ARC_APEX_DY`, and stacks its "revise" label and its
/// "approved in round N" pill above the peak.
const ARC_RISE: f32 = 14.;
const ARC_CTRL_RISE: f32 = 58.;
const ARC_APEX_DY: f32 = 47.;

/// The return track rounds its two corners at this radius and drops this far
/// below the widest row before it turns back left.
const CORNER_R: f32 = 40.;
const RETURN_DROP: f32 = 220.;
/// The review hub's radius, and how far a spoke runs from the hub's edge.
const HUB_R: f32 = 60.;
const SPOKE_LEN: f32 = 150.;
/// A spoke ends in this ring, with its label this far beyond it.
const SPOKE_RING: f32 = 18.;
const SPOKE_LABEL_GAP: f32 = 13.;
const SPOKE_LABEL_W: f32 = 96.;
/// The Fix loop leans back under the hub on the lower-left, at an angle held
/// clear of the spoke fan's lower spread (20°–130°) so the two never collide.
const FIX_ANGLE: f32 = 150.;
/// How far a spoke's ring and label reach from the hub. The merge column and
/// the return track both have to clear the fan's outer edge, so they read it.
const FAN_REACH: f32 = HUB_R + SPOKE_LEN + SPOKE_RING / 2. + SPOKE_LABEL_GAP + SPOKE_LABEL_W;
/// The merge bar, and the clear width its checklist labels need to its right.
const MERGE_W: f32 = 6.;
const MERGE_H: f32 = 56.;
const MERGE_COL_W: f32 = 100.;
/// Where the merge bar sits when there is no fan to clear.
const MERGE_GAP: f32 = 160.;
/// The dotted line down to the human's circle, and that circle's diameter.
const HUMAN_D: f32 = 52.;
const HUMAN_DROP: f32 = 120.;
const CHECK_D: f32 = 14.;
/// A legend swatch, and a stage chip's ordinal circle.
const LEGEND_W: f32 = 22.;
const LEGEND_H: f32 = 3.;
const CHIP_D: f32 = 20.;
/// How wide a stage chip's jump glow is.
const STAGE_HIGHLIGHT_W: f32 = 150.;

/// The map, parsed out of the page's `flow` object. Every part is optional:
/// the builder is fail-soft, so a run with no build (or no verifiers) simply
/// has fewer parts to draw.
struct Flow {
    ticket_id: String,
    design: Vec<Step>,
    design_loops: Vec<ReviseLoop>,
    packages: Vec<Package>,
    build_loops: Vec<ReviseLoop>,
    integrate: Option<Step>,
    tiers: Vec<Tier>,
    review: Option<Review>,
    merge: Option<Merge>,
    rollback: Option<Rollback>,
    legend: Vec<Legend>,
    stages: Vec<Stage>,
    /// The run's `node_start` / `node_end` stream, in order — what a replay
    /// replays.
    timeline: Vec<TimelineEvent>,
}

/// One `flow.timeline` entry: a node's start or end at an epoch second, with
/// the state the replay clock paints the step in (`in_progress` for a start,
/// the recorded end state otherwise).
struct TimelineEvent {
    t: f64,
    node: String,
    state: String,
}

/// One node of the map, as the flow model spells it.
struct Step {
    id: String,
    label: String,
    sub: String,
    state: String,
    score: String,
    action: Value,
}

struct ReviseLoop {
    from: String,
    to: String,
    approved_round: Option<u64>,
}

struct Package {
    label: String,
    lanes: Vec<Lane>,
}

struct Lane {
    role: String,
    node: Step,
    critic: Option<Step>,
}

struct Tier {
    label: String,
    state: String,
    passed: f32,
    total: f32,
}

/// The review hub the return track ends at, with the spokes fanning off it.
struct Review {
    hub: Option<Step>,
    verdict: String,
    score: String,
    spokes: Vec<Spoke>,
    fix: Option<Fix>,
}

/// One radial spoke: a ring in its state colour, a label and a sub.
struct Spoke {
    label: String,
    sub: String,
    state: String,
}

/// The "Fix — then review again" spoke, present once a revise round fired.
struct Fix {
    rounds: u64,
    state: String,
}

/// The merge bar and its checklist, from `flow.merge` (null until code merges).
struct Merge {
    state: String,
    checks: Vec<Check>,
    human: Option<Human>,
}

struct Check {
    label: String,
    ok: bool,
}

struct Human {
    label: String,
    sub: String,
}

/// The rolled-back record, drawn at the lower line's end when nothing merged.
struct Rollback {
    paths: u64,
}

/// One legend entry: a swatch keyed to an edge state and its label.
struct Legend {
    key: String,
    label: String,
}

/// A stage chip along the bottom: its ordinal and its roll-up state.
struct Stage {
    key: String,
    label: String,
    n: u64,
    state: String,
}

impl Flow {
    fn parse(flow: &Value) -> Self {
        let ticket = flow.get("ticket").cloned().unwrap_or(Value::Null);
        let design = flow.get("design").cloned().unwrap_or(Value::Null);
        let build = flow.get("build").cloned().unwrap_or(Value::Null);
        let integrate = flow.get("integrate").cloned().unwrap_or(Value::Null);
        let review = flow.get("review").cloned().unwrap_or(Value::Null);
        let legend = flow.get("legend").cloned().unwrap_or(Value::Null);
        let timeline = flow.get("timeline").cloned().unwrap_or(Value::Null);
        Self {
            ticket_id: str_of(&ticket, "id"),
            design: arr(&design, "nodes").iter().filter_map(parse_step).collect(),
            design_loops: parse_loops(&design),
            packages: arr(&build, "packages").iter().filter_map(parse_package).collect(),
            build_loops: parse_loops(&build),
            integrate: integrate.get("node").and_then(parse_step),
            tiers: arr(&integrate, "tiers").iter().filter_map(parse_tier).collect(),
            review: parse_review(&review),
            merge: flow.get("merge").and_then(parse_merge),
            rollback: flow.get("rollback").and_then(parse_rollback),
            legend: parse_legend(&legend),
            stages: arr(flow, "stages").iter().filter_map(parse_stage).collect(),
            timeline: parse_timeline(&timeline),
        }
    }

    /// Every step the map draws — the design markers, each lane's implementer
    /// node and critic, the integrate pill and the review hub. The ticket and
    /// the spoke rings carry no node id, so the replay never touches them.
    fn steps(&self) -> impl Iterator<Item = &Step> {
        self.design
            .iter()
            .chain(
                self.packages
                    .iter()
                    .flat_map(|package| package.lanes.iter())
                    .flat_map(|lane| std::iter::once(&lane.node).chain(lane.critic.iter())),
            )
            .chain(self.integrate.iter())
            .chain(self.review.iter().filter_map(|review| review.hub.as_ref()))
    }

    /// Override the steps' states from the timeline at `now`: a step whose id
    /// the timeline mentions shows its last event at or before `now`, or
    /// `pending` before its first. A step the timeline never mentions keeps its
    /// page state. The stage chips re-roll up with the same worst-of rule
    /// `run_flow.py` uses.
    fn apply_replay(&mut self, now: f64) {
        let mut mentioned: HashSet<String> = HashSet::new();
        let mut reached: HashMap<String, String> = HashMap::new();
        for event in &self.timeline {
            mentioned.insert(event.node.clone());
            if event.t <= now {
                reached.insert(event.node.clone(), event.state.clone());
            }
        }
        let at = |id: &str| -> Option<String> {
            if !mentioned.contains(id) {
                return None;
            }
            Some(reached.get(id).cloned().unwrap_or_else(|| "pending".to_string()))
        };
        for step in self.design.iter_mut() {
            if let Some(state) = at(&step.id) {
                step.state = state;
            }
        }
        for package in self.packages.iter_mut() {
            for lane in package.lanes.iter_mut() {
                if let Some(state) = at(&lane.node.id) {
                    lane.node.state = state;
                }
                if let Some(critic) = lane.critic.as_mut() {
                    if let Some(state) = at(&critic.id) {
                        critic.state = state;
                    }
                }
            }
        }
        if let Some(step) = self.integrate.as_mut() {
            if let Some(state) = at(&step.id) {
                step.state = state;
            }
        }
        if let Some(review) = self.review.as_mut() {
            if let Some(hub) = review.hub.as_mut() {
                if let Some(state) = at(&hub.id) {
                    hub.state = state;
                }
            }
        }
        let rolled: Vec<Option<String>> =
            self.stages.iter().map(|stage| self.rolled_up(&stage.key)).collect();
        for (stage, state) in self.stages.iter_mut().zip(rolled) {
            if let Some(state) = state {
                stage.state = state;
            }
        }
    }

    /// A stage chip's state, rolled up the way `run_flow.py::build_flow` does:
    /// the worst of the steps a stage draws, the integrate node's own state,
    /// and the review hub's verdict (or the worst spoke when there is no hub).
    fn rolled_up(&self, key: &str) -> Option<String> {
        match key {
            "intake" => Some("done".to_string()),
            "design" => Some(worst(self.design.iter().map(|step| step.state.as_str()))),
            "build" => Some(worst(
                self.packages
                    .iter()
                    .flat_map(|package| package.lanes.iter())
                    .map(|lane| lane.node.state.as_str()),
            )),
            "integrate" => Some(
                self.integrate
                    .as_ref()
                    .map(|step| step.state.clone())
                    .unwrap_or_else(|| "pending".to_string()),
            ),
            "review" => self.review.as_ref().map(|review| {
                review
                    .hub
                    .as_ref()
                    .map(|hub| hub.state.clone())
                    .unwrap_or_else(|| worst(review.spokes.iter().map(|spoke| spoke.state.as_str())))
            }),
            "merge" => Some(
                self.merge
                    .as_ref()
                    .map(|merge| merge.state.clone())
                    .unwrap_or_else(|| "pending".to_string()),
            ),
            _ => None,
        }
    }
}

fn parse_timeline(value: &Value) -> Vec<TimelineEvent> {
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            if !item.is_object() {
                return None;
            }
            let node = str_of(item, "node");
            if node.is_empty() {
                return None;
            }
            Some(TimelineEvent {
                t: item.get("t").and_then(Value::as_f64)?,
                node,
                state: str_of(item, "state"),
            })
        })
        .collect()
}

fn parse_step(value: &Value) -> Option<Step> {
    if !value.is_object() {
        return None;
    }
    Some(Step {
        id: str_of(value, "id"),
        label: str_of(value, "label"),
        sub: str_of(value, "sub"),
        state: str_of(value, "state"),
        score: str_of(value, "score"),
        action: value.get("do").cloned().unwrap_or(Value::Null),
    })
}

fn parse_package(value: &Value) -> Option<Package> {
    if !value.is_object() {
        return None;
    }
    let lanes = arr(value, "lanes")
        .iter()
        .filter_map(|lane| {
            if !lane.is_object() {
                return None;
            }
            Some(Lane {
                role: str_of(lane, "role"),
                node: parse_step(lane.get("node")?)?,
                critic: lane.get("critic").and_then(parse_step),
            })
        })
        .collect();
    Some(Package { label: str_of(value, "label"), lanes })
}

fn parse_loops(value: &Value) -> Vec<ReviseLoop> {
    arr(value, "revise")
        .iter()
        .filter_map(|looped| {
            if !looped.is_object() {
                return None;
            }
            Some(ReviseLoop {
                from: str_of(looped, "from"),
                to: str_of(looped, "to"),
                approved_round: looped.get("approved_round").and_then(Value::as_u64),
            })
        })
        .collect()
}

fn parse_tier(value: &Value) -> Option<Tier> {
    if !value.is_object() {
        return None;
    }
    Some(Tier {
        label: str_of(value, "label"),
        state: str_of(value, "state"),
        passed: value.get("passed").and_then(Value::as_f64).unwrap_or(0.) as f32,
        total: value.get("total").and_then(Value::as_f64).unwrap_or(0.) as f32,
    })
}

fn parse_review(value: &Value) -> Option<Review> {
    if !value.is_object() {
        return None;
    }
    let fix_value = value.get("fix").cloned().unwrap_or(Value::Null);
    let rounds = fix_value.get("rounds").and_then(Value::as_u64).unwrap_or(0);
    Some(Review {
        hub: value.get("hub").and_then(parse_step),
        verdict: str_of(value, "verdict"),
        score: str_of(value, "score"),
        spokes: arr(value, "spokes").iter().filter_map(parse_spoke).collect(),
        // A Fix spoke only exists once a round has fired; rounds 0 is "none".
        fix: (rounds > 0).then(|| Fix { rounds, state: str_of(&fix_value, "state") }),
    })
}

fn parse_spoke(value: &Value) -> Option<Spoke> {
    if !value.is_object() {
        return None;
    }
    Some(Spoke {
        label: str_of(value, "label"),
        sub: str_of(value, "sub"),
        state: str_of(value, "state"),
    })
}

fn parse_merge(value: &Value) -> Option<Merge> {
    if !value.is_object() {
        return None;
    }
    Some(Merge {
        state: str_of(value, "state"),
        checks: arr(value, "checks").iter().filter_map(parse_check).collect(),
        human: value.get("human").filter(|human| human.is_object()).map(|human| Human {
            label: str_of(human, "label"),
            sub: str_of(human, "sub"),
        }),
    })
}

fn parse_check(value: &Value) -> Option<Check> {
    if !value.is_object() {
        return None;
    }
    Some(Check { label: str_of(value, "label"), ok: bool_of(value, "ok") })
}

fn parse_rollback(value: &Value) -> Option<Rollback> {
    if !value.is_object() {
        return None;
    }
    Some(Rollback { paths: value.get("paths").and_then(Value::as_u64).unwrap_or(0) })
}

fn parse_legend(value: &Value) -> Vec<Legend> {
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            if !item.is_object() {
                return None;
            }
            Some(Legend { key: str_of(item, "key"), label: str_of(item, "label") })
        })
        .collect()
}

fn parse_stage(value: &Value) -> Option<Stage> {
    if !value.is_object() {
        return None;
    }
    Some(Stage {
        key: str_of(value, "key"),
        label: str_of(value, "label"),
        n: value.get("n").and_then(Value::as_u64).unwrap_or(0),
        state: str_of(value, "state"),
    })
}

/// Where every marker, box and pill goes. Only x/y arithmetic here — no
/// elements — so the canvas and the divs read the same numbers.
struct Layout {
    width: f32,
    height: f32,
    ticket_x: f32,
    ticket_label_x: f32,
    /// The design markers' centre x, in draw order.
    design_x: Vec<f32>,
    /// Package boxes, in draw order, with their lanes' marker x.
    packages: Vec<PackageSpot>,
    fan_x: f32,
    fan_in_x: f32,
    integrate_x: f32,
    /// Revise arcs, as the two marker x they span.
    arcs: Vec<Arc>,
    /// The lower line's y, the review hub's centre x, the x the return track
    /// drops down on, and where the lower line stops on the left.
    return_y: f32,
    hub_x: f32,
    ret_x_right: f32,
    end_x: f32,
    /// The merge bar's x, when the run has a merge bar to draw.
    merge_x: Option<f32>,
    /// One anchor x per stage chip, in `Flow.stages` order.
    stage_x: Vec<f32>,
}

struct PackageSpot {
    left: f32,
    top: f32,
    lanes: Vec<LaneSpot>,
}

struct LaneSpot {
    node_x: f32,
    critic_x: Option<f32>,
}

struct Arc {
    from: f32,
    to: f32,
    approved_round: Option<u64>,
}

impl Layout {
    fn compute(map: &Flow) -> Self {
        let ticket_x = 8.;
        let ticket_label_x = ticket_x + TICKET_W + 12.;
        let mut cursor = ticket_label_x + TICKET_LABEL_W + 30.;

        let mut design_x = Vec::new();
        let mut node_x: HashMap<String, f32> = HashMap::new();
        for step in &map.design {
            design_x.push(cursor);
            node_x.insert(step.id.clone(), cursor);
            cursor += STEP_PITCH;
        }
        // The track's last point before the build fan: a step's pitch past the
        // final marker, or the cursor when there are no design steps at all.
        let fan_x = if design_x.is_empty() { cursor } else { cursor - STEP_PITCH + 66. };

        let package_left = fan_x + 44.;
        let mut packages = Vec::new();
        let mut package_top = TRACK_Y + 52.;
        for package in &map.packages {
            let mut lanes = Vec::new();
            for lane in &package.lanes {
                let lane_x = package_left + 96.;
                let critic_x = lane.critic.as_ref().map(|_| package_left + 168.);
                node_x.insert(lane.node.id.clone(), lane_x);
                if let (Some(critic), Some(critic_x)) = (&lane.critic, critic_x) {
                    node_x.insert(critic.id.clone(), critic_x);
                }
                lanes.push(LaneSpot { node_x: lane_x, critic_x });
            }
            let height = PKG_HEAD + LANE_H * package.lanes.len() as f32;
            packages.push(PackageSpot { left: package_left, top: package_top, lanes });
            package_top += height + PKG_GAP;
        }
        let package_bottom =
            if packages.is_empty() { TRACK_Y + 52. } else { package_top - PKG_GAP };
        let fan_in_x = package_left + PKG_W + 44.;
        let integrate_x = fan_in_x + 28.;

        // A design loop spans its two markers. A build loop returns to an
        // Implementer lane, and the node that sent it back is usually a
        // reviewer with no marker of its own — so it arches over the lane.
        let mut arcs = Vec::new();
        for looped in &map.design_loops {
            if let (Some(&from), Some(&to)) = (node_x.get(&looped.from), node_x.get(&looped.to)) {
                arcs.push(Arc { from, to, approved_round: looped.approved_round });
            }
        }
        // Several loops can return to one Implementer (typecheck, test and
        // reviewer all retry it), and every lane shares its x — so group the
        // build loops by target and draw one arc per target. Otherwise the
        // "revise" label and the round pill stack on the same spot.
        let mut build_arcs: Vec<Arc> = Vec::new();
        for looped in &map.build_loops {
            let Some(&to) = node_x.get(&looped.to) else { continue };
            // x is a whole-pixel coordinate, so half a pixel identifies the span.
            match build_arcs.iter_mut().find(|arc| (arc.to - (to + 88.)).abs() < 0.5) {
                Some(arc) => {
                    // A round is only shown when every merged loop agrees there
                    // is one; keep the highest when they do.
                    arc.approved_round = match (arc.approved_round, looped.approved_round) {
                        (Some(earliest), Some(round)) => Some(earliest.max(round)),
                        _ => None,
                    };
                }
                None => build_arcs.push(Arc {
                    from: to - 88.,
                    to: to + 88.,
                    approved_round: looped.approved_round,
                }),
            }
        }
        arcs.extend(build_arcs);

        let tiers_bottom = if map.integrate.is_some() {
            TRACK_Y + 26. + 18. * map.tiers.len() as f32
        } else {
            TRACK_Y + 20.
        };
        let body_bottom = package_bottom.max(tiers_bottom).max(TRACK_Y + 40.);

        // The fan only exists with a review, and the merge column and the
        // return track both have to clear its outer edge — so it decides
        // where they sit.
        let fan_reach = if map.review.is_some() { FAN_REACH } else { 0. };
        let hub_x = integrate_x + PILL_W / 2.;
        let ret_x_right =
            (integrate_x + TIERS_W + CORNER_R).max(hub_x + fan_reach + CORNER_R + 16.);
        let merge_x = map.merge.is_some().then(|| {
            if map.review.is_some() {
                hub_x - (fan_reach + MERGE_COL_W + 20.)
            } else {
                hub_x - MERGE_GAP
            }
        });
        // Without a merge bar the lower line stops at the hub — or just short
        // of it, so a rollback marker can take the bar's place.
        let end_x = match (merge_x, map.rollback.is_some()) {
            (Some(x), _) => x,
            (None, true) => hub_x - HUB_R,
            (None, false) => hub_x,
        };

        let (return_y, height, width) = if map.integrate.is_some() {
            let return_y = body_bottom + RETURN_DROP;
            // The chip row lands a fixed distance under the lowest thing the
            // map draws: the fan, the human's sub, or the hub itself.
            let fan_bottom =
                if map.review.is_some() { HUB_R + SPOKE_LEN + SPOKE_RING } else { HUB_R };
            let below = return_y + fan_bottom;
            let below = match map.merge.as_ref().and_then(|merge| merge.human.as_ref()) {
                Some(_) => below.max(return_y + HUMAN_DROP + HUMAN_D / 2. + 30.),
                None => below,
            };
            let height = below + 22. + CHIP_D + 16.;
            let width = (integrate_x + PILL_W)
                .max(integrate_x + TIERS_W)
                .max(hub_x + fan_reach + 24.)
                .max(ret_x_right)
                + 24.;
            (return_y, height, width)
        } else {
            // The chips run along the bottom even before there is a hub, so
            // their row has to be reserved here too.
            let chips_top = if map.stages.is_empty() { body_bottom } else { body_bottom + 22. };
            let height = chips_top + CHIP_D + 16.;
            let width = (integrate_x + PILL_W).max(integrate_x + TIERS_W) + 24.;
            (body_bottom, height, width)
        };

        // A stage chip scrolls to its stage's anchor: the marker, box or pill
        // that spells the stage on the map.
        let stage_x = map
            .stages
            .iter()
            .map(|stage| match stage.key.as_str() {
                "intake" => ticket_x + TICKET_W / 2.,
                "design" => design_x.first().copied().unwrap_or(fan_x),
                "build" => packages.first().map(|spot| spot.left + PKG_W / 2.).unwrap_or(fan_x),
                "integrate" => integrate_x + PILL_W / 2.,
                "review" => hub_x,
                "merge" => merge_x.map(|x| x + MERGE_W / 2.).unwrap_or(hub_x),
                _ => fan_x,
            })
            .collect();

        Self {
            width,
            height,
            ticket_x,
            ticket_label_x,
            design_x,
            packages,
            fan_x,
            fan_in_x,
            integrate_x,
            arcs,
            return_y,
            hub_x,
            ret_x_right,
            end_x,
            merge_x,
            stage_x,
        }
    }
}

/// The spokes' angles, split over the upper and lower arcs so neither the left
/// nor the right sector (where the track runs in) carries a line. The lower
/// spread stops at 130° to leave [`FIX_ANGLE`] its own slot.
fn spoke_angles(count: usize) -> Vec<f32> {
    let upper = (count + 1) / 2;
    let mut angles = spread_angles(upper, 200., 340.);
    angles.extend(spread_angles(count - upper, 20., 130.));
    angles
}

/// `count` angles spread evenly over `start..=end` degrees; one sits centred.
fn spread_angles(count: usize, start: f32, end: f32) -> Vec<f32> {
    if count == 0 {
        return Vec::new();
    }
    if count == 1 {
        return vec![(start + end) / 2.];
    }
    (0..count)
        .map(|index| start + (end - start) * index as f32 / (count - 1) as f32)
        .collect()
}

/// One stroked connector, in the map's own coordinates.
struct Stroke {
    from: (f32, f32),
    to: (f32, f32),
    ctrl: Option<((f32, f32), (f32, f32))>,
    colour: Hsla,
    width: f32,
}

impl Stroke {
    fn line(from: (f32, f32), to: (f32, f32), colour: Hsla, width: f32) -> Self {
        Self { from, to, ctrl: None, colour, width }
    }

    fn cubic(
        from: (f32, f32),
        to: (f32, f32),
        ctrl1: (f32, f32),
        ctrl2: (f32, f32),
        colour: Hsla,
        width: f32,
    ) -> Self {
        Self { from, to, ctrl: Some((ctrl1, ctrl2)), colour, width }
    }
}

/// The colour of a step's state, as the design's table spells it.
fn state_colour(state: &str) -> u32 {
    match state {
        "approved" => p::GREEN,
        "in_progress" => p::BLUE,
        "sent_back" => p::ORANGE,
        "failed" => p::RED,
        // The ticket is the one "done" step, and it draws in the text colour.
        "done" => p::TEXT,
        _ => p::BORDER2,
    }
}

fn edge_width(state: &str) -> f32 {
    if state == "pending" || state == "skipped" { EDGE_W_PENDING } else { EDGE_W }
}

/// Worst-first, the same ranking `run_flow._RANK` uses.
fn rank(state: &str) -> u8 {
    match state {
        "sent_back" | "failed" => 0,
        "in_progress" => 1,
        "approved" | "done" => 3,
        _ => 2,
    }
}

fn worst<'a>(states: impl Iterator<Item = &'a str>) -> String {
    states
        .min_by_key(|state| rank(state))
        .unwrap_or("pending")
        .to_string()
}

pub(crate) fn render_flow_part_a(
    page: &MiniOrkPage,
    flow: &Value,
    scroll: &ScrollHandle,
    window: &mut Window,
    cx: &mut Context<MiniOrkPage>,
) -> AnyElement {
    let mut map = Flow::parse(flow);
    // "Live" is read off the page's own states, before any replay override: a
    // run that is still running reloads on its own and needs no replay.
    let live = map.steps().any(|step| step.state == "in_progress");
    let run_id = page.args.get("run").cloned().unwrap_or_default();
    if page.replay.active && page.replay.run == run_id {
        map.apply_replay(page.replay.now);
    }
    let layout = Layout::compute(&map);
    let strokes = connectors(&map, &layout);
    let mono = p::mono_font(cx);
    let mut pieces: Vec<AnyElement> = Vec::new();

    // The replay controls, when the run carries a timeline to replay.
    if !map.timeline.is_empty() {
        pieces.push(replay_controls(page, &map, &run_id, live, &mono, cx));
    }

    // A stage chip's jump glows behind the whole map for 1.5s (`flow_focus`).
    if let Some((key, _)) = &page.flow_focus {
        if let Some(index) = map.stages.iter().position(|stage| &stage.key == key) {
            let anchor = layout.stage_x.get(index).copied().unwrap_or(layout.hub_x);
            pieces.push(
                div()
                    .absolute()
                    .left(px(anchor - STAGE_HIGHLIGHT_W / 2.))
                    .top(px(0.))
                    .w(px(STAGE_HIGHLIGHT_W))
                    .h(px(layout.height))
                    .bg(ca(0x74ade833))
                    .into_any_element(),
            );
        }
    }

    // The connectors go in first: GPUI paints children in insertion order, so
    // every marker and label below lands on top of the strokes.
    pieces.push(paint_strokes(strokes));

    // Ticket in: a thick bar, with the run's id under its title.
    pieces.push(
        div()
            .absolute()
            .left(px(layout.ticket_x))
            .top(px(TRACK_Y - TICKET_H / 2.))
            .w(px(TICKET_W))
            .h(px(TICKET_H))
            .bg(c(p::TEXT))
            .into_any_element(),
    );
    pieces.push(
        div()
            .absolute()
            .left(px(layout.ticket_label_x))
            .top(px(TRACK_Y - 20.))
            .w(px(TICKET_LABEL_W))
            .child(
                div()
                    .text_size(px(13.))
                    .font_weight(FontWeight::SEMIBOLD)
                    .text_color(c(p::TEXT))
                    .child("Ticket in"),
            )
            .child(p::text_px(map.ticket_id.clone(), 11., p::SUB).truncate())
            .into_any_element(),
    );

    // Design: a marker per step, its score above, its label and sub below.
    for (index, step) in map.design.iter().enumerate() {
        let centre_x = layout.design_x[index];
        let colour = state_colour(&step.state);
        if !step.score.is_empty() {
            pieces.push(
                div()
                    .absolute()
                    .left(px(centre_x - 60.))
                    .top(px(TRACK_Y - 34.))
                    .w(px(120.))
                    .text_center()
                    .text_size(px(12.))
                    .font(mono.clone())
                    .text_color(c(p::GREEN))
                    .child(step.score.clone())
                    .into_any_element(),
            );
        }
        let mut hit = div()
            .id(("flow-step", index))
            .absolute()
            .left(px(centre_x - 15.))
            .top(px(TRACK_Y - 15.))
            .size(px(30.))
            .flex()
            .items_center()
            .justify_center();
        if let Some(handler) = page.action_handler(Some(&step.action), cx) {
            hit = hit.cursor_pointer().on_click(handler);
        }
        pieces.push(hit.child(marker_ring(colour, step.state == "skipped")).into_any_element());
        // The state table draws a skipped step's label in SUB, the rest in TEXT.
        let label_colour = if step.state == "skipped" { p::SUB } else { p::TEXT };
        pieces.push(
            div()
                .absolute()
                .left(px(centre_x - 62.))
                .top(px(TRACK_Y + 16.))
                .w(px(124.))
                .text_center()
                .text_size(px(13.))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(c(label_colour))
                .child(step.label.clone())
                .into_any_element(),
        );
        if !step.sub.is_empty() {
            pieces.push(
                div()
                    .absolute()
                    .left(px(centre_x - 62.))
                    .top(px(TRACK_Y + 33.))
                    .w(px(124.))
                    .text_center()
                    .text_size(px(11.))
                    .text_color(c(p::SUB))
                    .child(step.sub.clone())
                    .into_any_element(),
            );
        }
    }

    // Build: one box per package, one lane line per lane with its markers.
    for (package_index, package) in map.packages.iter().enumerate() {
        let spot = &layout.packages[package_index];
        let height = PKG_HEAD + LANE_H * package.lanes.len() as f32;
        pieces.push(
            div()
                .absolute()
                .left(px(spot.left))
                .top(px(spot.top))
                .w(px(PKG_W))
                .h(px(height))
                .bg(ca(0x363c4766))
                .into_any_element(),
        );
        // "PACKAGE n" only earns its place once there is more than one box.
        if map.packages.len() >= 2 && !package.label.is_empty() {
            pieces.push(
                div()
                    .absolute()
                    .left(px(spot.left + PKG_W - 88.))
                    .top(px(spot.top + 6.))
                    .w(px(80.))
                    .text_right()
                    .text_size(px(10.))
                    .font(mono.clone())
                    .text_color(c(p::SUB))
                    .child(package.label.clone())
                    .into_any_element(),
            );
        }
        for (lane_index, lane) in package.lanes.iter().enumerate() {
            let lane_spot = &spot.lanes[lane_index];
            let lane_y = spot.top + PKG_HEAD + LANE_H * lane_index as f32 + LANE_H / 2.;
            let colour = state_colour(&lane.node.state);
            pieces.push(
                div()
                    .absolute()
                    .left(px(spot.left + 16.))
                    .top(px(lane_y - 20.))
                    .text_size(px(10.5))
                    .text_color(c(p::SUB))
                    .child(lane.role.clone())
                    .into_any_element(),
            );
            pieces.push(
                div()
                    .absolute()
                    .left(px(spot.left + 14.))
                    .top(px(lane_y - 1.))
                    .w(px(PKG_W - 28.))
                    .h(px(2.))
                    .rounded(px(1.))
                    .bg(c(colour))
                    .into_any_element(),
            );
            pieces.push(lane_marker(page, &lane.node, lane_spot.node_x, lane_y, cx));
            if let (Some(critic), Some(critic_x)) = (&lane.critic, lane_spot.critic_x) {
                pieces.push(lane_marker(page, critic, critic_x, lane_y, cx));
            }
        }
    }

    // The revise loops: an orange arc, "revise" at its peak, and the round the
    // source finally passed in when it did.
    for (index, arc) in layout.arcs.iter().enumerate() {
        let apex_x = (arc.from + arc.to) / 2.;
        pieces.push(
            div()
                .absolute()
                .left(px(apex_x - 60.))
                .top(px(TRACK_Y - ARC_APEX_DY - 14.))
                .w(px(120.))
                .text_center()
                .text_size(px(11.))
                .text_color(c(p::SUB))
                .child("revise")
                .into_any_element(),
        );
        if let Some(round) = arc.approved_round {
            pieces.push(
                div()
                    .absolute()
                    .left(px(apex_x - 62.))
                    .top(px(TRACK_Y - ARC_APEX_DY - 36.))
                    .w(px(124.))
                    .flex()
                    .justify_center()
                    .child(
                        div()
                            .id(("flow-approved", index))
                            .px(px(8.))
                            .py(px(2.))
                            .border_1()
                            .border_color(c(p::ORANGE))
                            .text_size(px(10.5))
                            .text_color(c(p::ORANGE))
                            .child(format!("approved in round {round}")),
                    )
                    .into_any_element(),
            );
        }
    }

    // Integrate: the pill on the track, its captions stacked above it so
    // neither reaches the pill's top edge (TRACK_Y - PILL_H / 2), and its
    // tiers below.
    if let Some(step) = &map.integrate {
        let colour = state_colour(&step.state);
        pieces.push(
            div()
                .absolute()
                .left(px(layout.integrate_x - 20.))
                .top(px(TRACK_Y - PILL_H / 2. - 40.))
                .w(px(PILL_W + 40.))
                .line_height(px(18.))
                .text_center()
                .text_size(px(14.))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(c(p::TEXT))
                .child(step.label.clone())
                .into_any_element(),
        );
        pieces.push(
            div()
                .absolute()
                .left(px(layout.integrate_x - 20.))
                .top(px(TRACK_Y - PILL_H / 2. - 20.))
                .w(px(PILL_W + 40.))
                .line_height(px(14.))
                .text_center()
                .text_size(px(11.))
                .text_color(c(p::SUB))
                .child(step.sub.clone())
                .into_any_element(),
        );
        pieces.push(
            div()
                .absolute()
                .left(px(layout.integrate_x))
                .top(px(TRACK_Y - PILL_H / 2.))
                .w(px(PILL_W))
                .h(px(PILL_H))
                .flex()
                .items_center()
                .justify_center()
                .rounded_full()
                .border_3()
                .border_color(c(p::TEXT))
                .child(pill_glyph(&step.state, colour))
                .into_any_element(),
        );
    }
    for (index, tier) in map.tiers.iter().enumerate() {
        let failed = tier.state == "failed";
        let colour = if failed { p::RED } else { p::GREEN };
        let filled = if tier.total > 0. { (tier.passed / tier.total).clamp(0., 1.) } else { 0. };
        pieces.push(
            h_flex()
                .absolute()
                .left(px(layout.integrate_x))
                .top(px(TRACK_Y + 26. + 18. * index as f32))
                .gap(px(6.))
                .items_center()
                .child(
                    div()
                        .w(px(84.))
                        .text_size(px(12.))
                        .text_color(c(p::BODY))
                        .truncate()
                        .child(tier.label.clone()),
                )
                .child(
                    div().w(px(80.)).h(px(4.)).bg(c(p::BORDER2)).child(
                        div()
                            .w(px(80. * filled))
                            .h(px(4.))
                            .bg(c(colour)),
                    ),
                )
                .into_any_element(),
        );
    }

    // The return track's lower line carries the review hub: its spokes fan out
    // over the upper and lower arcs, the Fix loop hangs off it, and the merge
    // bar (or a rollback marker) sits at the line's left end.
    if map.integrate.is_some() {
        if let Some(review) = &map.review {
            let (hub_x, hub_y) = (layout.hub_x, layout.return_y);
            if let Some(hub) = &review.hub {
                let colour = state_colour(&hub.state);
                let mut circle = v_flex()
                    .id("flow-review-hub")
                    .absolute()
                    .left(px(hub_x - HUB_R))
                    .top(px(hub_y - HUB_R))
                    .size(px(HUB_R * 2.))
                    .items_center()
                    .justify_center()
                    .rounded_full()
                    .bg(c(p::CARD))
                    .border_3()
                    .border_color(c(p::TEXT))
                    .child(
                        div()
                            .text_size(px(16.))
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(c(p::TEXT))
                            .child("Review"),
                    )
                    .when(!review.verdict.is_empty(), |this| {
                        this.child(
                            div()
                                .text_size(px(11.))
                                .text_color(c(colour))
                                .child(review.verdict.clone()),
                        )
                    })
                    .when(!review.score.is_empty(), |this| {
                        this.child(
                            div().text_size(px(11.)).text_color(c(p::SUB)).child(review.score.clone()),
                        )
                    });
                if let Some(handler) = page.action_handler(Some(&hub.action), cx) {
                    circle = circle.cursor_pointer().on_click(handler);
                }
                pieces.push(circle.into_any_element());
            }

            // One ring, label and sub per spoke, out at its angle's radius.
            for (spoke, angle) in review.spokes.iter().zip(spoke_angles(review.spokes.len()).iter()) {
                let radians = angle.to_radians();
                let (sin, cos) = radians.sin_cos();
                let ring_x = hub_x + (HUB_R + SPOKE_LEN) * cos;
                let ring_y = hub_y + (HUB_R + SPOKE_LEN) * sin;
                pieces.push(spoke_ring(ring_x, ring_y, &spoke.state));
                let right = cos >= 0.;
                let label_left = if right {
                    ring_x + SPOKE_LABEL_GAP
                } else {
                    ring_x - SPOKE_LABEL_GAP - SPOKE_LABEL_W
                };
                pieces.push(spoke_label(label_left, ring_y, &spoke.label, &spoke.sub, right));
            }

            // The Fix spoke: an orange ring, "Fix" and the rounds it cost,
            // green-outlined once the fix was approved.
            if let Some(fix) = &review.fix {
                let radians = FIX_ANGLE.to_radians();
                let (sin, cos) = radians.sin_cos();
                let ring_x = hub_x + (HUB_R + SPOKE_LEN) * cos;
                let ring_y = hub_y + (HUB_R + SPOKE_LEN) * sin;
                let approved = fix.state == "approved";
                let colour = if approved { p::GREEN } else { p::ORANGE };
                let label_left = ring_x - SPOKE_LABEL_GAP - SPOKE_LABEL_W;
                pieces.push(
                    div()
                        .absolute()
                        .left(px(ring_x - SPOKE_RING / 2.))
                        .top(px(ring_y - SPOKE_RING / 2.))
                        .size(px(SPOKE_RING))
                        .rounded_full()
                        .bg(c(p::CARD))
                        .border_2()
                        .border_color(c(colour))
                        .into_any_element(),
                );
                pieces.push(
                    div()
                        .absolute()
                        .left(px(label_left))
                        .top(px(ring_y - 9.))
                        .w(px(SPOKE_LABEL_W))
                        .text_right()
                        .text_size(px(13.))
                        .font_weight(FontWeight::SEMIBOLD)
                        .text_color(c(p::ORANGE))
                        .child("Fix")
                        .into_any_element(),
                );
                pieces.push(
                    div()
                        .absolute()
                        .left(px(label_left))
                        .top(px(ring_y + 8.))
                        .w(px(SPOKE_LABEL_W))
                        .text_right()
                        .text_size(px(11.))
                        .text_color(c(p::SUB))
                        .child(format!(
                            "then review again · {} {}",
                            fix.rounds,
                            if fix.rounds == 1 { "round" } else { "rounds" }
                        ))
                        .into_any_element(),
                );
            }
        }

        if let Some(merge) = &map.merge {
            pieces.extend(merge_elements(merge, &layout));
        } else if let Some(rollback) = &map.rollback {
            pieces.push(rollback_marker(rollback, &layout));
        }
    }

    // The card's own "Full screen" control: with the column DAG retired the
    // flow map is the only in-page way in, so it has to carry the button.
    pieces.push(
        div()
            .id("flow-full")
            .absolute()
            .top(px(6.))
            .left(px(12.))
            .h(px(24.))
            .px(px(9.))
            .flex()
            .items_center()
            .border_1()
            .border_color(c(p::BORDER2))
            .bg(c(p::BG))
            .text_size(px(12.))
            .text_color(c(p::MUTED))
            .cursor_pointer()
            .hover(|style| style.bg(c(p::HOVER)).text_color(c(p::TEXT)))
            .tooltip(ui::Tooltip::text("Full screen · inspect every node"))
            .child("⤢ Full screen")
            .on_click(cx.listener(
                |page: &mut MiniOrkPage,
                 _: &ClickEvent,
                 window: &mut Window,
                 cx: &mut Context<MiniOrkPage>| {
                    page.open_graph(None, window, cx);
                },
            ))
            .into_any_element(),
    );

    // The legend, from `flow.legend`, sits at the map's top right.
    if !map.legend.is_empty() {
        let mut row = h_flex().absolute().top(px(6.)).right(px(12.)).gap(px(14.)).items_center();
        for item in &map.legend {
            row = row.child(
                h_flex()
                    .gap(px(6.))
                    .items_center()
                    .child(
                        div().w(px(LEGEND_W)).h(px(LEGEND_H)).bg(c(state_colour(&item.key))),
                    )
                    .child(
                        div()
                            .text_size(px(11.))
                            .font(mono.clone())
                            .text_color(c(p::SUB))
                            .child(item.label.clone()),
                    ),
            );
        }
        pieces.push(row.into_any_element());
    }

    // The stage chips, from `flow.stages`, run along the bottom; a click
    // scrolls its stage into view and highlights it for 1.5s.
    if !map.stages.is_empty() {
        let mut row = h_flex()
            .absolute()
            .left(px(layout.ticket_x))
            .top(px(layout.height - CHIP_D - 12.))
            .gap(px(12.))
            .items_center();
        for (index, stage) in map.stages.iter().enumerate() {
            let anchor = layout.stage_x.get(index).copied().unwrap_or(layout.hub_x);
            let key = stage.key.clone();
            let jump_scroll = scroll.clone();
            let focused = page.flow_focus.as_ref().map(|(k, _)| k == &stage.key).unwrap_or(false);
            let filled = stage.state == "approved";
            let ring_colour = match stage.state.as_str() {
                "approved" => p::GREEN,
                "in_progress" => p::BLUE,
                "sent_back" | "failed" => p::ORANGE,
                _ => p::BORDER2,
            };
            row = row.child(
                h_flex()
                    .id(("flow-chip", index))
                    .cursor_pointer()
                    .px(px(8.))
                    .py(px(3.))
                    .gap(px(7.))
                    .items_center()
                    .border_1()
                    .border_color(c(if focused { p::BLUE } else { p::BORDER2 }))
                    .child(
                        div()
                            .size(px(CHIP_D))
                            .flex_none()
                            .flex()
                            .items_center()
                            .justify_center()
                            .rounded_full()
                            .border_2()
                            .border_color(c(ring_colour))
                            .when(filled, |this| this.bg(c(p::GREEN)))
                            .text_size(px(11.))
                            .font_weight(FontWeight::SEMIBOLD)
                            .text_color(c(if filled { p::CARD } else { ring_colour }))
                            .child(format!("{}", stage.n)),
                    )
                    .child(
                        div().text_size(px(13.)).text_color(c(p::TEXT)).child(stage.label.clone()),
                    )
                    .on_click(cx.listener(
                        move |page: &mut MiniOrkPage,
                              _: &ClickEvent,
                              _: &mut Window,
                              cx: &mut Context<MiniOrkPage>| {
                            // Scroll offsets are negative: x runs from 0 (left
                            // edge) down to -max_offset (fully scrolled).
                            let max = jump_scroll.max_offset().x;
                            let want = px((anchor - 120.).max(0.)).min(max);
                            jump_scroll.set_offset(point(-want, jump_scroll.offset().y));
                            let next = page.flow_focus.as_ref().map(|(_, seq)| *seq).unwrap_or(0) + 1;
                            page.flow_focus = Some((key.clone(), next));
                            cx.notify();
                            cx.spawn(async move |this, cx| {
                                cx.background_executor().timer(Duration::from_millis(1500)).await;
                                this.update(cx, |page, cx| {
                                    if page.flow_focus.as_ref().map(|(_, seq)| *seq) == Some(next) {
                                        page.flow_focus = None;
                                        cx.notify();
                                    }
                                })
                                .ok();
                            })
                            .detach();
                        },
                    )),
            );
        }
        row = row.child(
            div()
                .text_size(px(11.))
                .font(mono.clone())
                .text_color(c(p::SUB))
                .child("Click a stage to jump to it"),
        );
        pieces.push(row.into_any_element());
    }

    let surface = div()
        .relative()
        .w(px(layout.width))
        .h(px(layout.height))
        .children(pieces);
    let scroller = div()
        .id("flow-scroll")
        .w_full()
        .overflow_x_scroll()
        .track_scroll(scroll)
        .child(surface);
    v_flex()
        .w_full()
        .min_w_0()
        .p(px(14.))
        .border_1()
        .border_color(c(p::BORDER))
        .bg(c(p::CHROME))
        .child(
            div().relative().w_full().child(scroller).custom_scrollbars(
                Scrollbars::always_visible(ScrollAxes::Horizontal).tracked_scroll_handle(scroll),
                window,
                cx,
            ),
        )
        .into_any_element()
}

/// The replay controls, in the legend's mono 11px style. They sit just under
/// the map's Full screen control so the two top-left controls never overlap.
/// A run that is still running gets a `● live` badge in place of the toggle.
fn replay_controls(
    page: &MiniOrkPage,
    map: &Flow,
    run_id: &str,
    live: bool,
    mono: &Font,
    cx: &mut Context<MiniOrkPage>,
) -> AnyElement {
    let start = map.timeline.first().map(|event| event.t).unwrap_or(0.);
    let end = map.timeline.last().map(|event| event.t).unwrap_or(0.);
    let run = run_id.to_string();
    let playing = page.replay.active && page.replay.playing;

    let mut row = h_flex().absolute().top(px(40.)).left(px(12.)).gap(px(12.)).items_center();

    if live {
        row = row.child(
            div().text_size(px(11.)).font(mono.clone()).text_color(c(p::BLUE)).child("● live"),
        );
    } else {
        row = row.child(
            div()
                .id("flow-replay-toggle")
                .cursor_pointer()
                .text_size(px(11.))
                .font(mono.clone())
                .text_color(c(p::TEXT))
                .child(if playing { "❚❚ Pause" } else { "▶ Replay" })
                .on_click(cx.listener(
                    move |page: &mut MiniOrkPage,
                          _: &ClickEvent,
                          _: &mut Window,
                          cx: &mut Context<MiniOrkPage>| {
                        if page.replay.active && page.replay.playing {
                            // Pause: hold the clock, and keep the override so the
                            // map keeps the mid-replay picture.
                            page.replay.playing = false;
                        } else {
                            // Resume a paused clock, else start fresh at the
                            // timeline's first `t`.
                            let resuming = page.replay.active
                                && page.replay.run == run
                                && page.replay.now < page.replay.end;
                            if !resuming {
                                page.replay.run = run.clone();
                                page.replay.start = start;
                                page.replay.end = end;
                                page.replay.now = start;
                            }
                            page.replay.active = true;
                            page.replay.playing = true;
                            page.replay.epoch += 1;
                            replay_ticker(page.replay.epoch, cx);
                        }
                        cx.notify();
                    },
                )),
        );
    }

    row = row.child(
        div()
            .id("flow-replay-restart")
            .cursor_pointer()
            .text_size(px(11.))
            .font(mono.clone())
            .text_color(c(p::SUB))
            .child("↺ Restart")
            .on_click(cx.listener(
                |page: &mut MiniOrkPage,
                 _: &ClickEvent,
                 _: &mut Window,
                 cx: &mut Context<MiniOrkPage>| {
                    page.stop_replay();
                    cx.notify();
                },
            )),
    );

    let mut speeds = h_flex().gap(px(6.)).items_center();
    for (index, choice) in [0.5_f32, 1., 2.].iter().enumerate() {
        if index > 0 {
            speeds =
                speeds.child(div().text_size(px(11.)).font(mono.clone()).text_color(c(p::DIM)).child("·"));
        }
        let selected = (page.replay.speed - *choice).abs() < 1e-3;
        let colour = if selected { p::TEXT } else { p::SUB };
        let choice = *choice;
        speeds = speeds.child(
            div()
                .id(("flow-replay-speed", index))
                .cursor_pointer()
                .text_size(px(11.))
                .font(mono.clone())
                .text_color(c(colour))
                .child(speed_label(choice))
                .on_click(cx.listener(
                    move |page: &mut MiniOrkPage,
                          _: &ClickEvent,
                          _: &mut Window,
                          cx: &mut Context<MiniOrkPage>| {
                        page.replay.speed = choice;
                        cx.notify();
                    },
                )),
        );
    }
    row.child(speeds).into_any_element()
}

/// `0.5×` / `1×` / `2×` — a whole number drops its `.0`.
fn speed_label(speed: f32) -> String {
    if (speed - speed.round()).abs() < 1e-3 {
        format!("{}×", speed.round() as i64)
    } else {
        format!("{speed}×")
    }
}

/// Tick the replay clock every 50 ms while `epoch` is still the page's current
/// one. The clock advances `speed × span / 12s` per real second, so a whole
/// timeline plays in about 12 seconds at 1×. A tick that crosses `end` stops
/// the clock and drops the override, showing the real states again.
fn replay_ticker(epoch: u64, cx: &mut Context<MiniOrkPage>) {
    cx.spawn(async move |this, cx| loop {
        cx.background_executor().timer(Duration::from_millis(50)).await;
        let ticking = this.update(cx, |page, cx| {
            if page.replay.epoch != epoch || !page.replay.playing {
                return false;
            }
            let span = (page.replay.end - page.replay.start).max(0.);
            page.replay.now += 0.05 * span * page.replay.speed as f64 / 12.;
            if page.replay.now >= page.replay.end {
                page.replay.now = page.replay.end;
                page.replay.playing = false;
                page.replay.active = false;
            }
            cx.notify();
            page.replay.playing
        });
        // Stop on a stale epoch, a failed update, or the clock reaching its end.
        if !matches!(ticking, Ok(true)) {
            break;
        }
    })
    .detach();
}

/// A marker inside a build lane: the same ring the design markers use, at the
/// lane's own anchor.
fn lane_marker(
    page: &MiniOrkPage,
    step: &Step,
    centre_x: f32,
    centre_y: f32,
    cx: &mut Context<MiniOrkPage>,
) -> AnyElement {
    let colour = state_colour(&step.state);
    let mut hit = div()
        .id(SharedString::from(format!("flow-lane-{}", step.id)))
        .absolute()
        .left(px(centre_x - 15.))
        .top(px(centre_y - 15.))
        .size(px(30.))
        .flex()
        .items_center()
        .justify_center();
    if let Some(handler) = page.action_handler(Some(&step.action), cx) {
        hit = hit.cursor_pointer().on_click(handler);
    }
    hit.child(marker_ring(colour, step.state == "skipped")).into_any_element()
}

/// The ring and its inner dot, in the step's state colour. A skipped step is
/// drawn hollow — the ring alone.
fn marker_ring(colour: u32, hollow: bool) -> Div {
    let ring = div()
        .size(px(MARKER_D))
        .flex_none()
        .flex()
        .items_center()
        .justify_center()
        .rounded_full()
        .bg(c(p::CARD))
        .border_3()
        .border_color(c(colour));
    if hollow {
        ring
    } else {
        ring.child(div().size(px(DOT_D)).rounded_full().bg(c(colour)))
    }
}

/// What the integrate pill shows: three dots while the tiers run, then the
/// verdict.
fn pill_glyph(state: &str, colour: u32) -> AnyElement {
    match state {
        "approved" | "done" => p::text_px("✓", 16., p::GREEN).into_any_element(),
        "failed" => p::text_px("✗", 16., p::RED).into_any_element(),
        "in_progress" => h_flex()
            .gap(px(3.))
            .children((0..3).map(|_| {
                div().size(px(4.)).rounded_full().bg(c(colour)).into_any_element()
            }))
            .into_any_element(),
        _ => div().into_any_element(),
    }
}

/// A spoke's terminus: a hollow ring in the spoke's state colour.
fn spoke_ring(centre_x: f32, centre_y: f32, state: &str) -> AnyElement {
    div()
        .absolute()
        .left(px(centre_x - SPOKE_RING / 2.))
        .top(px(centre_y - SPOKE_RING / 2.))
        .size(px(SPOKE_RING))
        .rounded_full()
        .bg(c(p::CARD))
        .border_2()
        .border_color(c(state_colour(state)))
        .into_any_element()
}

/// A spoke's label and its sub, beside the ring — to its right, or left when
/// the spoke leans left.
fn spoke_label(left: f32, centre_y: f32, label: &str, sub: &str, right: bool) -> AnyElement {
    let mut label_div = div()
        .text_size(px(13.))
        .font_weight(FontWeight::SEMIBOLD)
        .text_color(c(p::TEXT));
    let mut sub_div = div().text_size(px(11.)).text_color(c(p::SUB));
    if !right {
        label_div = label_div.text_right();
        sub_div = sub_div.text_right();
    }
    let mut block = v_flex()
        .absolute()
        .left(px(left))
        .top(px(centre_y - 9.))
        .w(px(SPOKE_LABEL_W))
        .child(label_div.child(label.to_string()));
    if !sub.is_empty() {
        block = block.child(sub_div.child(sub.to_string()));
    }
    block.into_any_element()
}

/// The merge bar, its label and checklist, and the human's circle below it.
fn merge_elements(merge: &Merge, layout: &Layout) -> Vec<AnyElement> {
    let mut pieces = Vec::new();
    let x = layout.merge_x.unwrap_or(layout.hub_x);
    pieces.push(
        div()
            .absolute()
            .left(px(x - MERGE_W / 2.))
            .top(px(layout.return_y - MERGE_H / 2.))
            .w(px(MERGE_W))
            .h(px(MERGE_H))
            .bg(c(state_colour(&merge.state)))
            .into_any_element(),
    );
    pieces.push(
        div()
            .absolute()
            .left(px(x - 44.))
            .top(px(layout.return_y - MERGE_H / 2. - 20.))
            .w(px(88.))
            .text_center()
            .text_size(px(13.))
            .font_weight(FontWeight::SEMIBOLD)
            .text_color(c(p::TEXT))
            .child("Merge")
            .into_any_element(),
    );
    // The checks stack upward from the bar.
    let checks_bottom = layout.return_y - MERGE_H / 2. - 26.;
    let count = merge.checks.len() as f32;
    for (index, check) in merge.checks.iter().enumerate() {
        let row_top = checks_bottom - (count - index as f32) * 20.;
        let mut tick = div()
            .absolute()
            .left(px(x - 70.))
            .top(px(row_top))
            .size(px(CHECK_D))
            .flex()
            .items_center()
            .justify_center()
            .border_1()
            .border_color(c(if check.ok { p::GREEN } else { p::BORDER2 }));
        if check.ok {
            tick = tick.child(p::text_px("✓", 10., p::GREEN));
        }
        pieces.push(tick.into_any_element());
        pieces.push(
            div()
                .absolute()
                .left(px(x - 70. + CHECK_D + 6.))
                .top(px(row_top + 1.))
                .w(px(150.))
                .text_size(px(11.))
                .text_color(c(p::BODY))
                .child(check.label.clone())
                .into_any_element(),
        );
    }
    if let Some(human) = &merge.human {
        let human_y = layout.return_y + HUMAN_DROP;
        pieces.push(
            div()
                .absolute()
                .left(px(x - HUMAN_D / 2.))
                .top(px(human_y - HUMAN_D / 2.))
                .size(px(HUMAN_D))
                .flex()
                .items_center()
                .justify_center()
                .rounded_full()
                .bg(c(p::CARD))
                .border_2()
                .border_color(c(p::BORDER2))
                .child(gpui::svg().path(IconName::Person.path()).size(px(24.)).text_color(c(p::BODY)))
                .into_any_element(),
        );
        pieces.push(
            div()
                .absolute()
                .left(px(x - 60.))
                .top(px(human_y + HUMAN_D / 2. + 4.))
                .w(px(120.))
                .text_center()
                .text_size(px(13.))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(c(p::TEXT))
                .child(human.label.clone())
                .into_any_element(),
        );
        if !human.sub.is_empty() {
            pieces.push(
                div()
                    .absolute()
                    .left(px(x - 70.))
                    .top(px(human_y + HUMAN_D / 2. + 20.))
                    .w(px(140.))
                    .text_center()
                    .text_size(px(11.))
                    .text_color(c(p::SUB))
                    .child(human.sub.clone())
                    .into_any_element(),
            );
        }
    }
    pieces
}

/// The red end marker the lower line closes with when the run rolled back. It
/// sits just left of where the line stops, so the two never overlap.
fn rollback_marker(rollback: &Rollback, layout: &Layout) -> AnyElement {
    let plural = if rollback.paths == 1 { "file" } else { "files" };
    h_flex()
        .absolute()
        .left(px(layout.end_x - 198.))
        .top(px(layout.return_y - 10.))
        .w(px(192.))
        .justify_center()
        .gap(px(6.))
        .items_center()
        .child(div().size(px(10.)).flex_none().rounded_full().bg(c(p::RED)))
        .child(
            div()
                .text_size(px(12.))
                .font_weight(FontWeight::SEMIBOLD)
                .text_color(c(p::RED))
                .child(format!("Rolled back · {} {plural}", rollback.paths)),
        )
        .into_any_element()
}

/// Every connector, in one canvas. The chart is small and fully known at
/// paint time, so there is no hit-testing or model state to keep.
fn paint_strokes(strokes: Vec<Stroke>) -> AnyElement {
    canvas(
        |_, _, _| (),
        move |bounds: Bounds<Pixels>, _, window, _| {
            let origin = bounds.origin;
            let at = |x: f32, y: f32| -> Point<Pixels> { point(origin.x + px(x), origin.y + px(y)) };
            for stroke in &strokes {
                let mut path = PathBuilder::stroke(px(stroke.width));
                path.move_to(at(stroke.from.0, stroke.from.1));
                match stroke.ctrl {
                    Some((ctrl1, ctrl2)) => path.cubic_bezier_to(
                        at(stroke.to.0, stroke.to.1),
                        at(ctrl1.0, ctrl1.1),
                        at(ctrl2.0, ctrl2.1),
                    ),
                    None => path.line_to(at(stroke.to.0, stroke.to.1)),
                }
                if let Ok(path) = path.build() {
                    window.paint_path(path, stroke.colour);
                }
            }
        },
    )
    .absolute()
    .top_0()
    .left_0()
    .size_full()
    .into_any_element()
}

/// The track, the build fan, and the revise arcs — each coloured by the state
/// of the step it leads to.
fn connectors(map: &Flow, layout: &Layout) -> Vec<Stroke> {
    let mut strokes = Vec::new();
    let integrate_state =
        map.integrate.as_ref().map(|step| step.state.as_str()).unwrap_or("pending");
    let build_state = worst(
        map.packages.iter().flat_map(|package| package.lanes.iter()).map(|lane| lane.node.state.as_str()),
    );
    let build_state = if map.packages.is_empty() { integrate_state } else { build_state.as_str() };

    // Ticket → the first design marker (or straight to the build fan).
    let first_x = layout.design_x.first().copied().unwrap_or(layout.fan_x);
    let first_state =
        map.design.first().map(|step| step.state.as_str()).unwrap_or(build_state);
    strokes.push(Stroke::line(
        (layout.ticket_x + TICKET_W, TRACK_Y),
        (first_x, TRACK_Y),
        c(state_colour(first_state)),
        edge_width(first_state),
    ));

    // Marker → marker, then the last marker → the fan point.
    for index in 1..layout.design_x.len() {
        let state = map.design[index].state.as_str();
        strokes.push(Stroke::line(
            (layout.design_x[index - 1], TRACK_Y),
            (layout.design_x[index], TRACK_Y),
            c(state_colour(state)),
            edge_width(state),
        ));
    }
    if let Some(&last) = layout.design_x.last() {
        strokes.push(Stroke::line(
            (last, TRACK_Y),
            (layout.fan_x, TRACK_Y),
            c(state_colour(build_state)),
            edge_width(build_state),
        ));
    }

    // Build: the track fans out into every lane, and every lane back in to
    // the integrate pill.
    for (package_index, package) in map.packages.iter().enumerate() {
        let spot = &layout.packages[package_index];
        for (lane_index, lane) in package.lanes.iter().enumerate() {
            let lane_y = spot.top + PKG_HEAD + LANE_H * lane_index as f32 + LANE_H / 2.;
            let state = lane.node.state.as_str();
            strokes.push(Stroke::cubic(
                (layout.fan_x, TRACK_Y),
                (spot.left, lane_y),
                (layout.fan_x + 36., TRACK_Y),
                (spot.left - 36., lane_y),
                c(state_colour(state)),
                edge_width(state),
            ));
            strokes.push(Stroke::cubic(
                (spot.left + PKG_W, lane_y),
                (layout.fan_in_x, TRACK_Y),
                (spot.left + PKG_W + 36., lane_y),
                (layout.fan_in_x - 36., TRACK_Y),
                c(state_colour(integrate_state)),
                edge_width(integrate_state),
            ));
        }
    }
    strokes.push(Stroke::line(
        (layout.fan_in_x, TRACK_Y),
        (layout.integrate_x, TRACK_Y),
        c(state_colour(integrate_state)),
        edge_width(integrate_state),
    ));

    // Revise loops: one arc per fired loop, over the run above the track.
    for arc in &layout.arcs {
        strokes.push(Stroke::cubic(
            (arc.from, TRACK_Y - ARC_RISE),
            (arc.to, TRACK_Y - ARC_RISE),
            (arc.from, TRACK_Y - ARC_CTRL_RISE),
            (arc.to, TRACK_Y - ARC_CTRL_RISE),
            c(p::ORANGE),
            EDGE_W,
        ));
    }

    // The return track — off the pill, down the right side, back left along
    // the lower line — and the review hub's spokes fanning out over the upper
    // and lower arcs.
    if map.integrate.is_some() {
        let review_state = map
            .review
            .as_ref()
            .and_then(|review| review.hub.as_ref())
            .map(|hub| hub.state.as_str())
            .unwrap_or("pending");
        let colour = c(state_colour(review_state));
        let width = edge_width(review_state);
        strokes.push(Stroke::line(
            (layout.integrate_x + PILL_W, TRACK_Y),
            (layout.ret_x_right - CORNER_R, TRACK_Y),
            colour,
            width,
        ));
        strokes.push(Stroke::cubic(
            (layout.ret_x_right - CORNER_R, TRACK_Y),
            (layout.ret_x_right, TRACK_Y + CORNER_R),
            (layout.ret_x_right, TRACK_Y),
            (layout.ret_x_right, TRACK_Y),
            colour,
            width,
        ));
        strokes.push(Stroke::line(
            (layout.ret_x_right, TRACK_Y + CORNER_R),
            (layout.ret_x_right, layout.return_y - CORNER_R),
            colour,
            width,
        ));
        strokes.push(Stroke::cubic(
            (layout.ret_x_right, layout.return_y - CORNER_R),
            (layout.ret_x_right - CORNER_R, layout.return_y),
            (layout.ret_x_right, layout.return_y),
            (layout.ret_x_right, layout.return_y),
            colour,
            width,
        ));
        strokes.push(Stroke::line(
            (layout.ret_x_right - CORNER_R, layout.return_y),
            (layout.end_x, layout.return_y),
            colour,
            width,
        ));

        if let Some(review) = &map.review {
            for angle in spoke_angles(review.spokes.len()) {
                let radians = angle.to_radians();
                let (sin, cos) = radians.sin_cos();
                strokes.push(Stroke::line(
                    (layout.hub_x + HUB_R * cos, layout.return_y + HUB_R * sin),
                    (
                        layout.hub_x + (HUB_R + SPOKE_LEN) * cos,
                        layout.return_y + (HUB_R + SPOKE_LEN) * sin,
                    ),
                    c(p::BORDER2),
                    2.,
                ));
            }
            // The Fix spoke is a double line, held clear of the fan's angle.
            if let Some(fix) = &review.fix {
                let radians = FIX_ANGLE.to_radians();
                let (sin, cos) = radians.sin_cos();
                let fix_colour = c(if fix.state == "approved" { p::GREEN } else { p::ORANGE });
                let from = (layout.hub_x + HUB_R * cos, layout.return_y + HUB_R * sin);
                let to = (
                    layout.hub_x + (HUB_R + SPOKE_LEN) * cos,
                    layout.return_y + (HUB_R + SPOKE_LEN) * sin,
                );
                for offset in [-2.5_f32, 2.5] {
                    let (dx, dy) = (-sin * offset, cos * offset);
                    strokes.push(Stroke::line(
                        (from.0 + dx, from.1 + dy),
                        (to.0 + dx, to.1 + dy),
                        fix_colour,
                        2.,
                    ));
                }
            }
        }

        // A dotted line drops from the merge bar to the human's circle.
        if let Some(merge) = &map.merge {
            if merge.human.is_some() {
                let x = layout.merge_x.unwrap_or(layout.hub_x);
                let end = layout.return_y + HUMAN_DROP - HUMAN_D / 2.;
                let mut y = layout.return_y + MERGE_H / 2.;
                while y < end {
                    let stop = (y + 3.).min(end);
                    strokes.push(Stroke::line((x, y), (x, stop), c(p::BORDER2), 2.));
                    y += 7.;
                }
            }
        }
    }

    strokes
}
