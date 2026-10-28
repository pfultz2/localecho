# localecho

Run `ssh`, `et` or any other interactive command with mosh-style predictive
local echo. Typed characters appear immediately (underlined or grey until the
server echoes them back) while the session itself stays a normal session of
that command.

```
pip install .
localecho ssh user@host
localecho --mark=grey et user@host
localecho kubectl exec -it mypod -- bash
```

localecho's own options go before the command; everything from the command
on is passed to it unchanged. There is no point wrapping `mosh`, which already
predicts on its own. See `localecho --help` for all options.

## Tests

```
pip install -e '.[test]'
pytest                      # everything
pytest -m "not integration" # fast unit tests only
tox                         # unit tests on each installed Python, integration tests, lint
tox -e integration          # just the end-to-end tests
```

The integration tests run `bash` (and `vim`/`tmux` if installed) inside a
pseudo-terminal behind a simulated 300 ms round trip, and compare what the
screen shows against the same keystrokes without prediction.
