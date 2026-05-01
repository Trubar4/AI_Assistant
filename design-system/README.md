# Liebherr-Inspired Design System (`lds`)

A **reusable, role-based design system** distilled from Liebherr's pattern library + LiMain Figma color roles.
Drop-in for HTML/CSS, React, or any framework. Theme-able via CSS custom properties.

## Structure

```
design-system/
├── tokens/
│   ├── primitives.css        # raw color/spacing/type values
│   ├── roles-co-light.css    # Corporate · Light theme
│   ├── roles-co-dark.css     # Corporate · Dark
│   ├── roles-ha-light.css    # Hausgeräte · Light
│   ├── roles-ha-dark.css     # Hausgeräte · Dark
│   ├── typography.css        # type scale + font-face
│   └── spacing.css           # spacing/radii/shadows
├── components/
│   ├── button.css
│   ├── input.css
│   ├── card.css
│   ├── alert.css
│   ├── pill.css
│   ├── nav.css
│   └── ...
└── lds.css                   # one-stop import
```

## Use in another project

```html
<link rel="stylesheet" href="https://cdn.your-org/lds/lds.css">
<html data-theme="co-light"> <!-- or co-dark, ha-light, ha-dark -->
```

```html
<button class="lds-btn lds-btn--primary">Senden</button>
<div class="lds-card">…</div>
<div class="lds-alert lds-alert--warning">…</div>
```

## Color role naming (from Figma)

Roles (not hex codes) — this is what makes it portable:

- **Base**: `background-colored | -default | -bright | -dim`, `surface-primary | -default | -high | -higher | -disabled`, `surface-{prominent|moderate|subtle}-{hover|pressed}`, `outline-{default|bright|brightest|dim|variant|disabled}`
- **Inverse**: `surface-inverse | -inverse-high | -inverse-higher`, `on-surface-{inverse|on-inverse|on-inverse-muted}`, `outline-on-inverse`
- **Accent** (1–5): `surface-accent-N`, `on-surface-on-accent`, `surface-accent-N-hover`, `outline-accent-N`, `on-surface-accent-N`
- **Functional**: `error | warning | success` × `surface | on-surface | -hover | outline`
- **On-surface**: `on-color | -default | -muted | -on-disabled` for text/icon contrast

Switching themes = swap the role file. Components reference roles only.
