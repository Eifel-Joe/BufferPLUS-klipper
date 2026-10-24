"""Gedrosselte Fehlerbehandlung im 50-Hz-Reactor-Tick (Audit-Befund M2).

Wurzel
------
`_main_tick` faengt jede Ausnahme und ruft `logging.exception` ohne Zaehler
oder Drossel. Bei einem dauerhaften Fehler (fehlendes Attribut, verschwundenes
Objekt, Logikfehler in einem Tick-Helfer) sind das 50 Tracebacks pro Sekunde,
rund 3000 pro Minute zu je einem Dutzend Zeilen. klippy.log rotiert nur
taeglich. Dieselbe Klasse wie die P7-78-Logflut (Issue #59), nur mit Traceback.

Zweiter, schwererer Teil derselben Wurzel: ein dauerhaft scheiternder Tick
bedeutet, dass die Aufsicht ausfaellt. Im Tick haengen die HALL1-Prioritaet,
die Safety-Timeouts, der verzoegerte Motor-Disable und die Nachlauf-Streams.
Der Flush-Pfad foerdert dabei weiter. Es wird also weiter Filament gefoerdert,
waehrend niemand mehr auf HALL1-Ueberlauf eskaliert — mechanisch die
gefaehrlichere Haelfte.

Verhalten
---------
Ein einzelner Aussetzer wird geschluckt und geloggt; ein erfolgreicher Tick
setzt die Serie zurueck.

Haelt der Fehler laenger als MAIN_TICK_FAULT_SECONDS an, wird der Druck ueber
den vorhandenen Jam-Weg pausiert (`_trigger_jam` -> `jam_action`), wie beim
Flush-Fangnetz. Entsperrung bleibt BUFFER_CLEAR_JAM.

WARUM ZEITBASIERT und nicht wie beim Flush-Pfad nach drei Fehlern in Folge:
der Tick laeuft mit 50 Hz, drei Fehler waeren 60 ms. Ein kurzer Schluckauf
(ein waehrend eines Neustarts kurz fehlendes print_stats zum Beispiel) wuerde
dann einen laufenden Druck pausieren. Der Flush-Pfad laeuft mit 4-5 Hz, dort
sind drei Fehler rund 600 ms. Gleiche Politik, an die Taktrate angepasste
Schwelle.

siehe buffer_feeder._on_mcu_flush, tests/test_flush_fault_guard.py
"""

import logging

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder
from klipper_extras._buffer_common import MAIN_TICK_INTERVAL


TICK_MARKER = "main_tick error"


def make_feeder():
    printer = FakePrinter()
    printer.fire_event('klippy:connect')
    feeder = buffer_feeder.BufferFeeder(
        FakeConfig(printer=printer, values={"jam_action": "PAUSE"}))
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    return printer, feeder


def arm_fault(feeder):
    """Laesst einen Tick-Helfer werfen — stellvertretend fuer jeden Fehler
    im Tick. _check_debounce laeuft vor dem Grace-Check und damit immer."""
    def boom(eventtime):
        raise RuntimeError("Fehler im Tick")
    feeder._check_debounce = boom


def heal(feeder):
    feeder._check_debounce = lambda eventtime: None


def run_ticks(feeder, count, start=100.0):
    """Ruft _main_tick mit echter Tick-Kadenz. Gibt die zuletzt benutzte
    Zeit zurueck."""
    t = start
    for _ in range(count):
        feeder._main_tick(t)
        t += MAIN_TICK_INTERVAL
    return t


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


def tick_errors(logs):
    return [r for r in logs.records if TICK_MARKER in r.getMessage()]


# ---------------------------------------------------------------------------
# 1 — Kern: kein Logsturm
# ---------------------------------------------------------------------------

def test_logausgabe_gedrosselt(logs):
    printer, feeder = make_feeder()
    arm_fault(feeder)

    run_ticks(feeder, 200)          # 4 Sekunden Dauerfehler

    treffer = tick_errors(logs)
    assert 0 < len(treffer) <= 6, (
        "200 Fehl-Ticks erzeugten %d Logzeilen — erwartet hoechstens 6"
        % len(treffer))


# ---------------------------------------------------------------------------
# 2 — Einzelner Aussetzer bleibt folgenlos
# ---------------------------------------------------------------------------

def test_einzelner_aussetzer_setzt_serie_zurueck():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    run_ticks(feeder, 1)
    heal(feeder)
    run_ticks(feeder, 1, start=100.02)

    assert feeder._main_tick_fault_count == 0, "Serie nach gutem Tick nicht zurueckgesetzt"
    assert feeder._jam_active is False


# ---------------------------------------------------------------------------
# 3 — Kurzer Aussetzer pausiert den Druck NICHT
# ---------------------------------------------------------------------------

def test_kurzer_aussetzer_pausiert_nicht():
    printer, feeder = make_feeder()
    arm_fault(feeder)

    run_ticks(feeder, 10)           # 200 ms, deutlich unter der Schwelle

    assert feeder._jam_active is False, (
        "200 ms Stoerung haben bereits pausiert — Schwelle zu scharf")
    assert feeder._state == buffer_feeder.STATE_AUTO


# ---------------------------------------------------------------------------
# 4 — Dauerfehler pausiert den Druck ueber den Jam-Weg
# ---------------------------------------------------------------------------

def test_dauerfehler_pausiert_den_druck():
    printer, feeder = make_feeder()
    arm_fault(feeder)

    ticks = int(buffer_feeder.MAIN_TICK_FAULT_SECONDS / MAIN_TICK_INTERVAL) + 5
    run_ticks(feeder, ticks)

    assert feeder._jam_active is True
    assert feeder._state == buffer_feeder.STATE_JAM
    printer.get_reactor().fire_pending_timers()
    scripts = printer.lookup_object('gcode').script_invocations
    assert any("PAUSE" in str(s) for s in scripts), (
        "jam_action wurde nicht ausgeloest: %r" % (scripts,))


# ---------------------------------------------------------------------------
# 5 — Der Tick laeuft trotz Dauerfehler weiter (kein Totalausfall)
# ---------------------------------------------------------------------------

def test_tick_bleibt_planbar():
    printer, feeder = make_feeder()
    arm_fault(feeder)

    naechster = feeder._main_tick(100.0)

    assert naechster == pytest.approx(100.0 + MAIN_TICK_INTERVAL), (
        "Tick hat sich nicht neu eingeplant")


# ---------------------------------------------------------------------------
# 6 — Entsperrung ueber den gewohnten Weg
# ---------------------------------------------------------------------------

def test_clear_jam_setzt_fehlerzaehler_zurueck():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    ticks = int(buffer_feeder.MAIN_TICK_FAULT_SECONDS / MAIN_TICK_INTERVAL) + 5
    run_ticks(feeder, ticks)
    assert feeder._jam_active is True
    heal(feeder)

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._jam_active is False
    assert feeder._main_tick_fault_count == 0
