//! Section renderers (MINI_ORK_IDE_SPEC=2). Each renderer is called from
//! `MiniOrkPage::render_section`, which already supplies the section chrome
//! (a card with title and notes) for everything except `story` (which draws
//! its own rows full-width without a card header) and `agents` (rows separated
//! by `ROW_RULE`).

use std::sync::Arc;

use gpui::{AnyElement, App, ClickEvent, Context, KeyDownEvent, SharedString, Window, px};
use serde_json::Value;
use ui::{DiffStat, Divider, DividerColor, Indicator, prelude::*};

use crate::{
    components::{card, diff_lines, diff_sections, section_for, status_mark},
    page::{arr, code_box, str_of, MiniOrkPage},
    palette::{self as p, c, named},
    select::Look,
    store::{self, Origin},
};

impl MiniOrkPage {
    pub(crate) fn render_triage(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let tone = str_of(s, "tone");
        let text = str_of(s, "text");
        let icon = str_of(s, "icon");
        let detail = str_of(s, "detail");
        let counts = arr(s, "counts");
        let actions = arr(s, "actions");
        let menu = arr(s, "menu");
        let tone_colour = named(&tone);

        let accent = div()
            .absolute()
            .left_0()
            .top(rems_from_px(p::RADIUS_CARD))
            .bottom(rems_from_px(p::RADIUS_CARD))
            .w(rems_from_px(3_f32))
            .rounded_full()
            .bg(tone_colour);

        let row1 = h_flex()
            .gap_2p5()
            .items_center()
            .flex_wrap()
            .child(
                div()
                    .flex_none()
                    .text_size(rems_from_px(14_f32))
                    .text_color(tone_colour)
                    .child(SharedString::from(icon)),
            )
            .child(
                self.texts.text(
                    format!("s{index}-triage-text"),
                    &text,
                    Look::new(13., c(p::TEXT)).bold(true),
                    cx,
                ),
            )
            .child(
                h_flex()
                    .flex_1()
                    .min_w(px(0.))
                    .justify_end()
                    .gap_1p5()
                    .flex_wrap()
                    .children(counts.iter().enumerate().map(|(i, item)| {
                        let colour = named(&str_of(item, "c"));
                        let tint = colour.opacity(0.12);
                        h_flex()
                            .flex_none()
                            .id(SharedString::from(format!("s{index}-count{i}")))
                            .h(rems_from_px(18_f32))
                            .px_2()
                            .items_center()
                            .rounded_full()
                            .bg(tint)
                            .text_size(rems_from_px(p::BADGE))
                            .text_color(colour)
                            .child(str_of(item, "t"))
                            .into_any_element()
                    })),
            );

        let mut body = v_flex().relative().gap_2().child(row1);

        if !detail.is_empty() {
            body = body.child(self.texts.text(
                format!("s{index}-triage-detail"),
                &detail,
                Look::new(11.5, c(p::MUTED)),
                cx,
            ));
        }

        if !actions.is_empty() || !menu.is_empty() {
            let mut btn_row = h_flex().gap_1p5().flex_wrap().items_center();
            for (i, b) in actions.iter().enumerate() {
                btn_row = btn_row.child(
                    self.button(SharedString::from(format!("s{index}-triage-a{i}")), b, 24., 12., cx),
                );
            }
            if !menu.is_empty() {
                if !actions.is_empty() {
                    btn_row = btn_row.child(Divider::vertical().h_4().color(DividerColor::Border));
                }
                for (i, b) in menu.iter().enumerate() {
                    let mut ghost = b.clone();
                    if let Some(map) = ghost.as_object_mut() {
                        map.insert("kind".to_string(), Value::String("ghost".to_string()));
                    }
                    btn_row = btn_row.child(
                        self.button(SharedString::from(format!("s{index}-triage-m{i}")), &ghost, 24., 12., cx),
                    );
                }
            }
            body = body.child(btn_row);
        }

        card()
            .relative()
            .flex_1()
            .min_w_0()
            .p_3p5()
            .child(accent)
            .child(body)
            .into_any_element()
    }

