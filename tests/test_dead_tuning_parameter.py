"""flush_callback_chunk_mm hat keine Wirkung mehr (Audit-Befund N1).

Wurzel
------
Der Parameter wird gelesen (`buffer_config.py`), ist per
`BUFFER_SET CHUNK_MM` zur Laufzeit setzbar, erscheint in `get_status` und ist
in der mitgelieferten `lll.cfg` als „Chunk-Groesse fuer _on_mcu_flush"
beschrieben — aber kein Steuerpfad liest ihn. Die Chunk-Groesse im Flush-Pfad
bestimmt `interrupt_chunk_mm` ueber `_effective_interrupt_chunk_mm`.

Wer den Wert tunt, tunt also ins Leere, und zwar ohne jede Rueckmeldung. In der
mitgelieferten Config steht er auf 45.0, ist also bei praktisch jedem Nutzer
gesetzt.

Fix
---
Der Parameter bleibt lesbar und setzbar — ihn zu entfernen wuerde jede
bestehende Config ungueltig machen, weil Klipper unbekannte Optionen
zurueckweist. Stattdessen wird die Wirkungslosigkeit sichtbar gemacht: einmal
beim Start, wenn er in der Config steht, und bei jedem Laufzeit-Setzen.

NICHT gemacht: ihn wieder wirksam schalten. Welche Groesse der Flush-Pfad
benutzt, ist eine Motion-Entscheidung (`interrupt_chunk_mm` wurde am
2026-05-18 bewusst auf 3 mm gesetzt, wegen 3.6 mm HALL2-HALL1-Marge). Das
gehoert abgestimmt, nicht nebenbei geaendert.
"""

import logging

import pytest

from fakes_klipper import FakeConfig, FakePrinter
from helpers import FakeGCmd
from klipper_extras import buffer_feeder


HINWEIS = "flush_callback_chunk_mm"


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


def baue(values=None):
    printer = FakePrinter()
    printer.fire_event('klippy:connect')
    return printer, buffer_feeder.BufferFeeder(
        FakeConfig(printer=printer, values=values))


def hinweise(logs):
    return [r.getMessage() for r in logs.records
            if HINWEIS in r.getMessage() and 'ohne Wirkung' in r.getMessage()]


# ---------------------------------------------------------------------------
# 1 — Steht der Wert in der Config, sagt der Start es einmal
# ---------------------------------------------------------------------------

def test_hinweis_beim_start_wenn_gesetzt(logs):
    baue({"flush_callback_chunk_mm": 45.0})

    treffer = hinweise(logs)
    assert len(treffer) == 1, (
        "erwartet genau ein Hinweis, bekommen %d" % len(treffer))
    assert "interrupt_chunk_mm" in treffer[0], (
        "Hinweis nennt die wirksame Alternative nicht: %s" % treffer[0])


# ---------------------------------------------------------------------------
# 2 — Ohne Eintrag in der Config bleibt der Start still
# ---------------------------------------------------------------------------

def test_kein_hinweis_wenn_nicht_gesetzt(logs):
    baue()

    assert not hinweise(logs), (
        "Hinweis ohne Config-Eintrag: %s" % hinweise(logs))


# ---------------------------------------------------------------------------
# 3 — Auch das Laufzeit-Setzen meldet die Wirkungslosigkeit
# ---------------------------------------------------------------------------

def test_buffer_set_meldet_wirkungslosigkeit():
    printer, feeder = baue()

    feeder.cmd_BUFFER_SET(FakeGCmd({'CHUNK_MM': 30.0}))

    meldungen = printer.lookup_object('gcode').info_messages
    passend = [m for m in meldungen if HINWEIS in m and 'ohne Wirkung' in m]
    assert passend, (
        "BUFFER_SET CHUNK_MM meldet die Wirkungslosigkeit nicht: %s"
        % meldungen)


# ---------------------------------------------------------------------------
# 4 — Regressionswaechter: Parameter bleibt gueltig und sichtbar
# ---------------------------------------------------------------------------

def test_parameter_bleibt_lesbar_und_setzbar():
    printer, feeder = baue({"flush_callback_chunk_mm": 45.0})
    assert feeder.flush_callback_chunk_mm == pytest.approx(45.0)

    feeder.cmd_BUFFER_SET(FakeGCmd({'CHUNK_MM': 30.0}))

    assert feeder.flush_callback_chunk_mm == pytest.approx(30.0)
    status = feeder.get_status(printer.get_reactor().monotonic())
    assert status['flush_callback_chunk_mm'] == pytest.approx(30.0)
