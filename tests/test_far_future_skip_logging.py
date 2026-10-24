"""Far-Future-Skip wiederholt sich nicht mit Tickrate (Audit-Befund M1).

Wurzel
------
Im Pfad `forced_t0=None` (Taster, MANUAL_FEED, INITIAL_GRIP, LOAD/UNLOAD,
Park-Fill-Fortsetzung) ankert `_plan_t0_anchor` auf
`toolhead.get_last_move_time() + lead_time` und ueberspringt den Submit, wenn
das mehr als MAX_T0_LOOKAHEAD_S vor `mcu_now` liegt (P7-77 B).

Waehrend eines Drucks haelt Klipper die Toolhead-Queue bis `buffer_time_high`
= 2.0 s gefuellt. Mit `lead_time` 0.12 greift der Skip also ab 1.88 s Vorlauf —
das ist der Normalzustand im Druck, nicht die Ausnahme. Der Tick versucht es
50-mal pro Sekunde erneut, und jeder Versuch kostet:

  - zwei identische Warnungen (geloggt in `_plan_t0_anchor` UND in
    `_compute_t0_anchor` fuer denselben Plan),
  - einen Lookahead-Flush durch `get_last_move_time()` selbst,
  - bei > REPRIME_GAP_S Feeder-Ruhe zusaetzlich ein
    `toolhead.flush_step_generation()` aus dem Reprime.

Gemessen (Audit-Beleg, 50 Ticks): 0 Submits, 100 Warnungen, 50 Reprimes,
100 Toolhead-Flushes. Der P7-58-Kommentar beschreibt, was ein einzelner solcher
Flush pro Chunk im Druck bewirkt: sichtbare Extruder-Pausen.

Umgesetzt (Teil 1 von M1, zugleich Audit-Befund N3)
---------------------------------------------------
Die doppelte Warnung faellt weg: `_plan_t0_anchor` loggte sie und
`_compute_t0_anchor` gleich noch einmal fuer denselben Plan. Jetzt meldet sich
ein Skip genau einmal.

OFFEN (Teil 2 von M1, bewusst NICHT hier geloest): die Wiederholung selbst.
Jeder Fix dafuer braucht eine Entscheidung mit dem Maintainer, weil er mit den
Fixtures aus dem Refactor kollidiert — `test_pending_stream_lifecycle.py`
dreht in `drain_pending` die Toolhead-Uhr von 100 auf 0 zurueck, waehrend die
Reactor-Zeit stehen bleibt. Real ist `get_last_move_time()` monoton; eine
Bremse, die sich darauf stuetzt, laesst genau diese vier Tests scheitern.

NICHT umgesetzt (urspruengliche Audit-Empfehlung, nach Pruefung verworfen):
den Reprime hinter den Anchor-Entscheid zu ziehen. Der Reprime kann ueber
`flush_step_generation()` bei Host-Last 100 ms und mehr blockieren; ein davor
berechnetes t0 waere danach abgelaufen ("Timer too close"). Genau deshalb liest
der Code `mcu_now` nach dem Reprime neu.
"""

import logging

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import set_sensor_active
from klipper_extras import buffer_feeder
from klipper_extras._buffer_common import MAIN_TICK_INTERVAL


SKIP_MARKER = "anchor skipped"


def make_feeder(state=buffer_feeder.STATE_MANUAL_FEED):
    printer = FakePrinter()
    printer.fire_event('klippy:connect')
    feeder = buffer_feeder.BufferFeeder(FakeConfig(printer=printer, values={
        "use_flush_callback_bang_bang": True, "lead_time": 0.12,
    }))
    feeder._startup_grace_done = True
    feeder._state = state
    feeder._stepcompress_primed = True
    for sensor in ('hall_overflow', 'hall_full', 'hall_empty'):
        set_sensor_active(feeder, sensor, False)
    return printer, feeder


def dauerfeed(feeder, speed=15.0):
    feeder._continuous_feed = True
    feeder._continuous_feed_direction = 1
    feeder._continuous_feed_speed = speed


def ticks_mit_vollem_toolhead(feeder, printer, anzahl, vorlauf=1.95,
                              start=100.0):
    """Tickt mit gefuellter Toolhead-Queue, wie waehrend eines Drucks."""
    toolhead = printer.lookup_object('toolhead')
    mcu = feeder.stepper.get_mcu()
    t = start
    for _ in range(anzahl):
        printer.get_reactor().now = t
        toolhead.last_move_time = mcu.estimated_print_time(t) + vorlauf
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


def skip_warnungen(logs):
    return [r for r in logs.records if SKIP_MARKER in r.getMessage()]


# ---------------------------------------------------------------------------
# 1 — Ein Skip meldet sich genau einmal, nicht doppelt
# ---------------------------------------------------------------------------

def test_ein_skip_meldet_sich_einmal(logs):
    """Ein einzelner uebersprungener Submit erzeugt genau eine Warnung.

    Vor dem Fix waren es zwei: `_plan_t0_anchor` loggte, und
    `_compute_t0_anchor` loggte dieselbe Aussage fuer denselben Plan noch
    einmal. Absichtlich auf Ebene EINES Submits geprueft, nicht eines Ticks —
    ein Tick loest zwei echte Versuche aus (Dauerfeed-Chunk und
    Nachlauf-Pumpe), und zwei Versuche duerfen auch zweimal melden.
    """
    printer, feeder = make_feeder()
    toolhead = printer.lookup_object('toolhead')
    mcu = feeder.stepper.get_mcu()
    printer.get_reactor().now = 100.0
    toolhead.last_move_time = mcu.estimated_print_time(100.0) + 1.95

    feeder._submit_move(10.0, 15.0)

    assert len(skip_warnungen(logs)) == 1, (
        "ein einzelner Submit erzeugte %d Warnungen"
        % len(skip_warnungen(logs)))


# ---------------------------------------------------------------------------
# 2 — Regressionswaechter: ein naher Toolhead-Anker submittet weiterhin
# ---------------------------------------------------------------------------

def test_naher_toolhead_submittet_weiterhin():
    printer, feeder = make_feeder()
    dauerfeed(feeder)
    motion = printer.lookup_object('motion_queuing')
    vorher = len(motion.append_calls)

    # Toolhead nur knapp voraus — der Skip darf hier nicht greifen.
    ticks_mit_vollem_toolhead(feeder, printer, 3, vorlauf=0.05)

    neu = [c for c in motion.append_calls[vorher:] if c[0] is feeder.trapq]
    assert neu, "kein Trapezoid trotz nahem Toolhead-Anker"
