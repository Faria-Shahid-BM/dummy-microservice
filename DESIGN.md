---
name: CAD Workbench
description: Scope-gated LLM document review console for bank credit-adjudication workflows.
colors:
  institutional-sage: "#87ae73"
  sage-deep: "#5f8a4a"
  sage-ink: "#4c7239"
  sage-wash: "#e7f0e0"
  cool-paper: "#f6f8f2"
  pure-white: "#ffffff"
  cool-mist: "#eef2e7"
  border-sage: "#dfe6d3"
  soft-divider: "#ebefe3"
  slate-ink: "#263021"
  muted-slate: "#74806a"
  faint-sage: "#94a086"
  verified-green: "#3f8f4f"
  alert-red: "#c0392b"
  caution-amber: "#b06a1a"
  amber-wash: "#fbf1e2"
  amber-border: "#eeddb8"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "1.05rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "0.9rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "normal"
rounded:
  sm: "6px"
  md: "10px"
  lg: "14px"
components:
  button-primary:
    backgroundColor: "{colors.institutional-sage}"
    textColor: "{colors.pure-white}"
    rounded: "{rounded.sm}"
    padding: "0.6rem 1rem"
    typography:
      fontSize: "0.85rem"
      fontWeight: 600
      letterSpacing: "0.01em"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.muted-slate}"
    rounded: "{rounded.sm}"
    padding: "0.35rem 0.7rem"
    typography:
      fontSize: "0.75rem"
      fontWeight: 500
  card:
    backgroundColor: "{colors.pure-white}"
    rounded: "{rounded.lg}"
    padding: "1.5rem 1.75rem"
  input-field:
    backgroundColor: "{colors.pure-white}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.sm}"
    padding: "0.55rem 0.7rem"
    typography:
      fontSize: "0.9rem"
  sidebar-nav-item:
    backgroundColor: "transparent"
    textColor: "{colors.muted-slate}"
    rounded: "{rounded.sm}"
    padding: "0.6rem 0.75rem"
    typography:
      fontSize: "0.85rem"
      fontWeight: 500
  sidebar-nav-item-active:
    backgroundColor: "{colors.institutional-sage}"
    textColor: "{colors.pure-white}"
    rounded: "{rounded.sm}"
    padding: "0.6rem 0.75rem"
    typography:
      fontSize: "0.85rem"
      fontWeight: 600
  stat-card:
    backgroundColor: "{colors.cool-paper}"
    rounded: "{rounded.md}"
    padding: "0.7rem 0.85rem"
---

# Design System: CAD Workbench

## Overview

**Creative North Star: "The Verification Desk"**

CAD Workbench reads as a procedural, checklist-driven workspace, not a marketing surface. One quiet accent — Institutional Sage — marks entitlement, active state, and primary action; everything else stays flat, cool, and legible, because the interface's job is to get out of the way of the document underneath it. Borders and subtle background-tone shifts (card vs. page, sidebar vs. content) carry the visual hierarchy instead of color or motion, matching a tool whose real content is a legal opinion, a valuation report, or an audit trail rather than the chrome around it.

The system runs on the OS-native font stack end to end — there is no separate display face. That is deliberate: a credit-adjudication review console earns trust by feeling unadorned and consistent, not styled. Status color (green/amber/red) is reserved strictly for verification outcomes — match/mismatch/missing, warning banners, audit-log status — never for decoration.

**Key Characteristics:**
- One accent color, spent only on entitlement and primary action, never on decoration
- Flat-first surfaces; the only elevation is a restrained, identical ambient shadow on every card and button
- Single OS-native font stack for every role — hierarchy comes from size and weight, not typeface
- Verification-outcome colors (green/amber/red) are semantic, not brand — they never appear outside a status, warning, or result context
- Sidebar app-shell (nav + content) is the one structural layout pattern, reused by both the reviewer dashboard and the admin console

## Colors

A cool, low-saturation sage-and-paper palette: one green accent, warm-neutral paper tones for surfaces, and a strict three-color status vocabulary for outcomes.

### Primary
- **Institutional Sage** (`#87ae73`): the one accent. Primary button fill, active sidebar item, input focus ring, and any "this is currently entitled / selected" signal.
- **Sage Deep** (`#5f8a4a`): the dark end of the primary gradient and the primary button's resting-state shadow tint.
- **Sage Ink** (`#4c7239`): darkest sage step; hover-state text on ghost buttons and stat-card numerals — sage doing double duty as data, not just chrome.
- **Sage Wash** (`#e7f0e0`): the palest sage tint; focus-ring glow, hover background on ghost buttons and table rows, and the highlighted background behind inline `<code>`.

