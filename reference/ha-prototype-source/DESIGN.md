---
name: Rivian Solar EV Charging
description: Compact Home Assistant controls for safe, legible solar charging operation.
colors:
  surface: "var(--ha-card-background, var(--card-background-color))"
  surface-soft: "var(--secondary-background-color)"
  text: "var(--primary-text-color)"
  text-muted: "var(--secondary-text-color)"
  primary: "var(--primary-color)"
  success: "var(--success-color)"
  warning: "var(--warning-color)"
  error: "var(--error-color)"
  divider: "color-mix(in srgb, var(--divider-color) 64%, transparent)"
typography:
  body:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "16px"
    lineHeight: 1.45
  title:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "24px"
    fontWeight: 650
    lineHeight: 1.15
  compact-title:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.15
  page-title:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "30px"
    fontWeight: 650
    lineHeight: 1.15
  label:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "12px"
    fontWeight: 650
    letterSpacing: "0.03em"
    lineHeight: 1.35
  helper:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "13px"
    lineHeight: 1.4
  diagnostic:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "14px"
    lineHeight: 1.4
  compact-value:
    fontFamily: "var(--paper-font-body1_-_font-family, Roboto, sans-serif)"
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.2
rounded:
  input: "8px"
  control: "9px"
  surface: "12px"
  pill: "999px"
spacing:
  xs: "8px"
  sm: "10px"
  md: "12px"
  lg: "16px"
  xl: "20px"
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "40px"
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "var(--primary-background-color, var(--text-primary-color, #fff))"
    rounded: "{rounded.control}"
    padding: "0 13px"
    height: "40px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
    padding: "{spacing.lg}"
  state-chip:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.pill}"
    padding: "7px 10px"
  field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "40px"
---

# Design System: Rivian Solar EV Charging

## Overview

**Creative North Star: "Compact Home Assistant controls"**

This is an operational surface for rapid setup and safe control. It follows the existing Savant-Card compact language: small, readable tiles; gently tinted surfaces; soft offset depth; 12px corner language; and restrained spacing. The current implementation is the authority for exact values, while Home Assistant theme variables own the palette.

The visual system keeps requested state, observed telemetry, and safety state legible at a glance. Dry-run, fault, pending, stale, and unavailable conditions are explicit states; missing telemetry remains an em dash or a clear not-reported label rather than being converted to zero. The panel and card use the same summary and control vocabulary, with compact density in the card variant.

**Key Characteristics:**
- Compact, scan-friendly controls with soft tonal gradients.
- Home Assistant theme variables are the palette source of truth.
- Requested, observed, stale, dry-run, fault, and unavailable states stay distinct.
- Accessible focus, disabled, error, and warning treatments are part of the visual language.

## Colors

The palette is inherited from Home Assistant so the surface remains native to the host theme. Solar-specific meaning is assigned semantically: primary for action, success for enabled state, warning for stale or pending state, and error for faults or destructive controls.

### Primary
- **Theme Primary** (`var(--primary-color)`): Primary actions, active borders, and the subtle tint mixed into elevated operational surfaces.

### Neutral
- **Card Surface** (`var(--ha-card-background, var(--card-background-color))`): Cards, controls, dialogs, and the primary panel canvas.
- **Soft Surface** (`var(--secondary-background-color)`): Quiet controls, chips, neutral callouts, and diagnostic rows.
- **Primary Text** (`var(--primary-text-color)`): Headings, values, and readable control labels.
- **Secondary Text** (`var(--secondary-text-color)`): Explanations, metric notes, hints, and metadata.
- **Divider** (`color-mix(in srgb, var(--divider-color) 64%, transparent)`): Quiet control and select borders.

### Named Rules
**The Host Palette Rule.** Do not introduce a competing fixed palette when Home Assistant theme variables provide the role.

## Typography

**Display Font:** Not used; this is an operational control surface.
**Body Font:** Home Assistant's body font variable, with Roboto and sans-serif fallback.
**Label/Mono Font:** No distinct label or mono family is present.

**Character:** Neutral and compact, with weight and case doing the hierarchy work instead of decorative type treatment.

### Hierarchy
- **Title** (650, 24px, 1.15): Panel summary headings; the card uses the observed compact 20px variant.
- **Page title** (650, 30px, 1.15): The panel's top-level heading.
- **Body** (host body font, 16px, 1.45): Explanatory copy, status reasons, and control guidance.
- **Label** (650, 12px, 1.35, 0.03em): Metric labels and forecast terms; uppercase is used for telemetry labels.
- **Helper / diagnostic** (13px / 14px): Selector labels and compact diagnostic values.
- **Value** (650, 23px panel / 19px compact card): Current telemetry values, allowed to wrap when narrow.

### Named Rules
**The Readable State Rule.** Use concise labels and supporting notes so a requested value is never mistaken for observed telemetry.

