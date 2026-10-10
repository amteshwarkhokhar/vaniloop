# vaniloop console: design reference

The look follows Apple's Human Interface Guidelines (HIG), plus a small vaniloop touch. Tokens live in
`src/vaniloop/web/app.css`. This file replaces the look and tokens in `ui_spec.md` (the old dark design).

## From the HIG

| HIG page | What we took |
|---|---|
| Buttons | Capsule buttons. One prominent (accent) button per view. A press state. An ellipsis when a button opens a confirm. |
| Color | Apple's 2025 system colours, increased-contrast values for text. Colour is never the only signal: every status has a word. |
| Typography | The system font (`system-ui`), no light weights, 15px body. |
| Sidebars | At most 2 levels. Accent-coloured icons. Nothing critical at the bottom. |
| Segmented controls | Tabs inside a page are a segmented control with at most 5 segments. |
| Progress indicators | One place for progress (the job capsule at the top), determinate when the log gives a count, and a Cancel button. |
| Alerts | A confirm names the action in its button, and Cancel comes first. |
| Materials | Translucent bars: `saturate(180%) blur(20px)`. |

Sources: `https://developer.apple.com/design/human-interface-guidelines/` (buttons, color, typography, sidebars,
segmented-controls, progress-indicators, alerts, materials), read on 10 Oct 2026.

## Our touch

- The accent is systemIndigo `#6155F5` (5.09:1 on white). Accent text uses `#4B3FD9`.
- Numbers use SF Rounded (`ui-rounded`).
- The mark is a loop ring with a gap and a voice dot.
- The live job is a capsule at the top of every page. Its log opens as a sheet below it.