### Neutral
- **Cool Paper** (`#f6f8f2`): page background and the background of secondary "data" surfaces (stat cards, redline diff blocks, chat bubbles) sitting inside a white card.
- **Pure White** (`#ffffff`): card and input surfaces — the "document" layer, one level up from the paper background.
- **Cool Mist** (`#eef2e7`): sidebar background — a third, distinct neutral so the app-shell's two structural regions never share a fill.
- **Border Sage** (`#dfe6d3`): default input and sidebar borders.
- **Soft Divider** (`#ebefe3`): the lighter border used inside cards (card outline, table rule lines) — one step quieter than Border Sage.
- **Slate Ink** (`#263021`): primary text.
- **Muted Slate** (`#74806a`): secondary text — subtitles, field labels, table headers.
- **Faint Sage** (`#94a086`): tertiary text — hints, empty-state copy, timestamps.

### Status
- **Verified Green** (`#3f8f4f`): match / success outcomes, in tables and the collateral log.
- **Alert Red** (`#c0392b`): mismatch / error outcomes and deleted-text markup in the redline diff.
- **Caution Amber** (`#b06a1a`) with **Amber Wash** (`#fbf1e2`) background and **Amber Border** (`#eeddb8`): warning banners, "possible missing section" flags, and forbidden/flagged states.

### Named Rules
**The One Accent Rule.** Institutional Sage is the only brand color in the system. It never appears as a large fill outside the primary button and the active sidebar item — everywhere else it shows up only as a thin ring, a wash, or a text tint.

**The Status-Only-Color Rule.** Verified Green, Alert Red, and Caution Amber are reserved for verification outcomes and system status. They are never used decoratively, and no other color is ever pressed into service for status.

## Typography

**Font (all roles):** `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif`

**Character:** One OS-native stack, no display face. Hierarchy is built entirely from size, weight, and letter-spacing — a deliberately unstyled, procedural voice.

### Hierarchy
- **Headline** (700, 1.5rem, -0.01em): page-level title (`<h1>` in the login/console header). Used once per screen.
- **Title** (600, 1.05rem): card and sidebar section headers (`.card h2`, `.sidebar h1`). Also used at 1.3rem/700 for the dashboard's per-service topbar heading — same role, larger step.
- **Body** (400, 0.9rem): default running text — subtitles, hints, table cells, observations.
- **Label** (500, 0.8rem): form field labels and small captions (`.field span`, `.file-field span`).
- **Micro-label** (600, 0.7rem, 0.03em, uppercase): stat-card units (`.stat-card .l`) — the one place the system goes to uppercase+tracked type.

### Named Rules
**The No-Display-Face Rule.** The interface never reaches for a second font family. Every heading, no matter how prominent, is the same stack at a larger size and heavier weight — never a distinct typeface.

## Layout

App-shell pattern: a fixed 230px sidebar (nav + session footer) beside a flexible main-content column, `min-height: 100vh`, used identically by the reviewer dashboard and the admin console. Main content caps at `860px` max-width with `2.25rem 2.5rem 4rem` padding; the login page instead uses a centered single-column `.page` capped at `720px`.

Spacing rhythm is rem-based and fairly tight: `0.3rem`–`0.6rem` for inline gaps (icon-to-label, chip padding), `0.85rem`–`1.1rem` for stacked block spacing inside a card, `1.5rem`–`1.75rem` between cards and around card padding, and `2.25rem`+ only at the page/main-content margins. There is no explicit spacing token scale in code (no `--spacing-*` custom properties) — the rhythm above is observed, not tokenized; treat it as a scale to match, not exact values to enforce.

Cards stack vertically inside main-content with `1.5rem` bottom margin between them. Tables and stat-row grids are the only components that go full-width inside a card; everything else respects the card's own padding.

## Elevation & Depth