## Layout

The panel centers a maximum-width 1080px content region with 28px top, responsive horizontal, and 48px bottom padding. The card keeps 16px internal padding. Operational groups use a 12px gap and 16px internal padding; actions use a 10px gap. The desktop telemetry grid starts at five columns, moves to three columns below 900px, and two columns below 600px; the final card spans the row on narrow screens. At 390px and below, the rhythm tightens to 8px gaps and 12px padding, with actions stacking full-width.

Controller selection is a native-looking 40px field. The panel header places it beside the title on wide screens and below it on small screens. Dialogs cap at 640px and remain within a 12px viewport gutter.

## Elevation & Depth

Depth is a hybrid of tonal layering and soft ambient shadows. Summary, operation, forecast, diagnostic, metric, and empty surfaces share a subtle 160-degree gradient that mixes the surface with the host primary color, then use one restrained shadow. Dialogs use a stronger shadow because they are a temporary layer.

### Shadow Vocabulary
- **Operational surface** (`0 8px 22px rgb(15 23 42 / .09)`): Shared ambient lift for cards, metrics, summaries, and diagnostics.
- **Dialog layer** (`0 18px 48px rgb(15 23 42 / .25)`): Stronger separation for setup and physical-stop confirmation dialogs.

### Named Rules
**The Soft-Lift Rule.** Use tonal tint and quiet ambient shadow for resting surfaces; reserve stronger depth for modal layers.

## Shapes

The form language is gently rounded rather than pill-heavy: 12px for surfaces and dialogs, 9px for buttons and selects, and 8px for compact diagnostic rows. State chips are the deliberate exception and use a full pill. Borders are quiet and theme-derived; surfaces do not use ornamental outlines. Focus uses a visible 3px outline with a 2px offset.

## Components

### Buttons
- **Shape:** Gently rounded controls (9px) with a 40px minimum height.
- **Primary:** Host primary background, host text-on-primary color, and 13px horizontal padding.
- **Default:** Card-surface background, primary text, and a quiet theme-derived border.
- **Hover / Focus:** Hover shifts the border to primary and adds an 8% primary surface mix; focus-visible uses a 3px outline with 2px offset.
- **Danger / Disabled:** Danger uses the host error role. Disabled controls remain present at 52% opacity and do not imply that an action succeeded.

### Chips
- **Style:** Full-pill state chip (999px), 7px 10px padding, 12px semibold label, and soft-surface background by default.
- **State:** Enabled uses success; fault uses error; stale and pending use warning. The words remain explicit rather than relying on color alone.

### Cards / Containers
- **Corner Style:** Shared 12px surface radius.
- **Background:** Card surface with a 160-degree, subtle primary tint.
- **Shadow Strategy:** Operational surface shadow from Elevation & Depth.
- **Internal Padding:** 16px by default; 12px in the smallest responsive mode.
- **Grouping:** Summary, operation, forecast, diagnostics, metrics, and empty states share the same container grammar.

### Inputs / Fields
- **Style:** Native Home Assistant font and theme colors, 40px minimum height, 9px radius, card-surface fill, and quiet divider border.
- **Focus:** Visible 3px focus outline with 2px offset.
- **Error / Disabled:** Errors use explicit callouts and host error color; unavailable or disabled states are labeled and never represented as zero.

### Telemetry Metrics
- **Role:** Five compact metric tiles expose requested current, available solar, wall power, observed current, and vehicle state.
- **Typography:** 12px uppercase labels, semibold values, and muted explanatory notes.
- **Truthfulness:** Formatting helpers render missing values as `—`; labels identify requested versus observed readings.

### Diagnostics and Dialogs
- **Role:** Details sections expose readiness, input freshness, units, control phase, and commissioning actions without crowding the primary control path.
- **Shape:** Diagnostic rows use 8px radius; dialogs use 12px radius, 20px internal form padding, and the dialog shadow.
- **Safety:** Physical stop confirmation is a separate explicit action; stale, dry-run, pending, fault, and unavailable states remain visible.

## Do's and Don'ts

### Do:
- **Do** use Home Assistant theme variables for palette roles and keep semantic state readable in text.
- **Do** preserve the compact 12px surface radius, 12px rhythm, 16px default padding, and soft offset shadow.
- **Do** distinguish requested values from observed telemetry and show freshness or fault context beside the affected control.
- **Do** keep focus-visible outlines, disabled affordances, and warning/error callouts visible.

### Don't:
- **Don't** replace missing readings with `0`, or imply physical proof from a schedule write/readback.
- **Don't** add a new fixed brand palette when the Home Assistant variables already own the theme.
- **Don't** use dense decorative gradients, hard borders, or oversized hero typography in this operational surface.
- **Don't** hide dry-run, stale, pending, fault, or unavailable state behind color alone.