    pub(crate) fn render_callout(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let tone = str_of(s, "tone");
        let title = str_of(s, "title");
        let body_md = str_of(s, "text_md");
        let icon = str_of(s, "icon");
        let meta = arr(s, "meta");
        let actions = arr(s, "actions");
        let tone_colour = named(&tone);
        // The engine's icon when it has one, else a mark derived from the tone.
        // The old hardcoded "?" meant a red failure and a green note were drawn
        // identically, with a glyph that described neither.
        let mark = if icon.is_empty() {
            SharedString::from(p::tone_mark(&tone))
        } else {
            SharedString::from(icon)
        };

        let head = h_flex()
            .gap_2()
            .items_center()
            .child(
                div()
                    .flex_none()
                    .text_size(rems_from_px(13_f32))
                    .font_weight(gpui::FontWeight::SEMIBOLD)
                    .text_color(tone_colour)
                    .child(mark),
            )
            .when(!title.is_empty(), |this| {
                this.child(self.texts.text(
                    format!("s{index}-callout-title"),
                    &title,
                    Look::new(13., c(p::TEXT)).bold(true),
                    cx,
                ))
            });

        let mut body = v_flex().gap_2p5().child(head);

        if !body_md.is_empty() {
            body = body.child(self.texts.markdown(
                format!("s{index}-callout-md"),
                &body_md,
                Look::new(12.5, c(p::BODY)),
                cx,
            ));
        }

        if !meta.is_empty() {
            // Same shape as the hero's meta row: `·`-separated cells, so a
            // reader sees what run a row is about (id, recipe, attempt, age)
            // without opening it.
            let mut row = h_flex().gap_1p5().items_center().flex_wrap();
            for (i, item) in meta.iter().enumerate() {
                let colour = named(&str_of(item, "c"));
                let mono_flag = item.get("mono").and_then(Value::as_bool).unwrap_or(false);
                row = row.child(self.texts.text(
                    format!("s{index}-callout-m{i}"),
                    str_of(item, "t"),
                    Look::new(11., colour).mono(mono_flag),
                    cx,
                ));
                if i + 1 < meta.len() {
                    row = row.child(
                        div()
                            .text_size(rems_from_px(11_f32))
                            .text_color(c(p::DIM))
                            .child(SharedString::from("·")),
                    );
                }
            }
            body = body.child(row);
        }

        if !actions.is_empty() {
            body = body.child(
                h_flex()
                    .justify_end()
                    .gap_1p5()
                    .flex_wrap()
                    .children(actions.iter().enumerate().map(|(i, b)| {
                        self.button(SharedString::from(format!("s{index}-callout-a{i}")), b, 24., 12., cx)
                    })),
            );
        }

        // No `.p_3()`: `card()` already carries it.
        card()
            .flex_1()
            .min_w_0()
            .border_color(tone_colour.opacity(0.45))
            .bg(tone_colour.opacity(0.06))
            .child(body)
            .into_any_element()
    }

    pub(crate) fn render_hero(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let title = str_of(s, "title");
        let goal = str_of(s, "goal");
        let pill = s.get("pill").filter(|v| !v.is_null());
        let meta = arr(s, "meta");
        let criteria = arr(s, "criteria");

        let mut content = v_flex().gap_2p5();

        if !title.is_empty() || pill.is_some() {
            let mut title_row = h_flex().gap_2p5().items_center().flex_wrap();
            if !title.is_empty() {
                title_row = title_row.child(
                    div()
                        .flex_1()
                        .min_w(px(0.))
                        .text_size(rems_from_px(11_f32))
                        .font_weight(gpui::FontWeight::SEMIBOLD)
                        .text_color(c(p::MUTED))
                        .child(SharedString::from(title.to_uppercase())),
                );
            }
            if let Some(pill_value) = pill {
                // Inlined (instead of components::state_pill) because the pill's
                // colour comes from the page's `c`, not from a state word, so
                // state_pill's palette::state_mark lookup would ignore it.
                let t = str_of(pill_value, "t");
                let colour = named(&str_of(pill_value, "c"));
                if !t.is_empty() {
                    title_row = title_row.child(
                        h_flex()
                            .flex_none()
                            .h(rems_from_px(18_f32))
                            .px_2()
                            .items_center()
                            .rounded_full()
                            .bg(colour.opacity(0.15))
                            .text_size(rems_from_px(p::PILL))
                            .text_color(colour)
                            .child(SharedString::from(t.to_uppercase())),
                    );
                }
            }
            content = content.child(title_row);
        }

        if !goal.is_empty() {
            content = content.child(self.texts.markdown(
                format!("s{index}-hero-goal"),
                &goal,
                Look::new(14., c(p::TEXT)),
                cx,
            ));
        }

        if !criteria.is_empty() {
            content = content.child(
                v_flex()
                    .gap_1()
                    .mt(px(2.))
                    .children(criteria.iter().enumerate().map(|(i, crit)| {
                        let text = crit.as_str().unwrap_or_default().to_string();
                        h_flex()
                            .gap_2()
                            .items_center()
                            .child(
                                div()
                                    .flex_none()
                                    .text_size(rems_from_px(12.5_f32))
                                    .text_color(c(p::MUTED))
                                    .child(SharedString::from("○")),
                            )
                            .child(self.texts.text(
                                format!("s{index}-hero-c{i}"),
                                &text,
                                Look::new(12.5, c(p::BODY)),
                                cx,
                            ))
                    })),
            );
        }

        if !meta.is_empty() {
            let mut row = h_flex().gap_1p5().items_center().flex_wrap();
            for (i, item) in meta.iter().enumerate() {
                let colour = named(&str_of(item, "c"));
                let mono_flag = item.get("mono").and_then(Value::as_bool).unwrap_or(false);
                row = row.child(self.texts.text(
                    format!("s{index}-hero-m{i}"),
                    str_of(item, "t"),
                    Look::new(11., colour).mono(mono_flag),
                    cx,
                ));
                if i + 1 < meta.len() {
                    row = row.child(
                        div()
                            .text_size(rems_from_px(11_f32))
                            .text_color(c(p::DIM))
                            .child(SharedString::from("·")),
                    );
                }
            }
            content = content.child(row);
        }

        card().p_3p5().flex_1().min_w_0().child(content).into_any_element()
    }

