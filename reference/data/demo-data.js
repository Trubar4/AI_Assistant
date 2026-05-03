// Canned demo data — German content matching real Liebherr LR 1300 SX context
window.DEMO_DATA = {
  machine: {
    series: "LR 1300 SX",
    type: "Raupenkran",
    serial: "WLH123456789",
    manualVersion: "DE 04/2025",
    lastSync: "vor 2 Min."
  },

  // Suggested first-question chips
  suggestions: [
    {
      id: "s1",
      icon: "wrench",
      tone: "yellow",
      title: "Aufrüsten 60 m Hauptausleger",
      sub: "Schritt-für-Schritt-Anleitung mit Sicherheitshinweisen"
    },
    {
      id: "s2",
      icon: "alert",
      tone: "clay",
      title: "Fehlercode E1042 nachschlagen",
      sub: "Hydraulikdruck zu niedrig – Diagnose & Maßnahmen"
    },
    {
      id: "s3",
      icon: "calendar",
      tone: "slate",
      title: "Wartungsintervall 500 h",
      sub: "Welche Arbeiten sind fällig?"
    },
    {
      id: "s4",
      icon: "snow",
      tone: "leaf",
      title: "Inbetriebnahme bei Kälte (< -10 °C)",
      sub: "Vorwärmen, Hydrauliköl, Batterieprüfung"
    }
  ],

  // Example user questions (chips)
  quickChips: [
    "Welche Schritte beim Aufrüsten mit 60 Metern?",
    "Abstützung prüfen – wie?",
    "Gegengewicht montieren",
    "Warnung Last-Moment-Begrenzer ignorieren?"
  ],

  recentHistory: [
    { id: "h1", title: "Aufrüsten Hauptausleger 60 m – Schritt 4", time: "Heute, 14:23", status: "ok" },
    { id: "h2", title: "Fehlercode E1042 – Hydraulikdruck zu niedrig", time: "Heute, 11:08", status: "warning" },
    { id: "h3", title: "Lastmoment-Begrenzer kalibrieren", time: "Gestern, 16:42", status: "ok" },
    { id: "h4", title: "Fehlercode E2117 – CAN-Bus Timeout", time: "Gestern, 09:14", status: "error" },
    { id: "h5", title: "Gegengewicht 45 t montieren", time: "23.04., 10:30", status: "ok" },
    { id: "h6", title: "Wartung 500 h – Filterwechsel", time: "22.04., 08:12", status: "ok" }
  ],

  // The canonical demo answer (Use Case 1)
  answerAufruesten: {
    confidence: "BELEGT",
    confidenceLabel: "Vollständig im Manual belegt",
    coverageNote: "5 Quellen ausgewertet · 100 % Übereinstimmung",
    breadcrumb: ["Inbetriebnahme", "Hauptausleger", "Aufrüsten 60 m"],
    summary:
      "Beim Aufrüsten des Hauptauslegers mit 60 m Länge müssen vor Beginn die Abstützung, das Gegengewicht und der Lastmoment-Begrenzer überprüft werden. Die folgenden 6 Schritte führen Sie durch das vollständige Aufrüstverfahren gemäß Betriebsanleitung.",
    warning: {
      type: "warning",
      title: "WARNUNG · Quetschgefahr",
      text: "Während des Aufrüstvorgangs darf sich niemand im Schwenkbereich des Auslegers aufhalten. Sicherheitsabstand mindestens 5 m einhalten."
    },
    steps: [
      {
        title: "Maschine waagerecht ausrichten und abstützen",
        text: "Alle 4 Stützen vollständig ausfahren und mit Wasserwaage prüfen. Maximale Neigung 0,5°."
      },
      {
        title: "Gegengewicht 85 t montieren",
        text: "Verwenden Sie ausschließlich freigegebene Gewichtspakete (siehe Tabelle 4-2). Bolzen mit 480 Nm anziehen."
      },
      {
        title: "Hauptausleger-Grundstück anbauen",
        text: "Bolzen 1A und 1B durch die Drehverbindung führen. Sicherungssplinte beidseitig einsetzen."
      },
      {
        title: "Verlängerungsstücke montieren (4 × 12 m)",
        text: "Reihenfolge: Verlängerung A → B → C → D. Bei jedem Stück die hydraulischen Kupplungen prüfen."
      },
      {
        title: "Auslegerkopf anbauen und Seil einziehen",
        text: "Hubseil über Umlenkrolle führen. Mindesteinscherung: 6-fach bei 60 m Konfiguration."
      },
      {
        title: "Lastmoment-Begrenzer aktivieren und prüfen",
        text: "Konfiguration „60m_HA_85t_GG\" auswählen. Funktionsprüfung mit Testlast 2 t durchführen."
      }
    ],
    sources: [
      {
        title: "Aufrüsten des Hauptauslegers",
        breadcrumb: "Inbetriebnahme › Hauptausleger",
        file: "ID_8a3f12-de-DE.html",
        match: "98 %"
      },
      {
        title: "Gegengewicht montieren – 85 t Konfiguration",
        breadcrumb: "Inbetriebnahme › Gegengewicht",
        file: "ID_b41c09-de-DE.html",
        match: "94 %"
      },
      {
        title: "Lastmoment-Begrenzer einrichten",
        breadcrumb: "Sicherheit › LMB",
        file: "ID_2e7d44-de-DE.html",
        match: "89 %"
      },
      {
        title: "Abstützung – Prüfung und Ausfahrwerte",
        breadcrumb: "Inbetriebnahme › Abstützung",
        file: "ID_6f8e21-de-DE.html",
        match: "82 %"
      },
      {
        title: "Hubseilkonfiguration 60 m",
        breadcrumb: "Inbetriebnahme › Hubwerk",
        file: "ID_15a7ce-de-DE.html",
        match: "76 %"
      }
    ],
    relatedQuestions: [
      "Welches Drehmoment für die Auslegerbolzen?",
      "Was tun wenn die Wasserwaage > 0,5° anzeigt?",
      "Wie lange dauert das Aufrüsten typischerweise?"
    ]
  },

  // A second answer with low confidence (for the disclaimer state)
  answerLowConfidence: {
    confidence: "NICHT_BELEGT",
    confidenceLabel: "Nicht eindeutig im Manual",
    coverageNote: "Keine direkte Übereinstimmung gefunden",
    breadcrumb: ["Manuelle Suche"],
    summary:
      "Diese Information konnte ich im Manual nicht eindeutig finden. Bitte schlagen Sie direkt in der Betriebsanleitung nach oder kontaktieren Sie den Service.",
    sources: [
      {
        title: "Mögliche relevante Stelle: Sonderkonfigurationen",
        breadcrumb: "Anhang › Sonderkonfigurationen",
        file: "ID_99e102-de-DE.html",
        match: "32 %"
      }
    ]
  },

  // Error code database
  errorCodes: {
    E1042: {
      code: "E1042",
      title: "Hydraulikdruck im Hauptkreis zu niedrig",
      severity: "warning",
      severityLabel: "Warnung",
      cause:
        "Der Druck im Hauptarbeitskreis liegt unter 280 bar. Mögliche Ursachen: Leckage in einer Schlauchverbindung, defektes Druckbegrenzungsventil, niedriger Ölstand im Hydrauliktank, oder verstopfter Saugfilter.",
      action:
        "1. Maschine sofort sicher abstellen, Last absetzen.\n2. Hydraulikölstand am Schauglas prüfen (Min. 75 %).\n3. Sichtprüfung aller Hochdruck-Schlauchverbindungen auf Leckage.\n4. Saugfilter (Position F1) prüfen, ggf. wechseln.\n5. Falls Fehler weiterhin: Service kontaktieren (Code E1042 melden).",
      sources: [
        { title: "Hydrauliksystem – Hauptkreis", file: "ID_h1042a-de-DE.html", breadcrumb: "Wartung › Hydraulik" },
        { title: "Filterwechsel F1 / F2", file: "ID_filter01-de-DE.html", breadcrumb: "Wartung › Filter" }
      ]
    },
    E2117: {
      code: "E2117",
      title: "CAN-Bus Timeout – Steuergerät Drehwerk",
      severity: "error",
      severityLabel: "Fehler",
      cause:
        "Keine Kommunikation zum Steuergerät des Drehwerks für >2000 ms. Häufig verursacht durch lockere Steckverbindung X42, defektes Buskabel oder Spannungsabfall am Bordnetz unter 22 V.",
      action:
        "1. Maschine ausschalten, 30 s warten, neu starten.\n2. Steckverbindung X42 am Drehwerk-Steuergerät prüfen.\n3. Batteriespannung im Stand messen (Soll: > 24 V).\n4. CAN-Terminierung (120 Ω) am Busende prüfen.\n5. Wenn Fehler bleibt: Service ohne Drehbewegung anfordern.",
      sources: [
        { title: "CAN-Bus Topologie", file: "ID_can01-de-DE.html", breadcrumb: "Elektrik › Bus" },
        { title: "Drehwerk Steuergerät", file: "ID_drehst-de-DE.html", breadcrumb: "Elektrik › Drehwerk" }
      ]
    },
    E0815: {
      code: "E0815",
      title: "Notbremse Hubwerk ausgelöst",
      severity: "error",
      severityLabel: "Sicherheitsabschaltung",
      cause:
        "Die Notbremse des Hubwerks wurde durch den Lastmoment-Begrenzer (LMB) ausgelöst. Aktuell wird eine Last erkannt, die 110 % der zulässigen Tragfähigkeit überschreitet.",
      action:
        "1. KEINE weiteren Bewegungen auslösen.\n2. Lastdiagramm prüfen: Aktuelle Konfiguration, Ausladung, Last.\n3. Last reduzieren oder Ausladung verringern.\n4. LMB nach Lastreduktion über Schlüsselschalter quittieren.\n5. Vorfall im Maschinenbuch dokumentieren.",
      sources: [
        { title: "Lastmoment-Begrenzer LMB", file: "ID_lmb01-de-DE.html", breadcrumb: "Sicherheit › LMB" }
      ]
    }
  }
};
