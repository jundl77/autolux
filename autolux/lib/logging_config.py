"""Shared console logging policy for the controller and dashboard processes."""

import logging
import os


def configure_logging(*, verbose=None):
    if verbose is not None:
        os.environ['AUTOLUX_LOG_LEVEL'] = 'DEBUG' if verbose else 'INFO'
    level = logging.DEBUG if os.environ.get('AUTOLUX_LOG_LEVEL') == 'DEBUG' else logging.INFO
    logging.basicConfig(
        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S', level=level
    )
    logging.getLogger().setLevel(level)
    # Werkzeug emits access records at INFO, including dashboard polling.
    # Keep server warnings and Flask's exception logs, even in verbose mode.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
