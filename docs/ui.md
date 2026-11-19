# Prized style guide (colors, fonts, icons, look)

Use this to make the board page look like Prized (prized.dev and prized.dev/dashboard). Layout
comes from the separate reference image. Build with **shadcn/ui components**, themed with the
values below in place of shadcn's default theme.

## Look

- Quiet, grey and dense. White cards on a light grey page (near-black in dark mode).
- Hairline 1px borders and barely-there shadows instead of heavy elevation.
- One accent color (blue), used for links, focus and selection. Everything else is grayscale.
  Green, yellow and red appear only to mean ok, warning and error.
- Small radii: 8px on buttons, chips and inner blocks; 12px on cards and panels.
- Monospace for anything machine-made (IDs, logs, commands, code).
- Sentence case, short plain copy ("Running", "Done", "Needs you"). No emoji, no gradients, no illustrations.
- Numbers use tabular figures.
- Light and dark follow the OS setting.

## Colors

Map these onto shadcn's variables (`--background` = window, `--card`, `--foreground`,
`--muted-foreground`, `--border`, `--primary`, `--ring` = primary, `--destructive` = bad).

| Token | Light | Dark | Use |
|---|---|---|---|
| window | `#efeff0` | `#09090a` | page background |
| bg | `#ffffff` | `#121213` | header, sidebar |
| card | `#ffffff` | `#161617` | cards, popovers |
| fg | `#1b1b1b` | `#ffffff` | main text |
| fg-2 | `#2f2f31` | `#e3e3e6` | strong secondary text |
| muted | `#5c5c5e` | `#959597` | secondary text, labels, icons |
| faint | `#9c9d9f` | `#565759` | timestamps, placeholders |
| muted-bg | `#ececed` | `#1a1a1b` | chips, code, table header |
| accent-bg | `#e1e1e2` | `#1d1d1f` | hover, selected row |
| border | `#dedede` | `#232426` | card and control borders |
| border-subtle | `#f1f1f1` | `#1a1b1d` | dividers inside cards |
| primary | `#0047d8` | `#3b6cff` | links, focus ring, selection |
| ok | `#26a544` | `#40b956` | success |
| warn | `#e0b100` | `#fecb00` | warning (dots and icons only, not text) |
| bad | `#e33e46` | `#ff5d5e` | error, blocked |
| busy | `#0047d8` | `#6a9cff` | running / live |

- Shadow: light `0 3px 6px -2px #00000005, 0 1px 1px #0000000a`; dark `0 .5px 1px 1px #0000004d`.
- Charts: primary first, then grays (light `#1b1b1b #5c5c5e #9c9d9f #c6c7ca`, dark `#ffffff #959597 #565759 #2f2f31`).
- Tints for status backgrounds: mix the status color with transparent (10% background, 30% border). Don't add new hex values.

## Fonts

| Role | Font | Fallback stack |
|---|---|---|
| UI and body | **Inter** (variable) | `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` |
| Code, logs, IDs | **JetBrains Mono** (variable) | `ui-monospace, SFMono-Regular, Menlo, monospace` |

Bundle both locally, because the page must work offline. With npm, use `@fontsource-variable/inter`
and `@fontsource-variable/jetbrains-mono` (family names `"Inter Variable"` and
`"JetBrains Mono Variable"`). Never load them from Google Fonts or another CDN.

Scale: 20px/28 page title (600), 15px/24 body and post titles (400 / 600), 13px/18 UI labels
(500), 12px/16 meta. Mono at 12–13px with ligatures off. Use `-webkit-font-smoothing: antialiased`.

The prized.dev marketing pages use **Zalando Sans** (headlines: weight 500, -0.04em tracking) and
**Ubuntu Sans Mono**. That's the marketing look. Use Inter / JetBrains Mono for the board, which
should look like the dashboard.

## Icons

- **Solar, Bold style** only (solid, rounded, filled). No lucide, no emoji, no second icon set.
- 63 face and people icons are bundled at `board/static/icons/solar.json` as
  `{ "name-bold": "<svg …>" }`. They use `fill="currentColor"`, so they take the text color.
  Render them through one `<Icon name="…" />` component.
- Sizes: 16px next to small text, 20px in rows and buttons, 24px in avatars, 40px in empty states.
  Default color is muted. Use fg when active, and a status color only next to that status.
- Agent avatars: hash the agent name to pick one icon from this pool, so the same name always
  gets the same icon:
  `ghost, ghost-smile, sticker-smile-circle, sticker-smile-circle-2, sticker-smile-square,
  smile-circle, smile-square, emoji-funny-circle, emoji-funny-square, masks, mask-happy,
  user-rounded, user-circle` (each with `-bold`).
- Fixed meanings: `user-hand-up` = needs the human, `users-group-rounded` = all agents,
  `sleeping-circle` = nothing posted yet, `sad-circle` = no matches, `expressionless-circle` =
  connection lost, `confounded-circle` = something failed to render. Don't use the sad, confounded
  or skull icons as avatars.
- Credit in the README: "Icons: Solar by 480 Design, CC BY 4.0."
