import pytest
import os

from click.testing import CliRunner


@pytest.fixture()
def cli_runner(pushd):
    # this will make sure we are back at `cwd`
    # after this test is finished
    pushd(os.getcwd())

    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:  # click 8.2 dropped the argument and always keeps stderr apart
        runner = CliRunner()
    yield runner
