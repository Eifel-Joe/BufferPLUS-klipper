"""Diagnostik darf den Regelzustand nicht veraendern (Audit-Befund M4).

Wurzel
------
`_tick_debug_metrics` ruft bei `buffer_debug_metrics` einmal pro Sekunde
`_compute_target_feed_speed()`, nur um die Zahl in die Logzeile zu schreiben.
Die Funktion ist aber kein reiner Rechner: sie setzt den Schmitt-Latch
`_modulator_feeding`, armt den High-Flow-Carry und setzt oder loescht den
Post-Full-Clamp.

Das passiert in JEDEM Zustand, auch wenn der Flush-Pfad gar nicht laeuft
(IDLE, LOAD, SYNC). Folge: der erste AUTO-Flush danach trifft eine andere
Hysterese-Entscheidung als ohne eingeschaltete Diagnostik. Eine Messung, die
das Gemessene veraendert.

Die mitgelieferte lll.cfg hat `buffer_debug_metrics: True`, auf dem Drucker
ebenfalls — der Nebeneffekt ist also der Normalfall, nicht die Ausnahme.

Fix
---
Der Flush-Pfad und die Sub-Chunk-Pumpe merken sich den Wert, den sie
tatsaechlich berechnet haben; die Diagnostik liest nur noch diesen mit. Damit
zeigt die Zeile den zuletzt WIRKSAMEN Wert statt eines Phantomwerts, der nie
eine Entscheidung getragen hat.
"""

import logging

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import set_sensor_active
from klipper_extras import buffer_feeder


def make_feeder(state=buffer_feeder.STATE_IDLE):
    printer = FakePrinter()
    printer.fire_event('klippy:connect')
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values={
        "buffer_debug_metrics": True,
        "use_flush_callback_bang_bang": True,
    }))
    feeder._startup_grace_done = True
    feeder._state = state
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    return printer, feeder


def tracker_liefert(feeder, velocity, flow):
    feeder.velocity_tracker.is_ready = lambda: True
    feeder.velocity_tracker.get_velocity = lambda: velocity
    feeder.velocity_tracker.get_volumetric_flow = lambda: flow


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def logs():
    handler = RecordingHandler()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous)


def metrik_zeilen(logs):
    return [r.getMessage() for r in logs.records
            if 'buffer_metrics' in r.getMessage()]


# ---------------------------------------------------------------------------
# 1 — Kern: die Metrik-Zeile fasst den Regelzustand nicht an
# ---------------------------------------------------------------------------

def test_metrik_laesst_den_regelzustand_unberuehrt():
    printer, feeder = make_feeder()
    tracker_liefert(feeder, velocity=20.0, flow=48.0)
    feeder._halt_motion()                       # definierter Ausgangszustand
    assert feeder._modulator_feeding is False
    assert feeder._high_flow_carry_armed_until == 0.0

    feeder._last_metrics_log_time = -10.0       # Zeile faellig
    feeder._main_tick(printer.get_reactor().monotonic())

    assert feeder._modulator_feeding is False, (
        "Metrik-Zeile hat den Schmitt-Latch gesetzt")
    assert feeder._high_flow_carry_armed_until == 0.0, (
        "Metrik-Zeile hat den High-Flow-Carry armiert")


# ---------------------------------------------------------------------------
# 2 — Auch der Post-Full-Clamp bleibt unberuehrt
# ---------------------------------------------------------------------------

def test_metrik_laesst_post_full_clamp_unberuehrt():
    printer, feeder = make_feeder()
    tracker_liefert(feeder, velocity=20.0, flow=48.0)
    feeder._halt_motion()
    set_sensor_active(feeder, 'hall_empty', True)   # HALL3-Zweig im Regler
    feeder._post_full_bias_clamp = True
    assert feeder._post_full_h3_since is None

    feeder._last_metrics_log_time = -10.0
    feeder._main_tick(printer.get_reactor().monotonic())

    # Der Regler startet im HALL3-Zweig die Dwell-Uhr des Post-Full-Clamps.
    # Schon dieser Zeitstempel ist eine Mutation: er entscheidet, wann der
    # Clamp beim naechsten echten Flush faellt.
    assert feeder._post_full_h3_since is None, (
        "Metrik-Zeile hat die Dwell-Uhr des Post-Full-Clamps gestartet")
    assert feeder._post_full_bias_clamp is True, (
        "Metrik-Zeile hat den Post-Full-Clamp geloescht")


# ---------------------------------------------------------------------------
# 3 — Die Zeile zeigt den zuletzt wirksam berechneten Wert
# ---------------------------------------------------------------------------

def test_metrik_zeigt_den_zuletzt_wirksamen_wert(logs):
    printer, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._stepcompress_primed = True
    tracker_liefert(feeder, velocity=12.0, flow=28.0)
    set_sensor_active(feeder, 'hall_empty', True)   # echte Nachfrage

    now = printer.get_reactor().monotonic()
    feeder._on_mcu_flush(now, now + 0.25)           # produktiver Pfad rechnet
    wirksam = feeder._last_target_speed
    assert wirksam > 0.0, "Flush-Pfad hat keinen Wert berechnet"

    feeder._last_metrics_log_time = -10.0
    feeder._main_tick(printer.get_reactor().monotonic())

    zeilen = metrik_zeilen(logs)
    assert zeilen, "keine Metrik-Zeile geschrieben"
    assert "target_speed=%.1fmm/s" % wirksam in zeilen[-1], (
        "Zeile zeigt nicht den wirksamen Wert %.1f: %s" % (wirksam, zeilen[-1]))


# ---------------------------------------------------------------------------
# 4 — Der gemerkte Wert veraltet nicht ueber das Sitzungsende hinaus
# ---------------------------------------------------------------------------

def test_wert_wird_beim_stopp_zurueckgesetzt(logs):
    """Schwaeche des Ansatzes, bewusst geschlossen: ohne Reset zeigte die
    Zeile nach Druckende stundenlang die letzte Foerder-Geschwindigkeit,
    obwohl der Buffer still steht. _halt_motion beendet jede Foerdersitzung
    und ist damit die richtige Stelle."""
    printer, feeder = make_feeder(state=buffer_feeder.STATE_AUTO)
    feeder._stepcompress_primed = True
    tracker_liefert(feeder, velocity=12.0, flow=28.0)
    set_sensor_active(feeder, 'hall_empty', True)
    now = printer.get_reactor().monotonic()
    feeder._on_mcu_flush(now, now + 0.25)
    assert feeder._last_target_speed > 0.0

    feeder._halt_motion()

    assert feeder._last_target_speed == 0.0, (
        "Nach dem Stopp zeigt die Metrik weiter die alte Geschwindigkeit")


# ---------------------------------------------------------------------------
# 5 — Regressionswaechter: die Zeile wird weiterhin geschrieben
# ---------------------------------------------------------------------------

def test_metrik_zeile_wird_weiterhin_geschrieben(logs):
    printer, feeder = make_feeder()
    feeder._last_metrics_log_time = -10.0
    feeder._main_tick(printer.get_reactor().monotonic())

    assert metrik_zeilen(logs), "Metrik-Zeile fehlt ganz"