Flat by default, with one restrained, identical ambient shadow reused everywhere something needs to lift off the page: cards and buttons, and nothing else. There is no shadow hierarchy — a card two levels deep in a flow gets the exact same shadow as a card at the top, because depth here is not used to encode importance, only to distinguish "surface" from "page."

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 1px 2px rgba(38,48,33,0.05), 0 1px 8px rgba(38,48,33,0.04)`): every `.card`. A two-layer, very soft shadow — barely visible, intentionally quiet.
- **Button rest** (`box-shadow: 0 1px 2px rgba(76,114,57,0.25)`): primary button at rest.
- **Button hover** (`box-shadow: 0 4px 10px rgba(76,114,57,0.32)`): primary button on hover, paired with a 1px `translateY` lift and a lighter gradient swap.

### Named Rules
**The Consistent-Lift Rule.** Only cards and buttons are ever elevated, and always with the same shadow value for their type. No component invents a heavier or lighter shadow to look more or less important.

## Shapes

Three-step radius scale: `6px` (small — inputs, buttons, ghost buttons, sidebar items), `10px` (medium — stat cards, chat bubbles, the redline diff block), `14px` (large — cards, the primary content container). A smaller `4px` micro-radius appears only on small status pills (warning badges, "possible missing section" flags) — a deliberate one-off step below the main scale for anything pill-sized rather than panel-sized. No component uses a fully rounded (pill/circle) shape; corners are always gently curved, never sharp, never fully round.

## Components

### Buttons
- **Shape:** 6px radius, all variants.
- **Primary:** diagonal gradient fill (Institutional Sage → Sage Deep, 135°), white text, 600-weight, `0.6rem 1rem` padding, 0.01em letter-spacing. This is the one component allowed to use a gradient instead of a flat fill.
- **Hover / Active:** hover swaps to a lighter gradient and increases the shadow while lifting 1px (`translateY(-1px)`); active returns to rest position and shadow. Disabled swaps to a flat grey gradient, loses its shadow, and dims text — never fully hidden, always visibly inert.
- **Ghost (`.clear`):** transparent background, 1px Border Sage outline, Muted Slate text, no shadow, smaller padding/type (0.75rem). Hover fills with Sage Wash and darkens text to Sage Ink — used for secondary actions (refresh, clear, remove) that shouldn't compete with the primary action.

### Cards
- **Corner Style:** 14px radius.
- **Background:** Pure White on Cool Paper page background.
- **Shadow Strategy:** the single card-lift shadow (see Elevation).
- **Border:** 1px Soft Divider.
- **Internal Padding:** `1.5rem 1.75rem`.

### Inputs / Fields
- **Style:** Pure White background, 1px Border Sage, 6px radius, `0.55rem 0.7rem` padding.
- **Focus:** border switches to Institutional Sage plus a 3px Sage Wash glow ring — no border-width change, so layout never shifts on focus.
- **File inputs:** the native file-picker button is restyled as a small Sage Wash chip (Sage Ink text, Institutional Sage border) rather than left as browser-default chrome.

### Navigation (sidebar)
- **Style:** flat nav items, Muted Slate text, 6px radius, `0.6rem 0.75rem` padding.
- **Hover:** Sage Wash background, Sage Ink text — same treatment as ghost-button hover, keeping the "soft sage wash = interactive, not committed" language consistent across the system.
- **Active:** full Institutional Sage gradient fill, white 600-weight text — the same visual weight as a primary button, signaling "you are here" with the system's one reserved accent.
- **Structure:** every sidebar carries the same three-part shape — title + subtitle, a stack of nav items, and a session footer (username, scopes, log-out button) pinned to the bottom via `margin-top: auto`.

### Signature: Redline Diff
The document-diff and collateral panels' most distinctive surface: a Cool-Paper, 10px-radius block with inline deletion/insertion markup — deletions get Alert Red strikethrough text on a faint red wash, insertions get Verified Green underlined text on a faint green wash. A jump-to-change interaction briefly outlines the target span in Institutional Sage. This is the one place in the system where status color is applied inline, character-by-character, rather than as a block-level badge.

### Signature: Chat Bubble
The policy Q&A panel's message list: bubbles use the card's 10px radius, Cool-Paper background and Soft Divider border by default; the user's own messages align right and swap to Sage Wash with an Institutional Sage border, the system's only left/right-aligned, self-vs-other visual distinction.

## Do's and Don'ts

### Do:
- **Do** reserve the Institutional Sage gradient fill for the primary button and the active sidebar item; every other interactive surface stays flat or ghost-bordered.
- **Do** keep shadows to exactly the two defined values (card lift, button rest/hover); never introduce a third shadow language for a new component.
- **Do** use Verified Green / Alert Red / Caution Amber only for verification outcomes and system status — never as branding or decoration.
- **Do** size new type by the existing scale (Headline 1.5rem/700 → Title 1.05rem/600 → Body 0.9rem/400 → Label 0.8rem/500) rather than picking an ad hoc size or weight.
- **Do** use the 4px micro-radius only for small pill-shaped status badges, and the 6/10/14px scale for everything else.

### Don't:
- **Don't** introduce a second accent color; the system is deliberately one-accent, and a second would break the One Accent Rule.
- **Don't** add a display or heading font — the OS-native stack is the entire typographic voice, at every size.
- **Don't** give tables, chips, badges, or chat bubbles a shadow of their own; only cards and buttons are ever elevated.
- **Don't** use a fully rounded (pill/circle) shape anywhere; the system's curves always stop at the 4/6/10/14px scale.
- **Don't** vary a card's shadow by nesting depth or perceived importance — every card gets the same shadow, on purpose.
