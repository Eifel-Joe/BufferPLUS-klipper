"""H1 (Audit 2026-09-10): Fangnetz fuer den Flush-Callback.

Wurzel
------
`_on_mcu_flush` ist die einzige Klipper-Einstiegsstelle des Plugins ohne
`try/except`. Klippers `motion_queuing` ruft die Flush-Callbacks ungeschuetzt
auf (`_advance_flush_time`, innerhalb von `reactor.assert_no_pause()`); faengt
`_flush_handler` eine Ausnahme, ruft er `invoke_shutdown("Exception in
flush_handler")` und stellt den Flush-Timer ab. Eine einzige Ausnahme im
Flush-Pfad beendet damit den laufenden Druck. Derselbe Fehler im Reactor-Tick
wird von `_main_tick` geschluckt (Z.2064).

Belegt im Audit-Bericht `docs/superpowers/specs/2026-09-10-code-audit-risikokern.md`
(Befund H1) und mit `2026-09-10-code-audit-belege/test_audit.py::
test_on_mcu_flush_propagates_exception`.

Verhalten laut User-Entscheidung 2026-09-12
-------------------------------------------
Ein einzelner Aussetzer wird geschluckt: der naechste Flush foerdert normal
weiter, ein Abbruch waere unverhaeltnismaessig.

Ein dauerhafter Fehler muss den Druck PAUSIEREN, nicht nur die Foerderung
stilllegen. Begruendung des Users: ein Buffer, der nicht mehr foerdert,
zwingt den Extruder, das Filament direkt von der Spule zu ziehen; bei dem
kurzen Armweg der Mellow LLL Plus ist Unterextrusion dann die sichere Folge.
Dafuer wird der vorhandene Jam-Weg benutzt (`_trigger_jam` -> `jam_action`,
auf diesem Drucker `_LLL_JAM_PAUSE`), damit die Entsperrung der gewohnte
`BUFFER_CLEAR_JAM` bleibt.

Die Eskalation darf NICHT aus dem Callback heraus laufen: er steht unter
`reactor.assert_no_pause()`. Der Callback protokolliert und setzt ein Flag,
`_main_tick` loest den Jam aus.

Taktung (klippy/extras/motion_queuing.py auf dem Drucker): rund 0.25 s im
Druck, rund 0.20 s im Hintergrund. Drei Fehler in Folge sind also nach
deutlich unter einer Sekunde erreicht.
"""

import logging

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd, set_sensor_active
from klipper_extras import buffer_feeder


FLUSH_MARKER = "flush callback fault"


def make_feeder(values=None):
    base = {"use_flush_callback_bang_bang": True, "jam_action": "PAUSE"}
    if values:
        base.update(values)
    printer = FakePrinter()
    printer.fire_event('klippy:connect')
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values=base))
    feeder._startup_grace_done = True
    feeder._state = buffer_feeder.STATE_AUTO
    feeder._stepcompress_primed = True
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    return printer, feeder


def arm_fault(feeder):
    """Laesst den ersten Aufruf innerhalb von _flush_submit_streaming_chunk
    werfen — stellvertretend fuer jeden Fehler im Flush-Pfad."""
    def boom():
        raise RuntimeError("Fehler im Flush-Pfad")
    feeder._compute_target_feed_speed = boom


def flush(feeder, printer):
    now = printer.get_reactor().monotonic()
    feeder._on_mcu_flush(now, now + 0.25)


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


# ---------------------------------------------------------------------------
# 1 — Kern: keine Ausnahme verlaesst den Callback
# ---------------------------------------------------------------------------

def test_ausnahme_verlaesst_den_callback_nicht():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    flush(feeder, printer)   # darf nicht werfen


# ---------------------------------------------------------------------------
# 2 — Einzelner Aussetzer bleibt folgenlos
# ---------------------------------------------------------------------------

def test_einzelner_aussetzer_loest_keinen_jam_aus():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    flush(feeder, printer)

    # Naechster Flush funktioniert wieder.
    feeder._compute_target_feed_speed = lambda: 0.0
    flush(feeder, printer)
    feeder._main_tick(printer.get_reactor().monotonic())

    assert feeder._jam_active is False
    assert feeder._state == buffer_feeder.STATE_AUTO
    assert feeder._flush_fault_count == 0, "Zaehler nach erfolgreichem Flush nicht zurueckgesetzt"


# ---------------------------------------------------------------------------
# 3 — Dauerfehler pausiert den Druck ueber den Jam-Weg
# ---------------------------------------------------------------------------

def test_dauerfehler_loest_jam_und_jam_action_aus():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    for _ in range(buffer_feeder.FLUSH_FAULT_LIMIT):
        flush(feeder, printer)

    feeder._main_tick(printer.get_reactor().monotonic())

    assert feeder._jam_active is True
    assert feeder._state == buffer_feeder.STATE_JAM
    # jam_action laeuft ueber einen 1ms-Timer, nicht direkt.
    printer.get_reactor().fire_pending_timers()
    scripts = printer.lookup_object('gcode').script_invocations
    assert any("PAUSE" in str(s) for s in scripts), (
        "jam_action wurde nicht ausgeloest: %r" % (scripts,))


# ---------------------------------------------------------------------------
# 4 — Eskalation nie aus dem Callback heraus (reactor.assert_no_pause)
# ---------------------------------------------------------------------------

def test_callback_selbst_loest_keinen_jam_aus():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    for _ in range(buffer_feeder.FLUSH_FAULT_LIMIT + 2):
        flush(feeder, printer)

    assert feeder._state == buffer_feeder.STATE_AUTO, (
        "Jam wurde im Callback ausgeloest — dort ist der Reactor gesperrt")
    assert feeder._jam_active is False
    assert feeder._flush_fault_pending, "Eskalation nicht fuer den Tick vorgemerkt"


# ---------------------------------------------------------------------------
# 5 — Kein Logsturm (P7-78-Muster, Issue #59)
# ---------------------------------------------------------------------------

def test_logausgabe_gedrosselt(logs):
    printer, feeder = make_feeder()
    arm_fault(feeder)
    for _ in range(50):
        flush(feeder, printer)

    treffer = [r for r in logs.records if FLUSH_MARKER in r.getMessage()]
    assert 0 < len(treffer) <= 5, (
        "50 Fehl-Flushes erzeugten %d Logzeilen — erwartet hoechstens 5"
        % len(treffer))


# ---------------------------------------------------------------------------
# 6 — Entsperrung ueber den gewohnten Weg
# ---------------------------------------------------------------------------

def test_clear_jam_setzt_fehlerzaehler_zurueck():
    printer, feeder = make_feeder()
    arm_fault(feeder)
    for _ in range(buffer_feeder.FLUSH_FAULT_LIMIT):
        flush(feeder, printer)
    feeder._main_tick(printer.get_reactor().monotonic())
    assert feeder._jam_active is True

    feeder.cmd_BUFFER_CLEAR_JAM(FakeGCmd())

    assert feeder._jam_active is False
    assert feeder._flush_fault_count == 0
    assert feeder._flush_fault_pending == ""