    /// `columns` `{cols:[…]}`: the kanban board. A horizontal band of fixed
    /// 260px columns that scrolls sideways; each column is a header (colour
    /// dot, title, count) over a vertically scrolling stack of cards. A whole
    /// card runs its `do` through the page's action path.
    pub(crate) fn render_columns(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let cols = arr(s, "cols");
        let mono_font = p::mono_font(cx);
        let band = h_flex()
            .gap_3()
            .items_start()
            .children(cols.iter().enumerate().map(|(ci, column)| {
                let title = str_of(column, "title");
                let colour = named(&str_of(column, "c"));
                let count = column.get("count").and_then(Value::as_u64).unwrap_or(0);
                let cards = arr(column, "cards");

                let header = h_flex()
                    .flex_none()
                    .gap_1p5()
                    .items_center()
                    .child(Indicator::dot().color(Color::Custom(colour)))
                    .child(
                        div()
                            .flex_1()
                            .min_w(px(0.))
                            .truncate()
                            .text_size(rems_from_px(13_f32))
                            .font_weight(gpui::FontWeight::SEMIBOLD)
                            .text_color(c(p::TEXT))
                            .child(SharedString::from(title)),
                    )
                    .child(
                        div()
                            .flex_none()
                            .text_size(rems_from_px(11_f32))
                            .text_color(c(p::SUB))
                            .child(SharedString::from(count.to_string())),
                    );

                let body = v_flex()
                    .id(SharedString::from(format!("s{index}-col{ci}-body")))
                    .max_h(rems_from_px(520_f32))
                    .gap_2()
                    .overflow_y_scroll()
                    .when(cards.is_empty(), |this| {
                        this.child(
                            div()
                                .text_size(rems_from_px(11.5_f32))
                                .text_color(c(p::DIM))
                                .child(SharedString::from("Nothing here.")),
                        )
                    })
                    .children(cards.iter().enumerate().map(|(ki, card_data)| {
                        let card_title = str_of(card_data, "title");
                        let sub = str_of(card_data, "sub");
                        let state = str_of(card_data, "state");
                        let mark = str_of(card_data, "mark");
                        let meta = arr(card_data, "meta");
                        let (state_glyph, mark_colour) = p::state_mark(&state);
                        let glyph = if mark.is_empty() { state_glyph.to_string() } else { mark.clone() };

                        let top = h_flex()
                            .gap_2()
                            .items_start()
                            .child(
                                div()
                                    .flex_none()
                                    .text_size(rems_from_px(13_f32))
                                    .text_color(c(mark_colour))
                                    .child(SharedString::from(glyph)),
                            )
                            .child(
                                div()
                                    .flex_1()
                                    .min_w(px(0.))
                                    .line_clamp(2)
                                    .text_size(rems_from_px(12.5_f32))
                                    .text_color(c(p::TEXT))
                                    .child(SharedString::from(card_title)),
                            );

                        let mut card_el = card()
                            .id(SharedString::from(format!("s{index}-col{ci}-card{ki}")))
                            .flex_none()
                            .gap_1p5()
                            .child(top)
                            .when(!sub.is_empty(), |this| {
                                this.child(
                                    div()
                                        .truncate()
                                        .text_size(rems_from_px(11_f32))
                                        .text_color(c(p::SUB))
                                        .child(SharedString::from(sub)),
                                )
                            })
                            .when(!meta.is_empty(), |this| {
                                this.child(h_flex().flex_wrap().gap_2().items_center().children(
                                    meta.iter().map(|item| {
                                        let mut el = div()
                                            .flex_none()
                                            .text_size(rems_from_px(10.5_f32))
                                            .text_color(named(&str_of(item, "c")))
                                            .child(SharedString::from(str_of(item, "t")));
                                        if item.get("mono").and_then(Value::as_bool).unwrap_or(false) {
                                            el = el.font(mono_font.clone());
                                        }
                                        el
                                    }),
                                ))
                            });
                        if let Some(handler) = self.action_handler(card_data.get("do"), cx) {
                            card_el = card_el
                                .cursor_pointer()
                                .hover(|this| this.border_color(c(p::BORDER2)))
                                .on_click(handler);
                        }
                        card_el
                    }));

                v_flex().flex_none().w(rems_from_px(260_f32)).gap_2().child(header).child(body)
            }));

        card()
            .flex_1()
            .min_w_0()
            .child(
                div()
                    .id(SharedString::from(format!("s{index}-columns-scroll")))
                    .w_full()
                    .overflow_x_scroll()
                    .child(band),
            )
            .into_any_element()
    }

