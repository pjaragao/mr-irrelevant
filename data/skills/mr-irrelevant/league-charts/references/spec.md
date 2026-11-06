# Chart spec (JSON) — every block type

Top level: `{"title", "subtitle", "footer", "blocks": [...]}` (1–8 blocks, stacked top to bottom). Texts are cut at
140 characters. Numbers must be finite; `null` means "missing / not played yet" where noted. `fmt` is `int` (default,
thousands with `.`), `float` (1 decimal) or `pct` (1 decimal + %). `unit` is only used in tooltips.

| block | required | optional | limits |
|---|---|---|---|
| `tiles` | `items: [{value, label, sub}]` | | 1–4 items |
| `heatmap` | `rows`, `cols`, `values` (rows × cols, `null` = future) | `title`, `subtitle`, `fmt`, `unit`, `row_totals`, `row_notes {row: text}`, `note` | ≤ 30 × 30 |
| `lines` | `x`, `series: [{name, values (null stops the line), highlight}]` | `title`, `subtitle`, `fmt`, `unit` | ≤ 40 x, ≤ 8 series; values length = x length |
| `columns` | `x`, `series: [{name, values}]` | `stacked`, `fmt`, `unit`, `title`, `subtitle` | ≤ 20 groups, ≤ 6 series |
| `bars` | `items: [{label, value, note, muted}]` | `orientation` `h` (ranked) or `v` (columns), `fmt`, `unit` | ≤ 30 items; `v` best ≤ 8 |
| `donut` | `items: [{label, value}]` | `fmt`, `unit`, `center_label` | > 8 slices fold into "Outros" |
| `scatter` | `points: [{label, x, y}]` | `x_label`, `y_label`, `fmt`, `mean_lines` (default true), `quadrants {tl,tr,bl,br: caption}` | ≤ 40 points |

Colour is assigned for you (fixed validated order); you cannot pass colours. `highlight: true` thickens a line.
The in-progress period should be `null` after its last real value (lines) or a `muted: true` item with a `note`
(bars) — never a fake zero.

Examples (render with `python3 chart_service.py spec <file>`): `spec-example.json` (tiles + heatmap + lines + bars),
`scatter-example.json`, `donut-columns-example.json`.
