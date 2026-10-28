import pytest

from localecho import parse_args


def test_ssh_arguments_pass_through():
    opts, cmd = parse_args(["-p", "2222", "user@host"])
    assert cmd == ["ssh", "-p", "2222", "user@host"]
    assert opts["predict"] == "adaptive" and opts["mark"] == "underline"


def test_options_before_host():
    opts, cmd = parse_args(["--mark=grey", "--predict=always", "--debug=/tmp/x", "host"])
    assert (opts["mark"], opts["predict"], opts["debug"]) == ("grey", "always", "/tmp/x")
    assert cmd == ["ssh", "host"]


def test_no_underline_alias():
    opts, _ = parse_args(["--no-underline", "host"])
    assert opts["mark"] == "none"


def test_exec_runs_any_command():
    _, cmd = parse_args(["--exec", "--", "bash", "--norc"])
    assert cmd == ["bash", "--norc"]


def test_double_dash_ends_options():
    _, cmd = parse_args(["--", "--mark=grey"])
    assert cmd == ["ssh", "--mark=grey"]


@pytest.mark.parametrize("argv", [["--mark=bold", "h"], ["--predict=sometimes", "h"], [], ["--exec"]])
def test_bad_arguments_exit(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
