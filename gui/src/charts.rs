//! Small hand-rolled bar charts, drawn with egui's own Painter.
//!
//! A plotting crate (egui_plot) was tried and dropped: every version on
//! crates.io that matches this project's egui_plot release pulls in a
//! DIFFERENT egui version than eframe does, so its `Plot::show()` cannot
//! actually accept this app's `&mut egui::Ui` - two same-named but
//! semver-incompatible egui crates would sit side by side in the dependency
//! tree. Bars are simple enough that hand-rolling them avoids that entirely.

use eframe::egui;

/// One horizontal bar: a label, a filled bar sized to `frac` (0..1) of
/// `bar_w`, and a right-aligned value string.
pub fn bar_row(
    ui: &mut egui::Ui,
    label_w: f32,
    bar_w: f32,
    label: &str,
    frac: f32,
    color: egui::Color32,
    track: egui::Color32,
    value: &str,
) {
    ui.horizontal(|ui| {
        ui.add_sized([label_w, 18.0], egui::Label::new(egui::RichText::new(label).small()));
        let (rect, _) = ui.allocate_exact_size(egui::vec2(bar_w, 16.0), egui::Sense::hover());
        let painter = ui.painter();
        painter.rect_filled(rect, 3.0, track);
        let w = (rect.width() * frac.clamp(0.0, 1.0)).max(2.0);
        let filled = egui::Rect::from_min_size(rect.min, egui::vec2(w, rect.height()));
        painter.rect_filled(filled, 3.0, color);
        ui.add_space(6.0);
        ui.label(egui::RichText::new(value).small().strong());
    });
}

/// Win rate as a colour from red (0%) through amber (50%) to green (100%).
pub fn win_rate_color(rate: f64) -> egui::Color32 {
    let t = rate.clamp(0.0, 1.0) as f32;
    let lerp = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * t) as u8;
    if t < 0.5 {
        let u = t * 2.0;
        egui::Color32::from_rgb(
            (220.0 - (220.0 - 235.0) * u) as u8,
            lerp(90, 170),
            lerp(80, 60),
        )
    } else {
        let u = (t - 0.5) * 2.0;
        egui::Color32::from_rgb(
            (235.0 - (235.0 - 90.0) * u) as u8,
            (170.0 + (200.0 - 170.0) * u) as u8,
            lerp(60, 110),
        )
    }
}
