"""Prevent concurrent large report builders without serializing financial work."""
import os
import stat

import pytest
from finplan_platform.reporting.render_lock import render_lock
from finplan_platform.reporting.reports import EvidenceError, PublicationDeferred


def test_shared_output_lock_contends_and_other_output_is_independent(tmp_path):
    root = tmp_path / 'reports'
    with render_lock(root):
        inode = (root / '.render.lock').stat().st_ino
        with pytest.raises(PublicationDeferred, match='Another report builder'):
            with render_lock(root): pytest.fail('Concurrent builder entered')
        with render_lock(tmp_path / 'different-report-root'): pass
    with render_lock(root):
        assert (root / '.render.lock').stat().st_ino == inode
        assert stat.S_IMODE((root / '.render.lock').stat().st_mode) == 0o600


def test_exception_releases_lock_without_deleting_shared_inode(tmp_path):
    with pytest.raises(RuntimeError, match='controlled crash'):
        with render_lock(tmp_path): raise RuntimeError('controlled crash')
    with render_lock(tmp_path): pass
    assert (tmp_path / '.render.lock').exists()


def test_symlink_hardlink_and_nonempty_files_are_refused_without_mutation(tmp_path):
    target = tmp_path / 'unrelated'
    target.write_bytes(b'preserve this unrelated content')
    target.chmod(0o664)
    root = tmp_path / 'reports';root.mkdir()
    lock = root / '.render.lock';lock.symlink_to(target)
    with pytest.raises(EvidenceError, match='safely'):
        with render_lock(root): pass
    assert target.read_bytes() == b'preserve this unrelated content'
    assert stat.S_IMODE(target.stat().st_mode) == 0o664
    lock.unlink();os.link(target, lock)
    with pytest.raises(EvidenceError, match='singly linked'):
        with render_lock(root): pass
    assert target.read_bytes() == b'preserve this unrelated content'
    assert stat.S_IMODE(target.stat().st_mode) == 0o664
    lock.unlink();lock.write_bytes(b'unrelated preexisting file')
    with pytest.raises(EvidenceError, match='empty'):
        with render_lock(root): pass
    assert lock.read_bytes() == b'unrelated preexisting file'


def test_fifo_is_rejected_without_blocking(tmp_path):
    os.mkfifo(tmp_path / '.render.lock')
    with pytest.raises(EvidenceError, match='regular file'):
        with render_lock(tmp_path): pass


def test_cli_contention_returns75_before_any_aws_or_aggregation(monkeypatch, tmp_path, capsys):
    from scripts import modeling_report as cli
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    monkeypatch.setattr(ModelingEvidence, 'from_ec2', lambda **kw: pytest.fail('AWS before render lock'))
    monkeypatch.setattr(cli, 'aggregate', lambda *a, **kw: pytest.fail('Concurrent aggregation'))
    with render_lock(tmp_path):
        code = cli.cli_exit_code(['--experiment','controlled-lock','--output',str(tmp_path),'--no-include-interim'])
    assert code == 75
    assert 'Report publication deferred' in capsys.readouterr().err


def test_cli_releases_cohort_copies_before_encoding(monkeypatch, tmp_path):
    import weakref
    from scripts import modeling_report as cli
    from finplan_platform.core.modeling_evidence import ModelingEvidence
    class Report(dict): pass
    class Reader:
        def read_registry(self, experiment): return {'approaches':[], 'comparison_policy':{}}
    refs = []
    def aggregate(*args, **kwargs):
        report = Report(runs=[], evidence_failures=[], missing_slots=[], leaderboard=[])
        refs.append(weakref.ref(report))
        return report
    monkeypatch.setattr(ModelingEvidence, 'from_ec2', lambda **kw: Reader())
    monkeypatch.setattr(cli, 'aggregate', aggregate)
    monkeypatch.setattr(cli, 'consolidate', lambda reports: {'runs':[], 'evidence_failures':[], 'missing_slots':[], 'leaderboard':[]})
    monkeypatch.setattr(cli, 'aggregate_interim', aggregate)
    def write(*args, **kwargs):
        assert len(refs) == 3 and all(ref() is None for ref in refs)
        return {'directory':str(tmp_path), 'archive':str(tmp_path), 'revision':'controlled-memory-only'}
    monkeypatch.setattr(cli, 'write_report', write)
    assert cli.main(['--experiment','controlled-a','--include-experiment','controlled-b','--output',str(tmp_path),'--no-s3-archive']) == 0


def test_symlink_output_root_is_refused_without_following_it(tmp_path):
    target = tmp_path / 'unrelated';target.mkdir()
    output = tmp_path / 'linked-reports';output.symlink_to(target, target_is_directory=True)
    with pytest.raises(EvidenceError, match='symbolic link'):
        with render_lock(output): pass
    assert list(target.iterdir()) == []
