from __future__ import annotations

from pathlib import Path

import pytest
from lupa.lua51 import LuaRuntime

from datum.api.internals.schemas import LogLine
from datum.config import MAX_LOG_ATTRIBUTES, MAX_LOG_MESSAGE

SCRIPT = Path(__file__).parents[1] / "fluent-bit" / "datum.lua"
TIMESTAMP = 1791983320.5


@pytest.fixture(scope="module")
def datum_line():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(SCRIPT.read_text())
    return lua.globals().datum_line, lua


def to_python(value):
    if hasattr(value, "items"):
        return {key: to_python(item) for key, item in value.items()}
    return value


def convert(datum_line, record: dict) -> tuple[int, float, dict]:
    function, lua = datum_line
    code, timestamp, line = function("datum.app", TIMESTAMP, lua.table_from(record))
    return code, timestamp, to_python(line)


def as_log_line(line: dict) -> LogLine:
    return LogLine(ts="2026-10-14T10:00:00Z", **line)


def test_a_record_becomes_a_line_and_keeps_its_timestamp(datum_line):
    record = {"product": "metal", "service": "metald", "level": "WARN", "msg": "disk low"}

    code, timestamp, line = convert(datum_line, record)

    assert (code, timestamp) == (2, TIMESTAMP)
    assert line == {
        "product": "metal",
        "service": "metald",
        "source": "unknown",
        "level": "warn",
        "message": "disk low",
    }


def test_other_fields_become_attributes(datum_line):
    record = {"message": "started", "virtual_machine_id": "vm-1", "pid": 42, "nested": {"a": 1}}

    _, _, line = convert(datum_line, record)

    assert line["attributes"] == {"virtual_machine_id": "vm-1", "pid": "42"}


def test_a_journald_record_is_named_after_its_unit(datum_line):
    record = {
        "MESSAGE": "listening",
        "PRIORITY": "3",
        "SYSLOG_IDENTIFIER": "metald",
        "_SYSTEMD_UNIT": "metal.service",
    }

    _, _, line = convert(datum_line, record)

    assert (line["service"], line["source"], line["level"]) == ("metald", "metal.service", "error")


def test_a_tailed_file_is_its_source(datum_line):
    _, _, line = convert(datum_line, {"log": "x", "filename": "/var/log/app/worker.log"})

    assert line["source"] == "worker.log"


@pytest.mark.parametrize("level", ["not a level", "", "9lives", "x" * 201])
def test_an_unusable_level_falls_back_to_info(datum_line, level):
    _, _, line = convert(datum_line, {"message": "x", "level": level})

    assert line["level"] == "info"


def test_names_are_made_storable(datum_line):
    record = {"message": "x", "product": "my product", "service": "web worker!", "1st key": "v"}

    _, _, line = convert(datum_line, record)

    assert (line["product"], line["service"]) == ("my_product", "web_worker_")
    assert line["attributes"] == {"_1st_key": "v"}


def test_a_long_message_is_cut_on_a_character_boundary(datum_line):
    message = "a" * (MAX_LOG_MESSAGE - 1) + "é" * 10

    _, _, line = convert(datum_line, {"message": message})

    assert line["message"] == "a" * (MAX_LOG_MESSAGE - 1)


def test_attributes_stop_at_the_cap(datum_line):
    record = {"message": "x", **{f"key_{index:03}": "v" for index in range(MAX_LOG_ATTRIBUTES + 5)}}

    _, _, line = convert(datum_line, record)

    assert len(line["attributes"]) == MAX_LOG_ATTRIBUTES


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"message": 12, "level": 6},
        {"MESSAGE": "x" * 20_000, "PRIORITY": "7", "_SYSTEMD_UNIT": "a b.service"},
        {"log": "x", "product": "", "service": "/", "filename": "dir/"},
        {"msg": "x", "level": "ERR OR", **{f"k{index}": index for index in range(100)}},
    ],
)
def test_every_line_passes_datum_validation(datum_line, record):
    _, _, line = convert(datum_line, record)

    as_log_line(line)
