//! Look and feel: the palette, spacing and the few building blocks every page
//! uses (cards, status pills, page titles, sidebar buttons).

use eframe::egui::{self, Color32, CornerRadius, Margin, RichText, Stroke};

// Deep purple with an orange accent.
pub const BG: Color32 = Color32::from_rgb(0x12, 0x0b, 0x1b);
pub const SIDEBAR: Color32 = Color32::from_rgb(0x17, 0x0e, 0x22);
pub const PANEL: Color32 = Color32::from_rgb(0x1c, 0x12, 0x2a);
pub const CARD: Color32 = Color32::from_rgb(0x24, 0x18, 0x36);
pub const WIDGET: Color32 = Color32::from_rgb(0x32, 0x21, 0x4a);
pub const HOVER: Color32 = Color32::from_rgb(0x46, 0x2d, 0x66);
pub const STRIPE: Color32 = Color32::from_rgb(0x29, 0x1b, 0x3c);
pub const BORDER: Color32 = Color32::from_rgb(0x38, 0x26, 0x52);
pub const TEXT: Color32 = Color32::from_rgb(0xec, 0xe4, 0xf7);
pub const MUTED: Color32 = Color32::from_rgb(0x9d, 0x8f, 0xb5);
pub const ORANGE: Color32 = Color32::from_rgb(0xff, 0x8a, 0x1f);
pub const ORANGE_DIM: Color32 = Color32::from_rgb(0xb8, 0x5c, 0x10);
pub const GOOD: Color32 = Color32::from_rgb(0x5a, 0xd0, 0x7a);
pub const BAD: Color32 = Color32::from_rgb(0xec, 0x68, 0x5e);
pub const WARN: Color32 = Color32::from_rgb(0xf0, 0xb4, 0x40);

const R: u8 = 6;

pub fn apply(ctx: &egui::Context) {
    let mut v = egui::Visuals::dark();
    v.panel_fill = PANEL;
    v.window_fill = CARD;
    v.window_stroke = Stroke::new(1.0, BORDER);
    v.window_corner_radius = CornerRadius::same(10);
    v.extreme_bg_color = BG;
    v.faint_bg_color = STRIPE;
    v.hyperlink_color = ORANGE;
    v.selection.bg_fill = ORANGE_DIM;
    v.selection.stroke = Stroke::new(1.0, ORANGE);
    v.override_text_color = Some(TEXT);
    let w = &mut v.widgets;
    w.noninteractive.bg_fill = PANEL;
    w.noninteractive.weak_bg_fill = PANEL;
    w.noninteractive.bg_stroke = Stroke::new(1.0, BORDER);
    w.inactive.bg_fill = WIDGET;
    w.inactive.weak_bg_fill = WIDGET;
    w.hovered.bg_fill = HOVER;
    w.hovered.weak_bg_fill = HOVER;
    w.hovered.bg_stroke = Stroke::new(1.0, ORANGE);
    w.active.bg_fill = ORANGE_DIM;
    w.active.weak_bg_fill = ORANGE_DIM;
    w.active.bg_stroke = Stroke::new(1.0, ORANGE);
    w.open.bg_fill = HOVER;
    w.open.weak_bg_fill = HOVER;
    for s in [&mut w.noninteractive, &mut w.inactive, &mut w.hovered, &mut w.active, &mut w.open] {
        s.corner_radius = CornerRadius::same(R);
    }
    ctx.set_visuals(v);
    ctx.all_styles_mut(|s| {
        s.spacing.item_spacing = egui::vec2(8.0, 6.0);
        s.spacing.button_padding = egui::vec2(10.0, 4.0);
        s.spacing.interact_size.y = 24.0;
        use egui::{FontFamily, FontId, TextStyle};
        s.text_styles.insert(TextStyle::Heading, FontId::new(22.0, FontFamily::Proportional));
        s.text_styles.insert(TextStyle::Body, FontId::new(14.0, FontFamily::Proportional));
        s.text_styles.insert(TextStyle::Button, FontId::new(14.0, FontFamily::Proportional));
        s.text_styles.insert(TextStyle::Small, FontId::new(11.5, FontFamily::Proportional));
    });
}

/// A rounded panel grouping related controls.
pub fn card<R>(ui: &mut egui::Ui, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    egui::Frame::new()
        .fill(CARD)
        .stroke(Stroke::new(1.0, BORDER))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::same(14))
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui)
        })
        .inner
}

/// A card with a small title row.
pub fn titled_card<R>(ui: &mut egui::Ui, title: &str, add: impl FnOnce(&mut egui::Ui) -> R) -> R {
    card(ui, |ui| {
        ui.label(RichText::new(title).strong().size(15.0).color(ORANGE));
        ui.add_space(4.0);
        add(ui)
    })
}

/// Page title plus a one-line explanation.
pub fn page_title(ui: &mut egui::Ui, title: &str, sub: &str) {
    ui.add_space(4.0);
    ui.label(RichText::new(title).heading().strong());
    if !sub.is_empty() {
        ui.label(RichText::new(sub).color(MUTED));
    }
    ui.add_space(10.0);
}

/// A small coloured status label with a rounded background.
pub fn pill(ui: &mut egui::Ui, text: &str, color: Color32) -> egui::Response {
    egui::Frame::new()
        .fill(color.gamma_multiply(0.18))
        .stroke(Stroke::new(1.0, color.gamma_multiply(0.6)))
        .corner_radius(CornerRadius::same(10))
        .inner_margin(Margin::symmetric(8, 1))
        .show(ui, |ui| ui.label(RichText::new(text).color(color).size(12.0).strong()))
        .response
}

/// A full-width sidebar entry; an orange bar marks the open page.
pub fn nav_button(ui: &mut egui::Ui, selected: bool, icon: &str, label: &str, badge: Option<Color32>)
    -> egui::Response
{
    let w = ui.available_width();
    let (rect, resp) = ui.allocate_exact_size(egui::vec2(w, 34.0), egui::Sense::click());
    let p = ui.painter();
    let fill = if selected { CARD } else if resp.hovered() { STRIPE } else { Color32::TRANSPARENT };
    p.rect_filled(rect, CornerRadius::same(R), fill);
    if selected {
        let bar = egui::Rect::from_min_size(rect.min + egui::vec2(0.0, 7.0), egui::vec2(3.0, 20.0));
        p.rect_filled(bar, CornerRadius::same(2), ORANGE);
    }
    let col = if selected { TEXT } else { MUTED };
    p.text(rect.left_center() + egui::vec2(14.0, 0.0), egui::Align2::LEFT_CENTER, icon,
           egui::FontId::proportional(15.0), if selected { ORANGE } else { col });
    p.text(rect.left_center() + egui::vec2(40.0, 0.0), egui::Align2::LEFT_CENTER, label,
           egui::FontId::proportional(14.5), col);
    if let Some(c) = badge {
        p.circle_filled(egui::pos2(rect.right() - 14.0, rect.center().y), 4.0, c);
    }
    resp.on_hover_cursor(egui::CursorIcon::PointingHand)
}

/// A big call-to-action button (Start, Fix).
pub fn primary_button(ui: &mut egui::Ui, text: &str) -> egui::Response {
    ui.add(egui::Button::new(RichText::new(text).strong().color(Color32::BLACK))
        .fill(ORANGE)
        .corner_radius(CornerRadius::same(R)))
}
