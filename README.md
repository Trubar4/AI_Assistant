# Liebherr AI Assistant – Handoff Paket

Inhalt zum Hochladen in ein neues GitHub-Repo, damit Claude Code mit dem
Liebherr Design System weiterarbeiten kann.

## Struktur

```
handoff/
├─ CLAUDE.md                   ← Anweisungen für Claude Code (Repo-Root)
├─ design-system/              ← Reusable Design System
│  ├─ lds.css                  (Single-Entry — importiert alle Tokens + Komponenten)
│  ├─ README.md
│  ├─ tokens/
│  │   ├─ primitives.css       (Rohfarben, Spacing, Typo-Metriken)
│  │   ├─ roles-co-light.css   (Default: Corporate Light)
│  │   ├─ roles-other.css      (CO-Dark, HA-Light, HA-Dark)
│  │   ├─ spacing.css
│  │   └─ typography.css       (LiMAM-CDN-Fonts via @font-face)
│  └─ components/
│      └─ components.css       (Button, Input, Card, Alert, Pill, Badge, Nav, AppBar, Breadcrumb)
│
└─ reference/                  ← Mock + Briefing als Vorlage
   ├─ Maschinen-Assistent.html (kompletter Click-Demo, alle Screens)
   ├─ Design System.html       (Theme-Switcher / Showcase)
   ├─ MaschinenAssistent_Projektbriefing.md
   ├─ styles/                  (Legacy-Styles des Mocks — als Referenz)
   └─ data/                    (Demo-Daten des Mocks)
```

## Schnellstart in neuer App

```html
<link rel="stylesheet" href="/design-system/lds.css" />
<html data-theme="co-light">
  <button class="lds-btn lds-btn--primary">Senden</button>
</html>
```

Theme zur Laufzeit wechseln:
```js
document.documentElement.dataset.theme = 'co-dark';
// erlaubt: co-light | co-dark | ha-light | ha-dark
```

## Hinweis zum Repo-Layout

Beim Hochladen ins neue GitHub-Repo:
- `CLAUDE.md` → Repo-Root (wichtig, sonst findet Claude Code es nicht)
- `design-system/` → Repo-Root
- `reference/` → Repo-Root (oder unter `docs/reference/`)
