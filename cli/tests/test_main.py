import io

from cli import main
from autolux.simulate.evaluator import print_evaluation


def test_report_printing_supports_redirected_windows_output(monkeypatch):
    output = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
    errors = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')

    async def print_report():
        print_evaluation(
            {
                'passed': True,
                'score': 1.0,
                'criteria': {
                    'timing': {'value': 8.0, 'passed': True, 'spec': {'max': 15.0}},
                },
            }
        )

    monkeypatch.setattr(main, 'main', print_report)
    monkeypatch.setattr(main.sys, 'stdout', output)
    monkeypatch.setattr(main.sys, 'stderr', errors)
    main.run_sync()
    output.flush()
    printed = output.buffer.getvalue().decode('utf-8')
    assert 'PASS' in printed and '≤ 15.0' in printed and '✓' in printed
    assert errors.encoding == 'utf-8'
