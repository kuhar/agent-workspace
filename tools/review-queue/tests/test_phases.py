from review_queue.phases import PhaseChange, PhaseStack


def test_nested_scopes_restore_parent_start_time(monkeypatch):
    times = iter(["2026-09-11T00:00:00Z", "2026-09-11T00:01:00Z"])
    monkeypatch.setattr("review_queue.phases.isoformat", lambda: next(times))
    parser = PhaseStack()
    assert parser.consume("::endgroup::\n") is None
    parent = parser.consume("::group::Build\n", log_offset=20)
    child = parser.consume("::group::Clang\r\n", log_offset=40)
    assert child.phase == "Build › Clang"
    assert child.started_at == "2026-09-11T00:01:00Z"
    assert child.log_offset == 40
    restored = parser.consume("::endgroup::\n")
    assert restored.phase == parent.phase
    assert restored.started_at == parent.started_at
    assert restored.log_offset == 20
    assert not restored.entered
    assert parser.consume("::endgroup::\n") == PhaseChange()
    assert parser.consume("::endgroup::\n") is None


def test_only_whole_line_markers_change_phase():
    parser = PhaseStack()
    for line in ["echo ::group::Build", "::endgroup::suffix", "::group::", "ordinary output"]:
        assert parser.consume(line) is None
    change = parser.consume("::group::Clang%0AASan%25%250A")
    assert change.phase == "Clang ASan%%0A"
    assert change.entered
