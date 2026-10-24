import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from autolux.lib.logging_config import configure_logging


@pytest.fixture(autouse=True)
def restore_logging(monkeypatch):
    root, web = logging.getLogger(), logging.getLogger('werkzeug')
    levels = root.level, web.level
    monkeypatch.setenv('AUTOLUX_LOG_LEVEL', 'INFO')
    yield
    root.setLevel(levels[0])
    web.setLevel(levels[1])


@pytest.mark.parametrize('verbose', [False, True])
def test_real_http_requests_are_quiet_but_application_errors_are_logged(caplog, verbose):
    from flask import Flask
    from werkzeug.serving import make_server

    configure_logging(verbose=verbose)
    app = Flask(__name__)

    @app.route('/')
    def ok():
        return 'ready'

    @app.route('/broken')
    def broken():
        raise RuntimeError('dashboard failed to render')

    server = make_server('127.0.0.1', 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        for _ in range(5):
            with urllib.request.urlopen(base, timeout=5) as response:
                assert response.read() == b'ready'
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + '/broken', timeout=5)
        assert error.value.code == 500
        error.value.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not any(
        record.name == 'werkzeug' and 'GET ' in record.getMessage() for record in caplog.records
    )
    assert 'dashboard failed to render' in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_verbose_mode_reaches_a_separate_dashboard_process():
    configure_logging(verbose=True)
    child = subprocess.run(
        [
            sys.executable,
            '-c',
            'import logging; from autolux.lib.logging_config import configure_logging; '
            'configure_logging(); logging.debug("diagnostics enabled"); '
            'logging.getLogger("werkzeug").info("GET /poll")',
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert 'diagnostics enabled' in child.stderr
    assert 'GET /poll' not in child.stderr
    assert os.environ['AUTOLUX_LOG_LEVEL'] == 'DEBUG'