    /// `composer` `{placeholder, cli}`: a single-line editor and a `Send`
    /// button. Enter (or `Send`) appends the text to `cli` as its last
    /// argument, runs it through the page's action path, and clears the
    /// editor. The button is dimmed while the editor is empty. The editor is
    /// created per section by `MiniOrkPage::ensure_composers`.
    pub(crate) fn render_composer(&self, index: usize, s: &Value, window: &mut Window, cx: &mut Context<Self>) -> AnyElement {
        let Some(composer) = self.composers.get(&index) else {
            return div().into_any_element();
        };
        let title = str_of(s, "title");
        let cli: Vec<String> =
            arr(s, "cli").iter().map(|a| a.as_str().unwrap_or_default().to_string()).collect();
        let editor = composer.editor.clone();
        let empty = editor.text(cx).trim().is_empty();

        // A single send path shared by Enter and the button.
        let send: Arc<dyn Fn(&mut Window, &mut App)> = {
            let editor = editor.clone();
            let workspace = self.workspace.clone();
            let page = cx.entity().downgrade();
            Arc::new(move |window: &mut Window, cx: &mut App| {
                let text = editor.text(cx).trim().to_string();
                if text.is_empty() {
                    return;
                }
                editor.clear(window, cx);
                let mut argv = cli.clone();
                argv.push(text);
                let action = serde_json::json!({ "cli": argv });
                let Some(workspace) = workspace.upgrade() else {
                    return;
                };
                let page = page.clone();
                window.defer(cx, move |window, cx| {
                    store::perform(&action, &workspace, Origin::Page(page), window, cx);
                });
            })
        };

        let mut content = v_flex().gap_2();
        if !title.is_empty() {
            content = content.child(self.texts.text(
                format!("s{index}-composer-title"),
                &title,
                Look::new(13., c(p::TEXT)).bold(true),
                cx,
            ));
        }

        let enter = send.clone();
        let field = h_flex()
            .flex_1()
            .min_w(px(0.))
            .px_2p5()
            .py_1p5()
            .border_1()
            .border_color(c(p::BORDER2))
            .bg(c(p::BG))
            .capture_key_down(cx.listener(move |_this, event: &KeyDownEvent, window, cx| {
                if event.keystroke.key == "enter" {
                    enter(window, cx);
                    cx.stop_propagation();
                }
            }))
            .child(div().flex_1().min_w(px(0.)).text_size(rems_from_px(12.5_f32)).child(editor.render(window, cx)));

        let on_send: Option<Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static>> = if empty {
            None
        } else {
            let send = send.clone();
            Some(Box::new(move |_event: &ClickEvent, window: &mut Window, cx: &mut App| send(window, cx)))
        };
        let send_button = p::button(
            SharedString::from(format!("s{index}-composer-send")),
            "Send",
            "primary",
            26.,
            12.,
            on_send,
        );

        content = content.child(h_flex().gap_2().items_center().child(field).child(send_button));

        card().flex_1().min_w_0().child(content).into_any_element()
    }

    /// `story` `{steps:[…]}`: a vertical list of step rows. The section draws
    /// its own rows full-width without a card header (`render_section` early-
    /// returns here). Each row is a 32px header; the title text is clickable
    /// for the step's `do`, the rest of the header toggles expansion.
    pub(crate) fn render_story(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let steps = arr(s, "steps");
        let mut content = v_flex().gap_1p5();
        for (i, step) in steps.iter().enumerate() {
            let step_id = str_of(step, "id");
            let step_key = format!("story-{step_id}");
            let is_open = self.expanded.contains(&step_key);
            let state = str_of(step, "state");
            let title = str_of(step, "title");
            let lane = str_of(step, "lane");
            let dur = str_of(step, "dur");
            let cost = str_of(step, "cost");
            let headline_obj = step.get("headline").cloned().unwrap_or(Value::Null);
            let headline_t = str_of(&headline_obj, "t");
            let headline_c = str_of(&headline_obj, "c");

            // ── header row ────────────────────────────────────────────────
            let do_handler = self.action_handler(step.get("do"), cx);
            let step_id_for_toggle = step_id.clone();

            // The title sub-region: runs the step's `do` and stops the click
            // from also toggling the row.
            let title_el = {
                let mut el = div()
                    .id(SharedString::from(format!("{step_key}-title")))
                    .min_w(px(0.))
                    .flex_1()
                    .truncate()
                    .text_size(rems_from_px(13_f32))
                    .font_weight(gpui::FontWeight::SEMIBOLD)
                    .text_color(c(p::TEXT))
                    .child(SharedString::from(title.clone()));
                if let Some(handler) = do_handler {
                    let wrapped: Box<dyn Fn(&ClickEvent, &mut Window, &mut App) + 'static> =
                        Box::new(move |event, window, app| {
                            app.stop_propagation();
                            handler(event, window, app);
                        });
                    el = el.cursor_pointer().on_click(wrapped);
                }
                el
            };

            let mut header = h_flex()
                .id(SharedString::from(format!("{step_key}-header")))
                .h_8()
                .gap_2()
                .items_center()
                .px_2p5()
                .border_1()
                .border_color(c(p::BORDER))
                .bg(c(p::CARD))
                .when(!is_open, |this| this.cursor_pointer())
                .child(status_mark(
                    &state,
                    13.,
                    &format!("{step_key}-mark"),
                ))
                .child(p::lane_mark(&lane, 13.))
                .child(title_el)
                .when(!headline_t.is_empty(), |this| {
                    this.child(
                        div()
                            .min_w(px(0.))
                            .max_w(rems_from_px(220_f32))
                            .truncate()
                            .text_size(rems_from_px(12_f32))
                            .text_color(p::named(&headline_c))
                            .child(SharedString::from(headline_t)),
                    )
                })
                .child(div().flex_1())
                .when(!dur.is_empty() || !cost.is_empty(), |this| {
                    let mono = p::mono_font(cx);
                    let mut meta = h_flex().flex_none().gap_2().items_baseline();
                    if !dur.is_empty() {
                        meta = meta.child(
                            div()
                                .text_size(rems_from_px(11_f32))
                                .text_color(c(p::SUB))
                                .font(mono.clone())
                                .child(SharedString::from(dur.clone())),
                        );
                    }
                    if !cost.is_empty() {
                        meta = meta.child(
                            div()
                                .text_size(rems_from_px(11_f32))
                                .text_color(c(p::SUB))
                                .font(mono.clone())
                                .child(SharedString::from(cost.clone())),
                        );
                    }
                    this.child(meta)
                })
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(13_f32))
                        .text_color(c(p::MUTED))
                        .child(SharedString::from(if is_open { "⌄" } else { "›" })),
                );
            // The header always toggles expansion on click; the title
            // sub-region above stops propagation when it has its own `do`.
            header = header.on_click(cx.listener(move |this, _, _, cx| {
                this.toggle_step_expanded("story", &step_id_for_toggle, cx);
            }));
            // A story has no card header of its own, so the section's floated
            // copy button lands on the first row: pad the row clear of it.
            if i == 0 {
                header = header.pr(rems_from_px(24_f32));
            }

