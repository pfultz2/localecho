import pytest

from localecho import parse_args


def test_command_runs_as_given():
    opts, cmd = parse_args(["ssh", "-p", "2222", "user@host"])
    assert cmd == ["ssh", "-p", "2222", "user@host"]
    assert opts["predict"] == "adaptive" and opts["mark"] == "underline"


def test_options_before_command():
    opts, cmd = parse_args(["--mark=grey", "--predict=always", "--debug=/tmp/x", "et", "host"])
    assert (opts["mark"], opts["predict"], opts["debug"]) == ("grey", "always", "/tmp/x")
    assert cmd == ["et", "host"]


def test_options_after_command_belong_to_it():
    opts, cmd = parse_args(["ssh", "--mark=grey", "host"])
    assert cmd == ["ssh", "--mark=grey", "host"]
    assert opts["mark"] == "underline"


def test_no_underline_alias():
    opts, _ = parse_args(["--no-underline", "ssh", "host"])
    assert opts["mark"] == "none"


def test_exec_is_still_accepted():
    _, cmd = parse_args(["--exec", "--", "bash", "--norc"])
    assert cmd == ["bash", "--norc"]


def test_double_dash_ends_options():
    _, cmd = parse_args(["--", "--mark=grey"])
    assert cmd == ["--mark=grey"]


@pytest.mark.parametrize("argv", [["--mark=bold", "h"], ["--predict=sometimes", "h"], [], ["--exec"],
                                  ["-p", "2222", "host"]])
def test_bad_arguments_exit(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)
