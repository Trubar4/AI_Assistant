# Liebherr Design System – Anweisungen für Claude Code

## Pflicht
- Verwende AUSSCHLIESSLICH das Design System in `design-system/`.
- Importiere immer über `design-system/lds.css`. Keine eigenen Farben, Spacings, Schriften, Radii.
- Farben/Spacings/Typo NUR über CSS-Variablen aus `tokens/` referenzieren
  (z. B. `var(--color-bg-surface)`, `var(--space-md)`, `var(--font-head)`).
- Komponenten zuerst aus `components/components.css` wiederverwenden
  (`.lds-btn`, `.lds-input`, `.lds-card`, `.lds-alert`, `.lds-pill`, `.lds-badge`,
  `.lds-nav`, `.lds-appbar`, `.lds-breadcrumb`). Nur erweitern wenn nichts passt.
- Themes: `data-theme="co-light" | "co-dark" | "ha-light" | "ha-dark"` am `<html>`.
- Sprache: Deutsch (de-DE), Tonfall sachlich, technisch, knapp.

## Referenz-Mock
- `reference/Maschinen-Assistent.html` zeigt die Ziel-Optik, Komponenten-Komposition
  und Interaktionsmuster. Visuelle Sprache, Header-Struktur, Antwort-Karten,
  Fehlercode-Lookup und Voice-Overlay daran orientieren.
- `reference/MaschinenAssistent_Projektbriefing.md` enthält das Funktions-Briefing.
- `reference/Design System.html` zeigt alle Tokens und Komponenten live mit Theme-Switcher.

## Verboten
- Inline-Farben (`#fff`, `rgb(…)`) außerhalb der Token-Dateien.
- Tailwind, Bootstrap, MUI, Chakra usw. – kein zweites Design-System.
- Emojis in der UI (außer im Briefing explizit gefordert).
- Eigene Schriftarten – nur LiebherrHead / LiebherrText (in typography.css definiert).

## Erweiterungs-Regeln
- Neue Komponenten → in `design-system/components/` als `<name>.css`,
  und in `lds.css` mit `@import` registrieren.
- Neue Tokens → in `tokens/primitives.css` (Rohwert) UND in
  jeder `roles-*.css` als semantische Rolle.
- Vor dem Bauen neuer Screens: `Maschinen-Assistent.html` öffnen und prüfen,
  ob ein vergleichbares Muster (Hero, Liste, Lookup, Detail) schon existiert.

## Verwendung in neuer App

```html
<link rel="stylesheet" href="/design-system/lds.css" />
<html data-theme="co-light">
  <button class="lds-btn lds-btn--primary">Senden</button>
</html>
```

Theme-Wechsel zur Laufzeit:
```js
document.documentElement.dataset.theme = 'co-dark';
```