            let mut row_el = v_flex().gap_1p5().child(header);

            if is_open {
                let body = self.render_step_body(index, i, step, cx);
                row_el = row_el.child(
                    div()
                        .pl_5()
                        .pt_0p5()
                        .child(body),
                );
            }

            content = content.child(row_el);
        }
        content.into_any_element()
    }

    /// The expanded body of a story step: each block by `kind`.
    fn render_step_body(&self, section_index: usize, step_index: usize, step: &Value, cx: &mut Context<Self>) -> AnyElement {
        let key_prefix = format!("s{section_index}-step{step_index}");
        let blocks = arr(step, "body");
        let mut col = v_flex().gap_2();
        for (bi, block) in blocks.iter().enumerate() {
            let key = format!("{key_prefix}-b{bi}");
            col = col.child(self.render_block_body(&key, block, cx));
        }
        col.into_any_element()
    }

    /// A single block by `kind`. The `md`, `lines`, `files`, `findings`,
    /// `checks` block kinds are also section types (files / findings / checks
    /// render with a card around this body in their section renderers); the
    /// same code path here is reused inside `story` step bodies.
    fn render_block_body(&self, key: &str, block: &Value, cx: &mut Context<Self>) -> AnyElement {
        let kind = str_of(block, "kind");
        match kind.as_str() {
            "md" => self.texts.markdown(
                format!("{key}-md"),
                str_of(block, "text"),
                Look::new(12.5, c(p::BODY)),
                cx,
            ),
            "lines" => {
                let lines = arr(block, "lines");
                code_box(&self.texts, key, lines, cx).into_any_element()
            }
            "files" => self.render_files_body_inner(key, block, cx),
            "findings" => self.render_findings_body_inner(key, block, cx),
            "checks" => self.render_checks_body_inner(key, block, cx),
            _ => self.texts.text(
                format!("{key}-unk"),
                &format!("This block ({kind}) needs a newer mini-ork IDE."),
                Look::new(12., c(p::MUTED)),
                cx,
            ),
        }
    }

    /// `files` body: one diff card per file. Used both by the `files` section
    /// (render_section routes through here) and by a `files` block in a story
    /// step. Reuses `diff_sections` and `section_for` from `components.rs`.
    fn render_files_body_inner(&self, key: &str, block: &Value, cx: &mut Context<Self>) -> AnyElement {
        let files = arr(block, "files");
        let diff = str_of(block, "diff");
        let commits = arr(block, "commits");
        let sections = diff_sections(&diff);
        let mono = p::mono_font(cx);

        let mut col = v_flex().gap_1();
        if files.is_empty() && commits.is_empty() && diff.is_empty() {
            let note = str_of(block, "diff_note");
            let text = if note.is_empty() { "No files changed.".to_string() } else { note };
            return col.child(self.texts.text(format!("{key}-empty"), &text, Look::new(12., c(p::MUTED)), cx)).into_any_element();
        }
        for (i, f) in files.iter().enumerate() {
            let path = str_of(f, "path");
            let abs = str_of(f, "abs");
            let status = str_of(f, "status");
            let added = f.get("added").and_then(Value::as_u64).unwrap_or(0);
            let removed = f.get("removed").and_then(Value::as_u64).unwrap_or(0);
            let open_key = format!("{key}:{path}");
            let is_open = self.expanded.contains(&open_key);

            let (verb, verb_colour) = match status.as_str() {
                "A" => ("A", p::c(p::GIT_ADD)),
                "D" => ("D", p::c(p::GIT_DEL)),
                _ => ("M", p::c(p::GIT_MOD)),
            };

            let row = h_flex()
                .id(SharedString::from(format!("{key}-file-{i}")))
                .items_center()
                .gap_2()
                .px_2()
                .py(rems_from_px(5_f32))
                .border_1()
                .border_color(c(p::BORDER2))
                .bg(c(p::CHROME))
                .cursor_pointer()
                .on_click(cx.listener({
                    let open_key = open_key.clone();
                    move |this, _, _, cx| {
                        let open_key = open_key.clone();
                        if !this.expanded.remove(&open_key) {
                            this.expanded.insert(open_key);
                        }
                        cx.notify();
                    }
                }))
                .child(
                    div()
                        .flex_none()
                        .w_3p5()
                        .text_size(rems_from_px(11.5_f32))
                        .font_weight(gpui::FontWeight::SEMIBOLD)
                        .text_color(verb_colour)
                        .child(SharedString::from(verb)),
                )
                .child(
                    div()
                        .flex_1()
                        .min_w(px(0.))
                        .truncate()
                        .text_size(rems_from_px(12_f32))
                        .font(mono.clone())
                        .text_color(c(p::TEXT))
                        .child(SharedString::from(path.clone())),
                )
                .when(added > 0 || removed > 0, |this| {
                    this.child(
                        DiffStat::new(
                            SharedString::from(format!("{key}-file-diff-{i}")),
                            added as usize,
                            removed as usize,
                        )
                        .label_size(LabelSize::XSmall),
                    )
                })
                .when(!abs.is_empty(), |this| {
                    let opener_value = serde_json::json!({ "path": abs.clone() });
                    let h = self.action_handler(Some(&opener_value), cx);
                    this.child(p::button(
                        SharedString::from(format!("{key}-open-{i}")),
                        "Open",
                        "ghost",
                        22.,
                        11.5,
                        match h {
                            Some(handler) => {
                                let wrapped: Box<dyn Fn(&ClickEvent, &mut Window, &mut gpui::App) + 'static> =
                                    Box::new(move |event, window, app| {
                                        app.stop_propagation();
                                        handler(event, window, app);
                                    });
                                Some(wrapped)
                            }
                            None => None,
                        },
                    ))
                })
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(13_f32))
                        .text_color(c(p::MUTED))
                        .child(SharedString::from(if is_open { "⌄" } else { "›" })),
                );

            col = col.child(row);

            if is_open {
                let slice = section_for(&sections, &path);
                let body_text = slice.unwrap_or_default();
                let lines = if body_text.is_empty() {
                    diff_lines(&diff, 400)
                } else {
                    diff_lines(&body_text, 400)
                };
                let lines_key = format!("{key}-diff-{i}");
                col = col.child(
                    div()
                        .pl_4()
                        .child(code_box(&self.texts, &lines_key, lines, cx)),
                );
            }
        }

        if !commits.is_empty() {
            col = col.child(
                v_flex()
                    .gap_0p5()
                    .mt(px(4.))
                    .children(commits.iter().enumerate().map(|(ci, cm)| {
                        let sha = str_of(cm, "sha");
                        let short: String = sha.chars().take(8).collect();
                        h_flex()
                            .id(SharedString::from(format!("{key}-commit-{ci}")))
                            .gap_2()
                            .px_1p5()
                            .py(rems_from_px(3_f32))
                            .child(
                                div()
                                    .flex_none()
                                    .text_size(rems_from_px(12_f32))
                                    .font(mono.clone())
                                    .text_color(c(p::YELLOW))
                                    .child(SharedString::from(short)),
                            )
                            .child(
                                div()
                                    .flex_1()
                                    .min_w(px(0.))
                                    .truncate()
                                    .text_size(rems_from_px(12.5_f32))
                                    .text_color(c(p::TEXT))
                                    .child(SharedString::from(str_of(cm, "subject"))),
                            )
                    })),
            );
        }

        col.into_any_element()
    }

    /// `findings` body: verdict pill, then rows per item with severity badge,
    /// file:line, issue, snippet; then `•` reasons. Used by the section and by
    /// a `findings` block in a story step.
    fn render_findings_body_inner(&self, key: &str, block: &Value, cx: &mut Context<Self>) -> AnyElement {
        let verdict_obj = block.get("verdict").cloned().unwrap_or(Value::Null);
        let items = arr(block, "items");
        let reasons = arr(block, "reasons");
        let mono = p::mono_font(cx);

        let mut col = v_flex().gap_2();

        if !str_of(&verdict_obj, "t").is_empty() {
            let t = str_of(&verdict_obj, "t");
            let color = p::named(&str_of(&verdict_obj, "c"));
            let tint = color.opacity(0.15);
            col = col.child(
                h_flex()
                    .flex_none()
                    .h_5()
                    .px(rems_from_px(9_f32))
                    .items_center()
                    .rounded_full()
                    .bg(tint)
                    .text_size(rems_from_px(p::PILL))
                    .text_color(color)
                    .child(SharedString::from(t.to_uppercase())),
            );
        }

        for (i, item) in items.iter().enumerate() {
            let severity = str_of(item, "severity");
            let sev_lower = severity.to_ascii_lowercase();
            let sev_color = match sev_lower.as_str() {
                "high" | "critical" | "blocker" => p::c(p::RED),
                "medium" => p::c(p::YELLOW),
                _ => p::c(p::SUB),
            };
            let line = item.get("line").and_then(|v| match v {
                Value::Number(n) => n.as_u64().map(|n| n.to_string()),
                Value::String(s) => Some(s.clone()),
                _ => None,
            });
            let file = str_of(item, "file");
            let file_line = if !file.is_empty() {
                match &line {
                    Some(l) => format!("{file}:{l}"),
                    None => file.clone(),
                }
            } else {
                String::new()
            };
            let abs = str_of(item, "abs");
            let issue = str_of(item, "issue");
            let snippet = str_of(item, "snippet");

            let file_line_el = if !file_line.is_empty() {
                let open_handler: Option<Box<dyn Fn(&ClickEvent, &mut Window, &mut gpui::App) + 'static>> =
                    if !abs.is_empty() {
                        self.action_handler(Some(&serde_json::json!({ "path": abs.clone() })), cx)
                    } else {
                        None
                    };
                let mut el = div()
                    .id(SharedString::from(format!("{key}-f{i}-loc")))
                    .text_size(rems_from_px(11.5_f32))
                    .font(mono.clone())
                    .text_color(c(p::BLUE))
                    .truncate()
                    .child(SharedString::from(file_line.clone()));
                if let Some(handler) = open_handler {
                    el = el.cursor_pointer().on_click(handler);
                }
                el
            } else {
                div().id(SharedString::from(format!("{key}-f{i}-loc")))
            };

            col = col.child(
                v_flex()
                    .id(SharedString::from(format!("{key}-find-{i}")))
                    .gap_0p5()
                    .px_2()
                    .py(rems_from_px(5_f32))
                    .border_1()
                    .border_color(c(p::BORDER2))
                    .bg(c(p::CHROME))
                    .child(
                        h_flex()
                            .gap_2()
                            .items_center()
                            .when(!severity.is_empty(), |this| {
                                this.child(
                                    h_flex()
                                        .flex_none()
                                        .h_4()
                                        .px_1p5()
                                        .items_center()
                                        .bg(sev_color.opacity(0.18))
                                        .text_size(rems_from_px(p::BADGE))
                                        .text_color(sev_color)
                                        .child(SharedString::from(severity.to_uppercase())),
                                )
                            })
                            .child(file_line_el)
                            .child(div().flex_1()),
                    )
                    .child(self.texts.text(
                        format!("{key}-find-{i}-issue"),
                        &issue,
                        Look::new(12.5, c(p::TEXT)),
                        cx,
                    ))
                    .when(!snippet.is_empty(), |this| {
                        this.child(
                            div()
                                .text_size(rems_from_px(11_f32))
                                .font(mono.clone())
                                .text_color(c(p::DIM))
                                .truncate()
                                .child(SharedString::from(snippet)),
                        )
                    }),
            );
        }

        if !reasons.is_empty() {
            col = col.child(
                v_flex()
                    .gap_0p5()
                    .children(reasons.iter().enumerate().map(|(ri, r)| {
                        let text = r.as_str().unwrap_or_default().to_string();
                        h_flex()
                            .id(SharedString::from(format!("{key}-reason-{ri}")))
                            .gap_1p5()
                            .items_start()
                            .child(
                                div()
                                    .flex_none()
                                    .text_size(rems_from_px(12.5_f32))
                                    .text_color(c(p::MUTED))
                                    .child(SharedString::from("•")),
                            )
                            .child(
                                div()
                                    .flex_1()
                                    .min_w(px(0.))
                                    .text_size(rems_from_px(12_f32))
                                    .text_color(c(p::BODY))
                                    .child(SharedString::from(text)),
                            )
                    })),
            );
        }

        if items.is_empty() && reasons.is_empty() && str_of(&verdict_obj, "t").is_empty() {
            col = col.child(self.texts.text(format!("{key}-empty"), "No findings.", Look::new(12., c(p::MUTED)), cx));
        }

        col.into_any_element()
    }

    /// `checks` body: a summary row (`✓ n passing  ✗ n failing  ● n pending`),
    /// then rows with a status glyph, name and detail. A failing row expands to
    /// its `log` lines (mono). Reused by the `checks` section and a `checks`
    /// block inside a story step.
    fn render_checks_body_inner(&self, key: &str, block: &Value, cx: &mut Context<Self>) -> AnyElement {
        let summary_obj = block.get("summary").cloned().unwrap_or(Value::Null);
        let rows = arr(block, "rows");

        let mut col = v_flex().gap_1p5();

        let passing = summary_obj.get("passing").and_then(Value::as_u64).unwrap_or(0);
        let failing = summary_obj.get("failing").and_then(Value::as_u64).unwrap_or(0);
        let pending = summary_obj.get("pending").and_then(Value::as_u64).unwrap_or(0);
        col = col.child(
            h_flex()
                .gap_3p5()
                .items_center()
                .px_2()
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(12_f32))
                        .text_color(c(p::GREEN))
                        .child(SharedString::from(format!("✓ {passing} passing"))),
                )
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(12_f32))
                        .text_color(c(p::RED))
                        .child(SharedString::from(format!("✗ {failing} failing"))),
                )
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(12_f32))
                        .text_color(c(p::YELLOW))
                        .child(SharedString::from(format!("● {pending} pending"))),
                ),
        );

        for (i, row) in rows.iter().enumerate() {
            let state = str_of(row, "state");
            let state_lc = state.to_ascii_lowercase();
            let name = str_of(row, "name");
            let detail = str_of(row, "detail");
            let log = arr(row, "log");
            let is_failing = matches!(state_lc.as_str(), "fail" | "failed" | "error");
            let open_key = format!("{key}-check-{i}");
            let is_open = self.expanded.contains(&open_key);

            let (glyph, glyph_color) = match state_lc.as_str() {
                "pass" | "done" | "ok" => ("✓", p::c(p::GREEN)),
                "fail" | "failed" | "error" => ("✗", p::c(p::RED)),
                "pending" | "running" => ("●", p::c(p::YELLOW)),
                _ => ("○", p::c(p::SUB)),
            };

            let r = h_flex()
                .id(SharedString::from(format!("{key}-check-{i}")))
                .items_center()
                .gap_2()
                .px_2()
                .py(rems_from_px(5_f32))
                .border_1()
                .border_color(c(p::BORDER2))
                .bg(c(p::CHROME))
                .when(is_failing && !log.is_empty(), |this| {
                    this.cursor_pointer().on_click(cx.listener({
                        let open_key = open_key.clone();
                        move |this, _, _, cx| {
                            let open_key = open_key.clone();
                            if !this.expanded.remove(&open_key) {
                                this.expanded.insert(open_key);
                            }
                            cx.notify();
                        }
                    }))
                })
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(12.5_f32))
                        .text_color(glyph_color)
                        .child(SharedString::from(glyph)),
                )
                .child(
                    div()
                        .flex_1()
                        .min_w(px(0.))
                        .truncate()
                        .text_size(rems_from_px(12_f32))
                        .text_color(c(p::TEXT))
                        .child(SharedString::from(name.clone())),
                )
                .child(
                    div()
                        .flex_none()
                        .text_size(rems_from_px(11.5_f32))
                        .text_color(c(p::SUB))
                        .truncate()
                        .child(SharedString::from(detail)),
                );
            col = col.child(r);

            if is_open && is_failing {
                let lines_key = format!("{key}-check-{i}-log");
                col = col.child(
                    div()
                        .pl_5()
                        .child(code_box(&self.texts, &lines_key, log, cx)),
                );
            }
        }

        if rows.is_empty() {
            col = col.child(self.texts.text(format!("{key}-empty"), "No checks recorded.", Look::new(12., c(p::MUTED)), cx));
        }

        col.into_any_element()
    }

    /// `files` section: the section's normal card with title and a body made
    /// from the same code path as a `files` block.
    pub(crate) fn render_files(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        self.render_files_body_inner(&format!("s{index}-files"), s, cx)
    }

    /// `findings` section: the section's normal card with title and a body made
    /// from the same code path as a `findings` block.
    pub(crate) fn render_findings(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        self.render_findings_body_inner(&format!("s{index}-findings"), s, cx)
    }

    /// `checks` section: the section's normal card with title and a body made
    /// from the same code path as a `checks` block.
    pub(crate) fn render_checks(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        self.render_checks_body_inner(&format!("s{index}-checks"), s, cx)
    }

    /// `agents` section: rows of agent data without the normal card header.
    /// Orca's three-line rows: status + lane + id + model + cost + dur on line
    /// 1; step on line 2; last on line 3. Each row is clickable via its `do`.
    pub(crate) fn render_agents(&self, index: usize, s: &Value, cx: &mut Context<Self>) -> AnyElement {
        let rows = arr(s, "rows");
        let mono = p::mono_font(cx);
        let mut col = v_flex().gap(rems_from_px(0_f32));
        for (i, row) in rows.iter().enumerate() {
            let id = str_of(row, "id");
            let state = str_of(row, "state");
            let lane = str_of(row, "lane");
            let model = str_of(row, "model");
            let step = str_of(row, "step");
            let last = str_of(row, "last");
            let cost = str_of(row, "cost");
            let dur = str_of(row, "dur");
            let do_handler = self.action_handler(row.get("do"), cx);

            let card_el = h_flex()
                .id(SharedString::from(format!("s{index}-agent-{i}")))
                .items_center()
                .gap_2()
                .px_2()
                .py(rems_from_px(7_f32))
                .border_b_1()
                .border_color(c(p::ROW_RULE))
                .child(status_mark(&state, 13., &format!("s{index}-agent-{i}-mark")))
                .child(p::lane_mark(&lane, 13.))
                .child(
                    div()
                        .flex_1()
                        .min_w(px(0.))
                        .truncate()
                        .text_size(rems_from_px(12.5_f32))
                        .font_weight(gpui::FontWeight::SEMIBOLD)
                        .text_color(c(p::TEXT))
                        .child(SharedString::from(id.clone())),
                )
                .when(!model.is_empty(), |this| {
                    this.child(
                        div()
                            .flex_none()
                            .text_size(rems_from_px(10.5_f32))
                            .font(mono.clone())
                            .text_color(c(p::SUB))
                            .child(SharedString::from(model.clone())),
                    )
                })
                .child(div().flex_1())
                .when(!dur.is_empty() || !cost.is_empty(), |this| {
                    let mut meta = h_flex().flex_none().gap_2().items_baseline();
                    if !dur.is_empty() {
                        meta = meta.child(
                            div()
                                .text_size(rems_from_px(11_f32))
                                .text_color(c(p::SUB))
                                .font(mono.clone())
                                .child(SharedString::from(dur.clone())),
                        );
                    }
                    if !cost.is_empty() {
                        meta = meta.child(
                            div()
                                .text_size(rems_from_px(11_f32))
                                .text_color(c(p::SUB))
                                .font(mono.clone())
                                .child(SharedString::from(cost.clone())),
                        );
                    }
                    this.child(meta)
                });

            let mut stack = v_flex()
                .id(SharedString::from(format!("s{index}-agent-{i}-stack")))
                .gap_0p5()
                .py(rems_from_px(7_f32))
                .px_2()
                .when(i < rows.len().saturating_sub(1), |this| {
                    this.border_b_1().border_color(c(p::ROW_RULE))
                })
                .child(card_el)
                .when(!step.is_empty(), |this| {
                    this.child(
                        div()
                            .pl(rems_from_px(22_f32))
                            .text_size(rems_from_px(11.5_f32))
                            .text_color(c(p::BODY))
                            .truncate()
                            .child(SharedString::from(step.clone())),
                    )
                })
                .when(!last.is_empty(), |this| {
                    this.child(
                        div()
                            .pl(rems_from_px(22_f32))
                            .text_size(rems_from_px(11_f32))
                            .font(mono.clone())
                            .text_color(c(p::DIM))
                            .truncate()
                            .child(SharedString::from(last.clone())),
                    )
                });

            // Apply the click handler on the whole stack (line 1 + step +
            // last), so the entire row acts as the trigger.
            if let Some(handler) = do_handler {
                stack = stack.cursor_pointer().on_click(handler);
            }
            col = col.child(stack);
        }
        if rows.is_empty() {
            col = col.child(self.texts.text(format!("s{index}-agents-empty"), "No agents recorded.", Look::new(12., c(p::MUTED)), cx));
        }
        col.into_any_element()
    }
}
