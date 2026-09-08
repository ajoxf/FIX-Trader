"""The launcher's guard rails.

Both tests here exist because of one incident: the launcher was run on
Anaconda's `base` environment (Python 3.7, old Flask), the children died on an
ImportError, and it restarted them in a loop — printing the same traceback six
times until the one line that explained it had scrolled away.
"""
import sys

import pytest

sys.path.insert(0, __import__('os').path.dirname(
    __import__('os').path.dirname(__import__('os').path.abspath(__file__))))

import start  # noqa: E402


def test_this_interpreter_is_acceptable():
    assert start.check_the_interpreter() is None


def test_an_old_python_is_refused_with_the_command_that_fixes_it(monkeypatch):
    """Naming the interpreter matters: the whole confusion was not knowing
    WHICH python was running."""
    monkeypatch.setattr(start.sys, 'version_info', (3, 7, 9))
    problem = start.check_the_interpreter()
    assert problem is not None
    assert '3.7.9' in problem
    assert sys.executable in problem          # which python, exactly
    assert 'conda activate fixtrader' in problem
    assert 'base' in problem                  # names the likely cause


def test_an_old_flask_is_refused_by_name():
    """Flask 1.x has no @app.get, so every route raises AttributeError at
    import — a failure that reads as a code bug, not an environment one."""
    problem = start.check_the_interpreter(version_of=lambda: '1.1.2')
    assert problem is not None
    assert '1.1.2' in problem
    assert 'requirements.txt' in problem


def test_a_version_that_cannot_be_read_is_allowed_not_refused():
    """`flask.__version__` is deprecated and goes away in Flask 3.2. Reading
    it wrongly and refusing would block a perfectly good environment — the
    failure this whole function exists to prevent, pointed the other way."""
    assert start.check_the_interpreter(version_of=lambda: None) is None
    assert start.check_the_interpreter(version_of=lambda: 'weird') is None


def test_the_real_version_reader_answers_something_usable():
    version = start.flask_version()
    assert version is None or version[0].isdigit()


def test_the_version_check_runs_before_anything_is_written(tmp_path, monkeypatch,
                                                           capsys):
    """It must refuse before it creates config.json and .env, or a bad run
    leaves files behind that look like a successful first run."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(start.sys, 'version_info', (3, 7, 9))
    code = start.main(['--no-browser'])
    assert code == 2
    assert not (tmp_path / 'config.json').exists()
    assert not (tmp_path / '.env').exists()
    assert 'cannot run here' in capsys.readouterr().out
